#include <SPI.h>
#include <esp_system.h>
#include <esp_timer.h>

#include "firmware_decoder.h"

// USB mode for both boards: 0 = internal test, 1 = external inputs.
// Shared SPI: D13 SCK, D11 MOSI, D12 MISO; common 3V3/GND.
// Independent clocks: SAMP-to-FIFO timing still needs bench validation.
enum class InputMode { InternalTest, Electrodes };

constexpr size_t BOARD_COUNT = 2;
constexpr size_t IRQ_CAPACITY = 128;
constexpr size_t TX_CAPACITY = 32;
constexpr size_t LINE_CAPACITY = 320;
constexpr uint32_t BAUD_RATE = 115200;
constexpr uint32_t CNFG_GEN_VALUE =
    0x080004; // ECG on; lead bias/off detection off.
constexpr uint32_t CNFG_ECG_VALUE =
    0x805000; // 128 sps, gain 20, HP/LP enabled.
// Rev 3 pp. 27-29: dedicated active-low SAMP, self-clear, every base instant.
// EINT/EOVF/PLL are polled: a persistent interrupt must not hide timing edges.
constexpr uint32_t EN_INT_VALUE =
    0x000201; // CMOS drive on each dedicated wire.
constexpr uint32_t MNGR_INT_VALUE = 0x380004; // EFIT = 8, CLR_SAMP = 1.
const SPISettings MAX_SPI(1000000, MSBFIRST, SPI_MODE0);

struct PulseEvent {
  uint64_t count;
  int64_t time;
};

struct TimingCapture {
  PulseEvent events[IRQ_CAPACITY];
  volatile uint16_t head = 0;
  volatile uint16_t tail = 0;
  volatile uint64_t count = 0;
  volatile uint32_t lost = 0;
  volatile int64_t last = 0;
  volatile bool armed = false;
};

struct Board {
  uint8_t cs;
  uint8_t interrupt;
  uint32_t id = 0;
  uint32_t status = 0;
  uint64_t sequence = 0;
  uint64_t lastPulseCount = 0;
  int64_t lastPulseTime = 0;
  int64_t lastInterval = 0;
  int64_t lastDataTime = 0;
  double pulsePeriod = 0;
  int64_t startupIntervals[16] = {};
  uint8_t startupCount = 0;

  Board(uint8_t csPin, uint8_t interruptPin)
      : cs(csPin), interrupt(interruptPin) {}
};

struct TxLine {
  char text[LINE_CAPACITY];
  uint16_t length = 0;
  uint16_t offset = 0;
};

// Pins are {CS, INTB}: Board 1 = Lead I; Board 2 = Lead III.
Board boards[BOARD_COUNT] = {{D10, D2}, {D9, D3}};
TimingCapture captures[BOARD_COUNT];
portMUX_TYPE captureMux = portMUX_INITIALIZER_UNLOCKED;
TxLine txLines[TX_CAPACITY];
uint16_t txHead = 0;
uint16_t txTail = 0;
uint32_t txLost = 0;
uint32_t session = 0;
uint32_t epoch = 0;
bool running = false;
bool serialWasConnected = false;
InputMode inputMode = InputMode::InternalTest;
int64_t launchTime = 0;
int64_t lastPoll = 0;
int64_t lastStatus = 0;
int64_t retryAfter = 0;
const char *fault = nullptr;
char timingFault[48];
uint8_t faultBoard = 0;

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

void ARDUINO_ISR_ATTR capturePulse(void *argument) {
  const size_t index = reinterpret_cast<uintptr_t>(argument) - 1;
  // Common MCU time; includes ISR latency, not an exact sample timestamp.
  const int64_t now = esp_timer_get_time();
  portENTER_CRITICAL_ISR(&captureMux);
  TimingCapture &capture = captures[index];
  if (capture.armed) {
    const uint64_t count = ++capture.count;
    capture.last = now;
    const uint16_t next = (capture.head + 1) % IRQ_CAPACITY;
    if (next == capture.tail) {
      ++capture.lost;
    } else {
      capture.events[capture.head].count = count;
      capture.events[capture.head].time = now;
      capture.head = next;
    }
  }
  portEXIT_CRITICAL_ISR(&captureMux);
}

