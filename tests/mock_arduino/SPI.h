#pragma once

#include <stdint.h>

constexpr uint8_t MSBFIRST = 1, SPI_MODE0 = 0;
struct SPISettings {
  SPISettings(uint32_t, uint8_t, uint8_t) {}
};

struct SpiFixture {
  void begin(uint8_t, uint8_t, uint8_t, int8_t) {}
  void beginTransaction(const SPISettings &);
  uint8_t transfer(uint8_t byte);
  void endTransaction();
};

extern SpiFixture SPI;
