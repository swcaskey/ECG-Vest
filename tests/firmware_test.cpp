#include "EcgPcb.h"
#include "SPI.h"
#include "ecg_ble_protocol.h"
#include "start_command.h"

#include <assert.h>
#include <string>

SpiFixture SPI;
namespace {
constexpr uint8_t selects[] = {D2, D7, D8};
uint8_t pinLevels[16] = {};
uint32_t registers[3][32] = {};
uint32_t status[3] = {};
uint8_t lockChecked = 0;
uint8_t command, bytes, selected;
uint32_t word;
unsigned syncCount = 0;
int64_t now = 0;

bool active() { return true; }

std::string readStream(EcgPcb &pcb) {
  uint8_t buffer[17];
  std::string stream;
  size_t count;
  while ((count = pcb.copyPending(buffer, sizeof(buffer))) != 0) {
    stream.append(reinterpret_cast<const char *>(buffer), count);
    pcb.consume(count);
  }
  return stream;
}
} // namespace

void pinMode(uint8_t, uint8_t) {}
void digitalWrite(uint8_t pin, uint8_t value) { pinLevels[pin] = value; }
void delay(uint32_t milliseconds) { now += 1000 * milliseconds; }
int64_t esp_timer_get_time() { return now; }
uint32_t esp_random() { return 42; }

void SpiFixture::beginTransaction(const SPISettings &) {
  bytes = selected = 0;
  word = 0;
}

uint8_t SpiFixture::transfer(uint8_t byte) {
  uint8_t activeSelects = 0;
  for (unsigned i = 0; i < 3; ++i) {
    if (pinLevels[selects[i]] == LOW) {
      activeSelects |= 1 << i;
    }
  }
  if (++bytes == 1) {
    selected = activeSelects;
    command = byte;
    const uint8_t address = command >> 1;
    if (command == 0x12) {
      assert(selected == 7 && lockChecked == 7);
    } else {
      assert(selected == 1 || selected == 2 || selected == 4);
    }
    if (command & 1) {
      const unsigned i = selected == 1 ? 0 : selected == 2 ? 1 : 2;
      if (address == 0x01 && !(status[i] & 0x100)) {
        lockChecked |= selected;
      }
      word = address == 0x0f ? 0x513742
             : address == 1  ? status[i]
                             : registers[i][address];
    }
    return 0;
  }
  assert(activeSelects == selected && bytes <= 4);
  if (command & 1) {
    return uint8_t(word >> (8 * (4 - bytes)));
  }
  word = (word << 8) | byte;
  return 0;
}

void SpiFixture::endTransaction() {
  assert(bytes == 4);
  for (uint8_t pin : selects) {
    assert(pinLevels[pin] == HIGH);
  }
  const uint8_t address = command >> 1;
  if (command == 0x12) {
    assert(word == 0);
    ++syncCount;
  } else if (!(command & 1)) {
    const unsigned i = selected == 1 ? 0 : selected == 2 ? 1 : 2;
    if (address == 8) {
      lockChecked &= ~selected;
      memset(registers[i], 0, sizeof(registers[i]));
    } else {
      registers[i][address] = word;
    }
  }
}

int main() {
  // Check the real acquisition core with a simulated bus and readback.
  EcgPcb pcb(active);
  pcb.begin();
  status[2] = 0x100;
  pcb.start();
  assert(syncCount == 0);
  assert(readStream(pcb).find("E2,E,42,1,3,PLL_UNLOCK,") != std::string::npos);

  status[2] = 0;
  pcb.start();
  assert(syncCount == 1);
  const auto started = readStream(pcb);
  assert(started.find("E2,C,42,2,1,1,") != std::string::npos);
  assert(started.find("E2,C,42,2,2,1,") != std::string::npos);
  assert(started.find("E2,C,42,2,3,1,") != std::string::npos);
  assert(started.find("E2,R,42,2,1\n") != std::string::npos);

  status[1] = 0x400000;
  pcb.service();
  assert(readStream(pcb).find("E2,E,42,2,2,FIFO_OVERFLOW,") !=
         std::string::npos);
  status[1] = 0;
  delay(1000);
  pcb.service();
  assert(syncCount == 2);
  assert(readStream(pcb).find("E2,R,42,3,1\n") != std::string::npos);

  assert(decodeFifo(0x7fffc7).value == 131071);
  assert(decodeFifo(0x800000).value == -131072);
  assert(decodeFifo(0xffffc8).value == -1);
  assert(!decodeFifo(0xffffc8).voltageValid &&
         decodeFifo(0xffffc8).advancesTime);
  assert(decodeFifo(0x30).empty && decodeFifo(0x38).overflow);

  FifoThresholdBracket bracket;
  assert(bracket.observe(false, 100, 130) == -1);
  assert(bracket.observe(true, 1000, 1100) == 600);
  assert(bracket.observe(true, 1200, 1250) == -1);
  bracket.observe(false, 1300, 1350);
  assert(bracket.observe(true, 3400, 3450) == -1);

  StartCommand start;
  assert(start.feed('G') == -1 && start.feed('o') == -1);
  assert(start.feed('\r') == 1 && start.feed('\n') == -1);
  assert(start.feed('1') == -1 && start.feed('\n') == -2);
  uint8_t sequence[4];
  writePacketNumber(sequence, 0x12345678);
  assert(sequence[0] == 0x78 && sequence[3] == 0x12);
  assert(readPacketNumber(sequence) == 0x12345678);
}
