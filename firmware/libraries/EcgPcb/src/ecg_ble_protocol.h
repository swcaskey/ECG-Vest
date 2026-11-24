#pragma once
#include <stdint.h>

// Wireless framing shared by the transmitter and receiver.
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
