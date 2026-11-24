#include "EcgPcb.h"

#include <SPI.h>
#include <esp_system.h>
#include <esp_timer.h>

namespace {
// Preserve the PCB build's external input configuration with internal bias off.
constexpr uint32_t CNFG_GEN_VALUE = 0x080004;
constexpr uint32_t CNFG_ECG_VALUE =
    0x805000; // 128 sps, gain 20, HP/LP enabled.
constexpr uint32_t ECG_LATENCY_TICKS = 4906;
constexpr uint32_t MNGR_INT_VALUE =
    0x380004; // EFIT = 8; interrupt pins unused.
constexpr uint32_t FIFO_READY = 1UL << 23;
constexpr uint32_t FIFO_OVERFLOW = 1UL << 22;
constexpr uint32_t PLL_UNLOCK = 1UL << 8;
constexpr uint8_t SYNCH_REGISTER = 0x09;
const SPISettings MAX_SPI(1000000, MSBFIRST, SPI_MODE0);
} // namespace

void EcgPcb::begin() {
  session = esp_random();
  if (!session) {
    session = 1;
  }
  for (const Afe &afe : afes) {
    pinMode(afe.cs, OUTPUT);
    digitalWrite(afe.cs, HIGH);
  }
  // Shared D13 SCK, D11 COPI, D12 CIPO with individual software select pins.
  SPI.begin(SCK, MISO, MOSI, -1);
  resetAfes();
}

void EcgPcb::stop() {
  running = false;
  retryAfter = 0;
  fault = nullptr;
  txHead = txTail = 0;
  resetAfes();
}

void EcgPcb::start() {
  discardQueuedData();
  emitEvent(0, "MODE_CHANGE");
  if (!beginCapture()) {
    handleFault(false);
  }
}

void EcgPcb::service() {
  if (running) {
    serviceAfes();
    const int64_t now = esp_timer_get_time();
    if (!fault && now - lastStatus >= 1000000) {
      lastStatus = now;
      for (size_t index = 0; index < AFE_COUNT; ++index) {
        emitStatus(index);
      }
    }
    if (fault) {
      handleFault(true);
    }
  } else if (retryAfter && esp_timer_get_time() >= retryAfter &&
             txHead == txTail && !beginCapture()) {
    handleFault(false);
  }
}

void EcgPcb::reportInvalidCommand() {
  const int64_t now = esp_timer_get_time();
  if (now - lastInvalidCommand >= 1000000) {
    lastInvalidCommand = now;
    emitEvent(0, "INVALID_MODE");
  }
}

bool EcgPcb::waitForClient(uint32_t milliseconds) const {
  for (uint32_t elapsed = 0; elapsed < milliseconds; elapsed += 10) {
    if (!clientActive()) {
      return false;
    }
    delay(min<uint32_t>(10, milliseconds - elapsed));
  }
  return clientActive();
}

uint32_t EcgPcb::readRegister(const Afe &afe, uint8_t address) const {
  SPI.beginTransaction(MAX_SPI);
  digitalWrite(afe.cs, LOW);
  SPI.transfer((address << 1) | 1);
  uint32_t value = uint32_t(SPI.transfer(0)) << 16;
  value |= uint32_t(SPI.transfer(0)) << 8;
  value |= SPI.transfer(0);
  digitalWrite(afe.cs, HIGH);
  SPI.endTransaction();
  return value;
}

void EcgPcb::writeRegister(const Afe &afe, uint8_t address,
                           uint32_t value) const {
  SPI.beginTransaction(MAX_SPI);
  digitalWrite(afe.cs, LOW);
  SPI.transfer(address << 1);
  SPI.transfer((value >> 16) & 0xff);
  SPI.transfer((value >> 8) & 0xff);
  SPI.transfer(value & 0xff);
  digitalWrite(afe.cs, HIGH);
  SPI.endTransaction();
}

void EcgPcb::synchronizeAll() const {
  // Write-only broadcast: every AFE sees the same last SCLK edge.
  // Read transactions always select just one AFE on the shared CIPO line.
  SPI.beginTransaction(MAX_SPI);
  for (const Afe &afe : afes) {
    digitalWrite(afe.cs, LOW);
  }
  SPI.transfer(SYNCH_REGISTER << 1);
  SPI.transfer(0);
  SPI.transfer(0);
  SPI.transfer(0);
  for (const Afe &afe : afes) {
    digitalWrite(afe.cs, HIGH);
  }
  SPI.endTransaction();
}

void EcgPcb::resetAfes() const {
  for (const Afe &afe : afes) {
    writeRegister(afe, 0x08, 0);
  }
}

