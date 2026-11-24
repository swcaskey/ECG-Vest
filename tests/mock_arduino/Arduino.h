#pragma once

#include <algorithm>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

using std::min;
constexpr uint8_t D2 = 2, D7 = 7, D8 = 8;
constexpr uint8_t SCK = 13, MISO = 12, MOSI = 11;
constexpr uint8_t HIGH = 1, LOW = 0, OUTPUT = 1;
void pinMode(uint8_t pin, uint8_t mode);
void digitalWrite(uint8_t pin, uint8_t value);
void delay(uint32_t milliseconds);
