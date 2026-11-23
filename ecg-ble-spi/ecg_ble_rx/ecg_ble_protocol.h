#pragma once
#include <stdint.h>
#include <string.h>

// Keep these wire settings identical in both sketches.
constexpr char ECG_SERVICE[] = "c0adc310-1f4a-4e8c-8c08-661960eeb030";
constexpr char ECG_DATA[] = "c0adc311-1f4a-4e8c-8c08-661960eeb030";
constexpr char ECG_COMMAND[] = "c0adc312-1f4a-4e8c-8c08-661960eeb030";
constexpr uint16_t ECG_MTU = 247;
constexpr uint16_t BLE_HEADER = 4; // Little-endian packet number.
constexpr uint16_t BLE_BODY = ECG_MTU - 3 - BLE_HEADER;

inline void writePacketNumber(uint8_t *bytes, uint32_t sequence) {
  for (uint8_t i = 0; i < BLE_HEADER; ++i) {
    bytes[i] = uint8_t(sequence >> (8 * i));
  }
}

inline uint32_t readPacketNumber(const uint8_t *bytes) {
  uint32_t sequence = 0;
  for (uint8_t i = 0; i < BLE_HEADER; ++i) {
    sequence |= uint32_t(bytes[i]) << (8 * i);
  }
  return sequence;
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
