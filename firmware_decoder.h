#ifndef ECG_FIRMWARE_DECODER_H
#define ECG_FIRMWARE_DECODER_H

#include <stdint.h>

// MAX30003 Rev 3, pp. 41-43: voltage bits 23:6; ETAG bits 5:3.
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

class ModeCommand {
public:
  // -1: incomplete/blank line; -2: invalid line; 0/1: valid mode choice.
  int feed(char c) {
    if (c == '\r' || c == '\n') {
      if (choice == 0 && !invalid) {
        return -1;
      }
      const int result = invalid ? -2 : choice - '0';
      choice = 0;
      invalid = false;
      return result;
    }
    if (c != ' ' && c != '\t') {
      if (choice == 0 && (c == '0' || c == '1')) {
        choice = c;
      } else {
        invalid = true;
      }
    }
    return -1;
  }

private:
  char choice = 0;
  bool invalid = false;
};

#endif
