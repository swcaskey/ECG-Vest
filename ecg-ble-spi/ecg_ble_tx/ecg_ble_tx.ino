#include <SPI.h>
#include <esp_system.h>
#include <esp_timer.h>

#include "ble_link.h"
#include "firmware_decoder.h"

// On a BLE start command, read the ECG PCB and send E2 records.
// Nano default SPI: D13 SCK, D11 COPI, D12 CIPO.
constexpr size_t BOARD_COUNT = 3;
constexpr size_t TX_CAPACITY = 48;
constexpr size_t LINE_CAPACITY = 320;
constexpr uint32_t CNFG_GEN_VALUE = 0x080004; // ECG only; 100 MΩ bias setting.
// Optional internal bias in electrode mode. Set only for a verified bias
// circuit. Bit 1 enables ECGP bias and bit 0 enables ECGN bias on the
// corresponding AFE.
constexpr uint8_t ELECTRODE_BIAS_PINS[BOARD_COUNT] = {0, 0, 0};
constexpr uint32_t ECG_LATENCY_TICKS =
    4906; // ECG filter latency at 128 sps with LPF.

constexpr uint32_t CNFG_ECG_VALUE =
    0x805000; // 128 sps, gain 20, HP/LP enabled.
// EINT in STATUS is set while there are eight or more unread ECG samples.
constexpr uint32_t FIFO_READY = 1UL << 23;
constexpr uint32_t FIFO_OVERFLOW = 1UL << 22;
constexpr uint32_t PLL_UNLOCK = 1UL << 8;
constexpr uint32_t MNGR_INT_VALUE = 0x380004; // EFIT = 8.
const SPISettings MAX_SPI(1000000, MSBFIRST, SPI_MODE0);

struct Board {
  uint8_t cs;
  uint32_t id = 0;
  uint32_t status = 0;
  uint64_t sequence = 0;
  int64_t launchTime = 0;
  int64_t lastDataTime = 0;
  FifoThresholdBracket threshold;

  explicit Board(uint8_t csPin) : cs(csPin) {}
};

struct TxLine {
  char text[LINE_CAPACITY];
  uint16_t length = 0;
  uint16_t offset = 0;
};

// AFE1, AFE2 and AFE3 chip selects on the fabricated PCB.
Board boards[BOARD_COUNT] = {Board(D2), Board(D7), Board(D8)};
TxLine txLines[TX_CAPACITY];
uint16_t txHead = 0;
uint16_t txTail = 0;
uint32_t txLost = 0;
uint32_t session = 0;
uint32_t epoch = 0;
bool running = false;
int64_t lastStatus = 0;
int64_t retryAfter = 0;
const char *fault = nullptr;
uint8_t faultBoard = 0;

uint32_t generalConfig(size_t index) {
  const uint8_t bias = ELECTRODE_BIAS_PINS[index];
  return CNFG_GEN_VALUE | (bias ? 0x10 | bias : 0);
}

uint32_t readRegister(const Board &board, uint8_t address) {
  SPI.beginTransaction(MAX_SPI);
  digitalWrite(board.cs, LOW);
  SPI.transfer((address << 1) | 1);
  uint32_t value = uint32_t(SPI.transfer(0)) << 16;
  value |= uint32_t(SPI.transfer(0)) << 8;
  value |= SPI.transfer(0);
  digitalWrite(board.cs, HIGH);
  SPI.endTransaction();
  return value;
}

void writeRegister(const Board &board, uint8_t address, uint32_t value) {
  SPI.beginTransaction(MAX_SPI);
  digitalWrite(board.cs, LOW);
  SPI.transfer(address << 1);
  SPI.transfer((value >> 16) & 0xFF);
  SPI.transfer((value >> 8) & 0xFF);
  SPI.transfer(value & 0xFF);
  digitalWrite(board.cs, HIGH);
  SPI.endTransaction();
}

bool waitForBleClient(uint32_t milliseconds) {
  for (uint32_t elapsed = 0; elapsed < milliseconds; elapsed += 10) {
    if (!bleClientActive()) {
      return false;
    }
    delay(milliseconds - elapsed < 10 ? milliseconds - elapsed : 10);
  }
  return bleClientActive();
}

void setFault(uint8_t board, const char *code) {
  if (!fault) {
    fault = code;
    faultBoard = board;
  }
}

bool enqueueLine(const char *text) {
  const uint16_t next = (txHead + 1) % TX_CAPACITY;
  const size_t length = strlen(text);
  if (next == txTail || length >= LINE_CAPACITY) {
    ++txLost;
    setFault(0, "TX_QUEUE_OVERFLOW");
    return false;
  }
  TxLine &line = txLines[txHead];
  memcpy(line.text, text, length);
  line.length = length;
  line.offset = 0;
  txHead = next;
  return true;
}