bool popPulse(size_t index, PulseEvent &event) {
  bool available = false;
  portENTER_CRITICAL(&captureMux);
  TimingCapture &capture = captures[index];
  if (capture.tail != capture.head) {
    event = capture.events[capture.tail];
    capture.tail = (capture.tail + 1) % IRQ_CAPACITY;
    available = true;
  }
  portEXIT_CRITICAL(&captureMux);
  return available;
}

void disarmTiming() {
  portENTER_CRITICAL(&captureMux);
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    captures[i].armed = false;
  }
  portEXIT_CRITICAL(&captureMux);
}

void setFault(uint8_t board, const char *code) {
  if (!fault) {
    fault = code;
    faultBoard = board;
  }
}

void setTimingFault(size_t index, const char *code, int64_t interval) {
  if (!fault) {
    // D=current, P=previous, R=reference interval, all in microseconds.
    snprintf(timingFault, sizeof(timingFault), "%s_D%lld_P%lld_R%ld", code,
             static_cast<long long>(interval),
             static_cast<long long>(boards[index].lastInterval),
             static_cast<long>(boards[index].pulsePeriod));
    setFault(index + 1, timingFault);
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

void pumpSerial() {
  // Never block acquisition; a full TX queue triggers recovery.
  for (size_t i = 0; i < 4 && txTail != txHead; ++i) {
    if (!Serial) {
      return;
    }
    const int available = Serial.availableForWrite();
    if (available <= 0) {
      return;
    }
    TxLine &line = txLines[txTail];
    const size_t remaining = line.length - line.offset;
    const size_t amount = remaining < size_t(available) ? remaining : available;
    line.offset += Serial.write(
        reinterpret_cast<const uint8_t *>(line.text + line.offset), amount);
    if (line.offset == line.length) {
      txTail = (txTail + 1) % TX_CAPACITY;
    }
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

bool readInputMode(InputMode &mode) {
  static ModeCommand command;
  static int64_t lastInvalidMode = -1000000;
  for (size_t i = 0; i < 64 && Serial.available() > 0; ++i) {
    const int selection = command.feed(Serial.read());
    if (selection == -2) {
      const int64_t now = esp_timer_get_time();
      if (now - lastInvalidMode >= 1000000) {
        lastInvalidMode = now;
        emitEvent(0, "INVALID_MODE");
      }
    } else if (selection >= 0) {
      mode = selection == 0 ? InputMode::InternalTest : InputMode::Electrodes;
      return true;
    }
  }
  return false;
}

bool configureBoard(size_t index, InputMode mode) {
  Board &board = boards[index];
  const uint32_t emux = mode == InputMode::InternalTest ? 0x3B0000 : 0;
  const uint32_t calibration = mode == InputMode::InternalTest ? 0x704800 : 0;
  readRegister(board, 0x0F);
  board.id = readRegister(board, 0x0F);
  if ((board.id & 0xF03000UL) != 0x503000UL) {
    setFault(index + 1, "CHIP_ID");
    return false;
  }
  writeRegister(board, 0x02, 0); // Mask timing until both chips are configured.
  writeRegister(board, 0x03, 0); // INT2B unused; no board rework required.
  writeRegister(board, 0x04, MNGR_INT_VALUE);
  writeRegister(board, 0x10, CNFG_GEN_VALUE);
  writeRegister(board, 0x12, 0); // Disable calibration before routing changes.
  writeRegister(board, 0x14, emux);
  writeRegister(board, 0x12, calibration);
  writeRegister(board, 0x15, CNFG_ECG_VALUE);
  board.sequence = 0;
  board.lastPulseCount = 0;
  board.lastPulseTime = 0;
  board.lastInterval = 0;
  board.pulsePeriod = 0;
  board.startupCount = 0;
  return true;
}

bool verifyBoard(size_t index) {
  const Board &board = boards[index];
  const uint32_t emux = inputMode == InputMode::InternalTest ? 0x3B0000 : 0;
  const uint32_t calibration =
      inputMode == InputMode::InternalTest ? 0x704800 : 0;
  // Masks include documented fields only (MAX30003 Rev 3 pp. 28-37).
  if ((readRegister(board, 0x10) & 0xF83FFF) != CNFG_GEN_VALUE ||
      (readRegister(board, 0x14) & 0xBF0000) != emux ||
      (readRegister(board, 0x12) & 0x707FFF) != calibration ||
      (readRegister(board, 0x15) & 0xC37000) != CNFG_ECG_VALUE ||
      (readRegister(board, 0x04) & 0xF80077) != MNGR_INT_VALUE ||
      (readRegister(board, 0x02) & 0xF00F03) != EN_INT_VALUE ||
      (readRegister(board, 0x03) & 0xF00F03) != 0) {
    setFault(index + 1, "CONFIG_READBACK");
    return false;
  }
  return true;
}

bool startAcquisition(InputMode mode) {
  running = false;
  disarmTiming();
  inputMode = mode;
  ++epoch;
  fault = nullptr;
  faultBoard = 0;
  retryAfter = 0;
  txLost =
      0; // Loss counters belong to the new epoch; prior loss has an E record.
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    writeRegister(boards[i], 0x08, 0);
  }
  delay(100);
  bool configured = true;
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    if (!configureBoard(i, mode)) {
      configured = false;
    }
  }
  if (!configured) {
    return false;
  }
  delay(1000);
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    readRegister(boards[i], 0x01);
  }
  delay(20);
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    boards[i].status = readRegister(boards[i], 0x01);
    if (boards[i].status & (1UL << 8)) {
      setFault(i + 1, "PLL_UNLOCK");
      return false;
    }
    writeRegister(boards[i], 0x02, EN_INT_VALUE);
    if (!verifyBoard(i)) {
      return false;
    }
  }

  // Write-only common SYNCH: independent FCLKs still determine launch phase.
  SPI.beginTransaction(MAX_SPI);
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    digitalWrite(boards[i].cs, LOW);
  }
  SPI.transfer(0x09 << 1);
  SPI.transfer(0);
  SPI.transfer(0);
  SPI.transfer(0);
  launchTime = esp_timer_get_time(); // Launch observation, not sample zero.
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    digitalWrite(boards[i].cs, HIGH);
  }
  SPI.endTransaction();
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    boards[i].lastDataTime = launchTime;
  }
  portENTER_CRITICAL(&captureMux);
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    TimingCapture &capture = captures[i];
    capture.head = capture.tail = 0;
    capture.count = 0;
    capture.lost = 0;
    capture.last = 0;
    capture.armed = true;
  }
  portEXIT_CRITICAL(&captureMux);
  char line[LINE_CAPACITY];
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    // Bench anchors show 128 SAMP edges/s at 128 sps: ratio 1.
    // 4906 master-clock input ticks = LPF latency (Table 4, p. 19).
    // Association=0: first SAMP edge to FIFO index phase needs bench evidence.
    snprintf(line, sizeof(line), "E2,C,%lu,%lu,%u,%u,%06lX,%lld,1,4906,0\n",
             static_cast<unsigned long>(session),
             static_cast<unsigned long>(epoch), unsigned(i + 1),
             mode == InputMode::InternalTest ? 0 : 1,
             static_cast<unsigned long>(boards[i].id),
             static_cast<long long>(launchTime));
    enqueueLine(line);
  }
  snprintf(line, sizeof(line), "E2,R,%lu,%lu,%u\n",
           static_cast<unsigned long>(session),
           static_cast<unsigned long>(epoch),
           mode == InputMode::InternalTest ? 0 : 1);
  enqueueLine(line);
  lastPoll = lastStatus = launchTime;
  running = !fault;
  return running;
}