bool EcgPcb::configureAfe(size_t index) {
  Afe &afe = afes[index];
  readRegister(afe, 0x0F);
  afe.id = readRegister(afe, 0x0F);
  const uint32_t part = afe.id & 0xF03000UL;
  if (part != 0x501000UL && part != 0x503000UL) {
    setFault(index + 1, "CHIP_ID");
    return false;
  }
  writeRegister(afe, 0x02, 0); // INTB unused.
  writeRegister(afe, 0x03, 0); // INT2B unused.
  writeRegister(afe, 0x04, MNGR_INT_VALUE);
  writeRegister(afe, 0x10, CNFG_GEN_VALUE);
  writeRegister(afe, 0x12, 0); // Disable calibration.
  writeRegister(afe, 0x14, 0); // Connect electrode inputs.
  writeRegister(afe, 0x15, CNFG_ECG_VALUE);
  afe.sequence = 0;
  afe.threshold.reset();
  return true;
}

bool EcgPcb::verifyAfe(size_t index) {
  const Afe &afe = afes[index];
  // The two supported AFE variants have different reserved bits.
  const bool max30003 = (afe.id & 0xF03000UL) == 0x503000UL;
  const uint32_t generalMask = max30003 ? 0xF83FFF : 0xFEFFFF;
  const uint32_t managerMask = max30003 ? 0xF80077 : 0xFF007F;
  const uint32_t interruptMask = max30003 ? 0xF00F03 : 0xFFFF03;
  if ((readRegister(afe, 0x10) & generalMask) != CNFG_GEN_VALUE ||
      (readRegister(afe, 0x14) & 0xBF0000) != 0 ||
      (readRegister(afe, 0x12) & 0x707FFF) != 0 ||
      (readRegister(afe, 0x15) & 0xC37000) != CNFG_ECG_VALUE ||
      (readRegister(afe, 0x04) & managerMask) != MNGR_INT_VALUE ||
      (readRegister(afe, 0x02) & interruptMask) != 0 ||
      (readRegister(afe, 0x03) & interruptMask) != 0) {
    setFault(index + 1, "CONFIG_READBACK");
    return false;
  }
  return true;
}

bool EcgPcb::beginCapture() {
  running = false;
  ++epoch;
  if (!epoch) {
    ++epoch;
  }
  fault = nullptr;
  faultAfe = 0;
  retryAfter = 0;
  txLost = 0;
  resetAfes();
  if (!waitForClient(100)) {
    return false;
  }
  for (size_t index = 0; index < AFE_COUNT; ++index) {
    if (!configureAfe(index)) {
      return false;
    }
  }
  if (!waitForClient(1000)) {
    return false;
  }
  for (const Afe &afe : afes) {
    readRegister(afe, 0x01); // Clear the earlier lock-transition status.
  }
  if (!waitForClient(20)) {
    return false;
  }
  for (size_t index = 0; index < AFE_COUNT; ++index) {
    afes[index].status = readRegister(afes[index], 0x01);
    if (afes[index].status & PLL_UNLOCK) {
      setFault(index + 1, "PLL_UNLOCK");
      return false;
    }
    if (!verifyAfe(index)) {
      return false;
    }
  }
  if (!clientActive()) {
    return false;
  }

  synchronizeAll();
  const int64_t launch = esp_timer_get_time();
  char line[LINE_CAPACITY];
  for (size_t index = 0; index < AFE_COUNT; ++index) {
    afes[index].lastDataTime = launch;
    // Keep E2 polling anchors until alignment is verified on the PCB.
    snprintf(line, sizeof(line), "E2,C,%lu,%lu,%u,1,%06lX,%lld,1,%lu,1\n",
             static_cast<unsigned long>(session),
             static_cast<unsigned long>(epoch), unsigned(index + 1),
             static_cast<unsigned long>(afes[index].id),
             static_cast<long long>(launch),
             static_cast<unsigned long>(ECG_LATENCY_TICKS));
    enqueueLine(line);
  }
  snprintf(line, sizeof(line), "E2,R,%lu,%lu,1\n",
           static_cast<unsigned long>(session),
           static_cast<unsigned long>(epoch));
  enqueueLine(line);
  lastStatus = esp_timer_get_time();
  running = !fault;
  return running;
}

void EcgPcb::drainFifo(size_t index, int64_t anchorTime) {
  Afe &afe = afes[index];
  char line[LINE_CAPACITY];
  size_t used = snprintf(line, sizeof(line), "E2,D,%lu,%lu,%u,%llu,",
                         static_cast<unsigned long>(session),
                         static_cast<unsigned long>(epoch), unsigned(index + 1),
                         static_cast<unsigned long long>(afe.sequence));
  for (size_t entries = 0; entries < FIFO_THRESHOLD; ++entries) {
    const FifoEntry entry = decodeFifo(readRegister(afe, 0x21));
    if (entry.overflow || entry.reserved) {
      setFault(index + 1, entry.overflow ? "FIFO_OVERFLOW" : "RESERVED_TAG");
      return;
    }
    if (entry.empty || (entry.last && entries + 1 < FIFO_THRESHOLD)) {
      setFault(index + 1, "FIFO_THRESHOLD_COUNT");
      return;
    }
    used += snprintf(line + used, sizeof(line) - used, "%s%ld:%u",
                     entries ? ";" : "", static_cast<long>(entry.value),
                     unsigned(entry.tag));
  }
  afe.sequence += FIFO_THRESHOLD;
  afe.lastDataTime = esp_timer_get_time();
  line[used++] = '\n';
  line[used] = 0;
  enqueueLine(line);

  if (!fault && anchorTime >= 0) {
    snprintf(line, sizeof(line), "E2,A,%lu,%lu,%u,%llu,%lld\n",
             static_cast<unsigned long>(session),
             static_cast<unsigned long>(epoch), unsigned(index + 1),
             static_cast<unsigned long long>(afe.sequence),
             static_cast<long long>(anchorTime));
    enqueueLine(line);
  }
}