void pumpBle() {
  for (size_t i = 0; i < 4 && txTail != txHead; ++i) {
    const BleLink current = getBleLink();
    const uint16_t capacity = bleSendCapacity(current);
    if (!capacity)
      return;

    uint8_t packet[BLE_HEADER + BLE_BODY];
    uint16_t tail = txTail;
    uint16_t offset = txLines[tail].offset;
    uint16_t used = 0;
    while (tail != txHead && used < capacity) {
      const TxLine &line = txLines[tail];
      const uint16_t count =
          min<uint16_t>(line.length - offset, capacity - used);
      memcpy(packet + BLE_HEADER + used, line.text + offset, count);
      used += count;
      offset += count;
      if (offset == line.length) {
        tail = (tail + 1) % TX_CAPACITY;
        offset = 0;
      }
    }
    // Keep the queue intact until the BLE stack accepts this packet.
    if (!notifyBle(current, packet, used))
      return;
    txTail = tail;
    if (txTail != txHead)
      txLines[txTail].offset = offset;
  }
}

void discardQueuedData() {
  // Finish partial lines before discarding queued data.
  if (txTail != txHead && txLines[txTail].offset != 0) {
    txHead = (txTail + 1) % TX_CAPACITY;
  } else {
    txHead = txTail;
  }
}

void emitEvent(uint8_t board, const char *code) {
  char line[LINE_CAPACITY];
  snprintf(line, sizeof(line), "E2,E,%lu,%lu,%u,%s,%lld\n",
           static_cast<unsigned long>(session),
           static_cast<unsigned long>(epoch), board, code,
           static_cast<long long>(esp_timer_get_time()));
  enqueueLine(line);
}

bool configureBoard(size_t index) {
  Board &board = boards[index];
  readRegister(board, 0x0F);
  board.id = readRegister(board, 0x0F);
  const uint32_t part = board.id & 0xF03000UL;
  if (part != 0x501000UL && part != 0x503000UL) {
    setFault(index + 1, "CHIP_ID");
    return false;
  }
  writeRegister(board, 0x02, 0); // INTB unused.
  writeRegister(board, 0x03, 0); // INT2B unused.
  writeRegister(board, 0x04, MNGR_INT_VALUE);
  writeRegister(board, 0x10, generalConfig(index));
  writeRegister(board, 0x12, 0); // Disable calibration.
  writeRegister(board, 0x14, 0); // Connect electrode inputs.
  writeRegister(board, 0x15, CNFG_ECG_VALUE);
  board.sequence = 0;
  board.threshold.reset();
  return true;
}

bool verifyBoard(size_t index) {
  const Board &board = boards[index];
  // The two AFEs share these ECG settings and have different reserved bits.
  const bool max30003 = (board.id & 0xF03000UL) == 0x503000UL;
  const uint32_t generalMask = max30003 ? 0xF83FFF : 0xFEFFFF;
  const uint32_t managerMask = max30003 ? 0xF80077 : 0xFF007F;
  const uint32_t interruptMask = max30003 ? 0xF00F03 : 0xFFFF03;
  if ((readRegister(board, 0x10) & generalMask) != generalConfig(index) ||
      (readRegister(board, 0x14) & 0xBF0000) != 0 ||
      (readRegister(board, 0x12) & 0x707FFF) != 0 ||
      (readRegister(board, 0x15) & 0xC37000) != CNFG_ECG_VALUE ||
      (readRegister(board, 0x04) & managerMask) != MNGR_INT_VALUE ||
      (readRegister(board, 0x02) & interruptMask) != 0 ||
      (readRegister(board, 0x03) & interruptMask) != 0) {
    setFault(index + 1, "CONFIG_READBACK");
    return false;
  }
  return true;
}

bool startAcquisition() {
  running = false;
  ++epoch;
  fault = nullptr;
  faultBoard = 0;
  retryAfter = 0;
  txLost =
      0; // Loss counters belong to the new epoch; prior loss has an E record.
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    writeRegister(boards[i], 0x08, 0);
  }
  if (!waitForBleClient(100)) {
    return false;
  }
  bool configured = true;
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    if (!configureBoard(i)) {
      configured = false;
    }
  }
  if (!configured) {
    return false;
  }
  if (!waitForBleClient(1000)) {
    return false;
  }
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    readRegister(boards[i], 0x01);
  }
  if (!waitForBleClient(20)) {
    return false;
  }
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    boards[i].status = readRegister(boards[i], 0x01);
    if (boards[i].status & PLL_UNLOCK) {
      setFault(i + 1, "PLL_UNLOCK");
      return false;
    }
    if (!verifyBoard(i)) {
      return false;
    }
  }

  if (!bleClientActive()) {
    return false;
  }

  // Select one chip at a time and record its launch on the common MCU clock.
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    writeRegister(boards[i], 0x09, 0);
    boards[i].launchTime = boards[i].lastDataTime = esp_timer_get_time();
  }
  char line[LINE_CAPACITY];
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    // Flag 1 identifies a capture timed by STATUS polling.
    snprintf(line, sizeof(line), "E2,C,%lu,%lu,%u,1,%06lX,%lld,1,%lu,1\n",
             static_cast<unsigned long>(session),
             static_cast<unsigned long>(epoch), unsigned(i + 1),
             static_cast<unsigned long>(boards[i].id),
             static_cast<long long>(boards[i].launchTime),
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

void drainFifo(size_t index, int64_t anchorTime) {
  Board &board = boards[index];
  char line[LINE_CAPACITY];
  size_t used = snprintf(
      line, sizeof(line), "E2,D,%lu,%lu,%u,%llu,",
      static_cast<unsigned long>(session), static_cast<unsigned long>(epoch),
      unsigned(index + 1), static_cast<unsigned long long>(board.sequence));
  for (size_t entries = 0; entries < FIFO_THRESHOLD; ++entries) {
    const FifoEntry entry = decodeFifo(readRegister(board, 0x21));
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
                     entry.tag);
  }
  board.sequence += FIFO_THRESHOLD;
  board.lastDataTime = esp_timer_get_time();
  line[used++] = '\n';
  line[used] = 0;
  enqueueLine(line);

  if (!fault && anchorTime >= 0) {
    snprintf(line, sizeof(line), "E2,A,%lu,%lu,%u,%llu,%lld\n",
             static_cast<unsigned long>(session),
             static_cast<unsigned long>(epoch), unsigned(index + 1),
             static_cast<unsigned long long>(board.sequence),
             static_cast<long long>(anchorTime));
    enqueueLine(line);
  }
}