void processTiming(size_t index) {
  Board &board = boards[index];
  uint32_t lost;
  int64_t last;
  portENTER_CRITICAL(&captureMux);
  lost = captures[index].lost;
  last = captures[index].last;
  portEXIT_CRITICAL(&captureMux);
  if (lost) {
    setFault(index + 1, "IRQ_QUEUE_OVERFLOW");
    return;
  }
  const int64_t now = esp_timer_get_time();
  if (now - (last ? last : launchTime) > 100000) {
    setFault(index + 1, "TIMING_STALE");
    return;
  }
  PulseEvent event;
  for (size_t i = 0; i < 32 && popPulse(index, event); ++i) {
    if (board.lastPulseCount) {
      const int64_t interval = event.time - board.lastPulseTime;
      if (event.count != board.lastPulseCount + 1 || interval <= 0) {
        setFault(index + 1, "TIMING_COUNT");
        return;
      }
      // The actual SAMP rate is measured, not assumed to equal FIFO rate.
      if (board.startupCount < 16) {
        board.startupIntervals[board.startupCount++] = interval;
        if (board.startupCount == 16) {
          int64_t sorted[16];
          memcpy(sorted, board.startupIntervals, sizeof(sorted));
          for (size_t j = 1; j < 16; ++j) {
            const int64_t value = sorted[j];
            size_t k = j;
            while (k > 0 && sorted[k - 1] > value) {
              sorted[k] = sorted[k - 1];
              --k;
            }
            sorted[k] = value;
          }
          board.pulsePeriod = (sorted[7] + sorted[8]) / 2.0;
          if (sorted[0] <= board.pulsePeriod * 0.5 ||
              sorted[15] >= board.pulsePeriod * 1.7) {
            setFault(index + 1, "TIMING_STARTUP");
            return;
          }
        }
      } else if (interval >= board.pulsePeriod * 1.7) {
        setTimingFault(index, "TIMING_MISSED", interval);
        return;
      } else if (interval <= board.pulsePeriod * 0.5) {
        setTimingFault(index, "TIMING_GLITCH", interval);
        return;
      } else if (interval < board.pulsePeriod * 1.5) {
        board.pulsePeriod += (interval - board.pulsePeriod) / 16;
      }
      board.lastInterval = interval;
    }
    board.lastPulseCount = event.count;
    board.lastPulseTime = event.time;
    // Capture every edge locally, transmit 1/32 plus the first anchor.
    if (event.count == 1 || event.count % 32 == 0) {
      char line[LINE_CAPACITY];
      snprintf(line, sizeof(line), "E2,A,%lu,%lu,%u,%llu,%lld\n",
               static_cast<unsigned long>(session),
               static_cast<unsigned long>(epoch), unsigned(index + 1),
               static_cast<unsigned long long>(event.count),
               static_cast<long long>(event.time));
      enqueueLine(line);
    }
  }
}