void EcgPcb::serviceAfes() {
  int64_t anchorTimes[AFE_COUNT];
  // Probe all three before reading data so FIFO drains do not widen a bracket.
  for (size_t index = 0; index < AFE_COUNT; ++index) {
    Afe &afe = afes[index];
    const int64_t started = esp_timer_get_time();
    afe.status = readRegister(afe, 0x01);
    const int64_t finished = esp_timer_get_time();
    if (afe.status & FIFO_OVERFLOW) {
      setFault(index + 1, "FIFO_OVERFLOW");
      return;
    }
    if (afe.status & PLL_UNLOCK) {
      setFault(index + 1, "PLL_UNLOCK");
      return;
    }
    if (finished - afe.lastDataTime > 1000000) {
      setFault(index + 1, "DATA_STALE");
      return;
    }
    anchorTimes[index] =
        afe.threshold.observe(afe.status & FIFO_READY, started, finished);
  }
  for (size_t index = 0; index < AFE_COUNT && !fault; ++index) {
    if (afes[index].status & FIFO_READY) {
      drainFifo(index, anchorTimes[index]);
    }
  }
}

void EcgPcb::setFault(uint8_t afe, const char *code) {
  if (!fault) {
    fault = code;
    faultAfe = afe;
  }
}

void EcgPcb::handleFault(bool retry) {
  const char *code = fault ? fault : "RESET";
  const uint8_t afe = faultAfe;
  running = false;
  resetAfes();
  discardQueuedData();
  fault = nullptr;
  faultAfe = 0;
  emitEvent(afe, code);
  for (size_t index = 0; index < AFE_COUNT; ++index) {
    emitStatus(index);
  }
  fault = nullptr;
  retryAfter = retry ? esp_timer_get_time() + 1000000 : 0;
}

void EcgPcb::enqueueLine(const char *text) {
  const size_t next = (txHead + 1) % TX_CAPACITY;
  const size_t length = strlen(text);
  if (next == txTail || length >= LINE_CAPACITY) {
    ++txLost;
    setFault(0, "TX_QUEUE_OVERFLOW");
    return;
  }
  TxLine &line = txLines[txHead];
  memcpy(line.text, text, length);
  line.length = length;
  line.offset = 0;
  txHead = next;
}

size_t EcgPcb::copyPending(uint8_t *destination, size_t capacity) const {
  size_t copied = 0;
  for (size_t index = txTail; index != txHead && copied < capacity;
       index = (index + 1) % TX_CAPACITY) {
    const TxLine &line = txLines[index];
    const size_t length =
        min<size_t>(line.length - line.offset, capacity - copied);
    memcpy(destination + copied, line.text + line.offset, length);
    copied += length;
  }
  return copied;
}

void EcgPcb::consume(size_t count) {
  while (txTail != txHead && count) {
    TxLine &line = txLines[txTail];
    const size_t length = min<size_t>(line.length - line.offset, count);
    count -= length;
    line.offset += length;
    if (line.offset == line.length) {
      txTail = (txTail + 1) % TX_CAPACITY;
    }
  }
}

void EcgPcb::discardQueuedData() {
  // Complete the last partially transmitted record before sending any event.
  if (txTail != txHead && txLines[txTail].offset) {
    txHead = (txTail + 1) % TX_CAPACITY;
  } else {
    txHead = txTail;
  }
}

void EcgPcb::emitEvent(uint8_t afe, const char *code) {
  char line[LINE_CAPACITY];
  snprintf(line, sizeof(line), "E2,E,%lu,%lu,%u,%s,%lld\n",
           static_cast<unsigned long>(session),
           static_cast<unsigned long>(epoch), unsigned(afe), code,
           static_cast<long long>(esp_timer_get_time()));
  enqueueLine(line);
}

void EcgPcb::emitStatus(size_t index) {
  char line[LINE_CAPACITY];
  snprintf(line, sizeof(line), "E2,S,%lu,%lu,%u,%06lX,0,0,%lu\n",
           static_cast<unsigned long>(session),
           static_cast<unsigned long>(epoch), unsigned(index + 1),
           static_cast<unsigned long>(afes[index].status),
           static_cast<unsigned long>(txLost));
  enqueueLine(line);
}