void serviceBoards() {
  int64_t anchorTimes[BOARD_COUNT];
  // Sample every STATUS before draining any FIFO. FIFO reads take longer than
  // a STATUS probe and could widen the timing interval for the next chip.
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    Board &board = boards[i];
    const int64_t started = esp_timer_get_time();
    board.status = readRegister(board, 0x01);
    const int64_t finished = esp_timer_get_time();
    if (board.status & FIFO_OVERFLOW) {
      setFault(i + 1, "FIFO_OVERFLOW");
      return;
    }
    if (board.status & PLL_UNLOCK) {
      setFault(i + 1, "PLL_UNLOCK");
      return;
    }
    if (finished - board.lastDataTime > 1000000) {
      setFault(i + 1, "DATA_STALE");
      return;
    }
    anchorTimes[i] =
        board.threshold.observe(board.status & FIFO_READY, started, finished);
  }
  for (size_t i = 0; i < BOARD_COUNT && !fault; ++i) {
    if (boards[i].status & FIFO_READY) {
      drainFifo(i, anchorTimes[i]);
    }
  }
}

void emitStatus(size_t index) {
  char line[LINE_CAPACITY];
  snprintf(line, sizeof(line), "E2,S,%lu,%lu,%u,%06lX,0,0,%lu\n",
           static_cast<unsigned long>(session),
           static_cast<unsigned long>(epoch), unsigned(index + 1),
           static_cast<unsigned long>(boards[index].status),
           static_cast<unsigned long>(txLost));
  enqueueLine(line);
}

void handleFault(bool retry) {
  const char *code = fault;
  const uint8_t board = faultBoard;
  running = false;
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    writeRegister(boards[i], 0x08,
                  0); // Stop all channels after losing continuity.
  }
  discardQueuedData();
  fault = nullptr;
  faultBoard = 0;
  emitEvent(board, code ? code : "RESET");
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    emitStatus(i);
  }
  fault = nullptr;
  retryAfter = retry ? esp_timer_get_time() + 1000000 : 0;
}

void setup() {
  session = esp_random();
  if (!session) {
    session = 1;
  }
  // All CS lines must be high before SPI begins (shared MISO).
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    pinMode(boards[i].cs, OUTPUT);
    digitalWrite(boards[i].cs, HIGH);
  }
  SPI.begin(SCK, MISO, MOSI, -1); // Separate software chip selects.
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    writeRegister(boards[i], 0x08, 0);
  }
  setupBle();
}

void loop() {
  if (takeBleChange()) {
    running = false;
    retryAfter = 0;
    fault = nullptr;
    txHead = txTail = 0;
    packetNumber = 0;
    for (size_t i = 0; i < BOARD_COUNT; ++i) {
      writeRegister(boards[i], 0x08, 0);
    }
    if (!getBleLink().connected) {
      BLEDevice::startAdvertising();
    }
  }
  if (!bleClientActive()) {
    delay(1);
    return;
  }
  if (takeStartRequest()) {
    discardQueuedData();
    emitEvent(0, "MODE_CHANGE");
    if (!startAcquisition()) {
      handleFault(false);
    }
  }
  if (running) {
    serviceBoards();
    const int64_t now = esp_timer_get_time();
    if (!fault && now - lastStatus >= 1000000) {
      lastStatus = now;
      for (size_t i = 0; i < BOARD_COUNT; ++i) {
        emitStatus(i);
      }
    }
    if (fault) {
      handleFault(true);
    }
  } else if (retryAfter && esp_timer_get_time() >= retryAfter &&
             txTail == txHead) {
    if (!startAcquisition()) {
      handleFault(false);
    }
  }
  pumpBle();
  delay(1);
}
