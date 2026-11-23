#ifndef ECG_FIRMWARE_DECODER_H
#define ECG_FIRMWARE_DECODER_H

#include <stdint.h>
#include <string.h>

constexpr uint8_t FIFO_THRESHOLD = 8;

constexpr int64_t MAX_THRESHOLD_WINDOW_US = 2000;

class FifoThresholdBracket {
public:
  void reset() { lastBelow = -1; }

  int64_t observe(bool above, int64_t started, int64_t finished) {
    if (!above) {
      // Use the start of the below-threshold transaction so the interval
      // contains the crossing regardless of when the AFE latches STATUS.
      lastBelow = started;
      return -1;
    }
    const int64_t lower = lastBelow;
    reset(); // Repeated high reads and backlog drains cannot create anchors.
    const int64_t width = finished - lower;
    if (lower < 0 || width <= 0 || width > MAX_THRESHOLD_WINDOW_US) {
      return -1;
    }
    return lower + width / 2;
  }

private:
  int64_t lastBelow = -1;
};

struct FifoEntry {
  int32_t value;
  uint8_t tag;
  bool advancesTime;
  bool voltageValid;
  bool last;
  bool empty;
  bool overflow;
  bool reserved;
};

// MAX30001: voltage bits 23:6, ECG tag bits 5:3.
inline FifoEntry decodeFifo(uint32_t word) {
  FifoEntry entry = {};
  entry.tag = (word >> 3) & 7;
  entry.value = (word >> 6) & 0x3FFFF;
  if (entry.value & 0x20000) {
    entry.value -= 0x40000;
  }
  entry.advancesTime = entry.tag <= 3;
  entry.voltageValid = entry.tag == 0 || entry.tag == 2;
  entry.last = entry.tag == 2 || entry.tag == 3;
  entry.empty = entry.tag == 6;
  entry.overflow = entry.tag == 7;
  entry.reserved = entry.tag == 4 || entry.tag == 5;
  return entry;
}

class StartCommand {
public:
  void reset() {
    length = 0;
    invalid = false;
    trailingSpace = false;
  }

  // -1: incomplete/blank line, -2: invalid line, 1: start capture.
  int feed(char c) {
    if (c == '\r' || c == '\n') {
      command[length] = '\0';
      const bool start = !invalid && (strcmp(command, "go") == 0 ||
                                      strcmp(command, "yes") == 0 ||
                                      strcmp(command, "y") == 0);
      const int result = start ? 1 : (length || invalid ? -2 : -1);
      reset();
      return result;
    }

    if (c == ' ' || c == '\t') {
      trailingSpace = length != 0;
      return -1;
    }
    if (c == '\0' || trailingSpace || length == sizeof(command) - 1) {
      invalid = true;
    } else {
      command[length++] = c >= 'A' && c <= 'Z' ? char(c - 'A' + 'a') : c;
    }
    return -1;
  }

private:
  char command[4] = {};
  uint8_t length = 0;
  bool invalid = false;
  bool trailingSpace = false;
};

#endif