void serviceBoard(size_t index) {
  Board &board = boards[index];
  if (esp_timer_get_time() - board.lastDataTime > 1000000) {
    // A transport watchdog, not an assumed SAMP-to-FIFO sample association.
    setFault(index + 1, "DATA_STALE");
    return;
  }
  board.status = readRegister(board, 0x01);
  if (board.status & (1UL << 8)) {
    setFault(index + 1, "PLL_UNLOCK");
    return;
  }
  if (board.status & (1UL << 22)) {
    setFault(index + 1, "FIFO_OVERFLOW");
    return;
  }
  if (!(board.status & (1UL << 23))) {
    return;
  }
  char line[LINE_CAPACITY];
  size_t used = snprintf(line, sizeof(line), "E2,D,%lu,%lu,%u,%llu,",
                         static_cast<unsigned long>(session),
                         static_cast<unsigned long>(epoch), unsigned(index + 1),
                         static_cast<unsigned long long>(board.sequence));
  size_t entries = 0;
  for (size_t i = 0; i < 8; ++i) {
    const FifoEntry entry = decodeFifo(readRegister(board, 0x21));
    if (entry.empty) {
      break;
    }
    if (entry.overflow || entry.reserved) {
      setFault(index + 1, entry.overflow ? "FIFO_OVERFLOW" : "RESERVED_TAG");
      return;
    }
    used +=
        snprintf(line + used, sizeof(line) - used, "%s%ld:%u",
                 entries ? ";" : "", static_cast<long>(entry.value), entry.tag);
    ++board.sequence; // Settling tags advance time, even with invalid voltage.
    ++entries;
    if (entry.last) {
      break;
    }
  }
  if (entries) {
    board.lastDataTime = esp_timer_get_time();
    line[used++] = '\n';
    line[used] = 0;
    enqueueLine(line);
  }
}

void emitStatus(size_t index) {
  uint64_t count;
  uint32_t lost;
  portENTER_CRITICAL(&captureMux);
  count = captures[index].count;
  lost = captures[index].lost;
  portEXIT_CRITICAL(&captureMux);
  char line[LINE_CAPACITY];
  snprintf(
      line, sizeof(line), "E2,S,%lu,%lu,%u,%06lX,%llu,%lu,%lu\n",
      static_cast<unsigned long>(session), static_cast<unsigned long>(epoch),
      unsigned(index + 1), static_cast<unsigned long>(boards[index].status),
      static_cast<unsigned long long>(count), static_cast<unsigned long>(lost),
      static_cast<unsigned long>(txLost));
  enqueueLine(line);
}

void handleFault(bool retry) {
  const char *code = fault;
  const uint8_t board = faultBoard;
  running = false;
  disarmTiming();
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    writeRegister(boards[i], 0x08, 0); // Stop both; continuity is now unknown.
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
  Serial.begin(BAUD_RATE);
  // Native USB CDC in the pinned Nano package; never wait for its TX mutex.
  Serial.setTxTimeoutMs(0);
  session = esp_random();
  if (!session) {
    session = 1;
  }
  // Both CS lines must be high before SPI begins (shared MISO).
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    pinMode(boards[i].cs, OUTPUT);
    digitalWrite(boards[i].cs, HIGH);
    pinMode(boards[i].interrupt, INPUT_PULLUP);
  }
  SPI.begin();
  for (size_t i = 0; i < BOARD_COUNT; ++i) {
    writeRegister(boards[i], 0x08, 0);
    attachInterruptArg(boards[i].interrupt, capturePulse,
                       reinterpret_cast<void *>(i + 1), FALLING);
  }
  while (!Serial) {
    delay(10);
  }
  // Human-readable menu is intentionally outside the E2 record grammar.
  Serial.println("Select input: 0 = Internal test, 1 = Electrodes; then Enter");
}

void loop() {
  if (!Serial) {
    if (serialWasConnected) {
      disarmTiming();
      running = false;
      retryAfter = 0;
      fault = nullptr;
      for (size_t i = 0; i < BOARD_COUNT; ++i) {
        writeRegister(boards[i], 0x08, 0);
      }
      // A new USB client must select a mode and start a fresh epoch.
      txHead = txTail = 0;
    }
    serialWasConnected = false;
    delay(1);
    return;
  }
  serialWasConnected = true;
  pumpSerial();
  InputMode requested;
  if (readInputMode(requested)) {
    disarmTiming();
    discardQueuedData();
    emitEvent(0, "MODE_CHANGE");
    if (!startAcquisition(requested)) {
      handleFault(false);
    }
  }
  if (running) {
    for (size_t i = 0; i < BOARD_COUNT; ++i) {
      processTiming(i);
    }
    const int64_t now = esp_timer_get_time();
    if (!fault && now - lastPoll >= 4000) {
      lastPoll = now;
      for (size_t i = 0; i < BOARD_COUNT && !fault; ++i) {
        serviceBoard(i);
      }
    }
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
    if (!startAcquisition(inputMode)) {
      handleFault(false);
    }
  }
  pumpSerial();
  delay(1);
}
