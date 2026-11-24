#pragma once

#include "firmware_decoder.h"
#include <Arduino.h>

class EcgPcb {
public:
  using ClientActive = bool (*)();
  explicit EcgPcb(ClientActive clientActive) : clientActive(clientActive) {}

  void begin();
  void stop();
  void start();
  void service();
  void reportInvalidCommand();

  // The transport advances the queue only by the number of accepted bytes.
  size_t copyPending(uint8_t *destination, size_t capacity) const;
  void consume(size_t count);

private:
  static constexpr size_t AFE_COUNT = 3;
  static constexpr size_t TX_CAPACITY = 48;
  static constexpr size_t LINE_CAPACITY = 320;

  struct Afe {
    explicit Afe(uint8_t chipSelect) : cs(chipSelect) {}
    uint8_t cs;
    uint32_t id = 0;
    uint32_t status = 0;
    uint64_t sequence = 0;
    int64_t lastDataTime = 0;
    FifoThresholdBracket threshold;
  };

  struct TxLine {
    char text[LINE_CAPACITY];
    uint16_t length = 0;
    uint16_t offset = 0;
  };

  bool waitForClient(uint32_t milliseconds) const;
  uint32_t readRegister(const Afe &afe, uint8_t address) const;
  void writeRegister(const Afe &afe, uint8_t address, uint32_t value) const;
  void synchronizeAll() const;
  void resetAfes() const;
  bool configureAfe(size_t index);
  bool verifyAfe(size_t index);
  bool beginCapture();
  void drainFifo(size_t index, int64_t anchorTime);
  void serviceAfes();
  void setFault(uint8_t afe, const char *code);
  void handleFault(bool retry);
  void enqueueLine(const char *text);
  void discardQueuedData();
  void emitEvent(uint8_t afe, const char *code);
  void emitStatus(size_t index);

  ClientActive clientActive;
  Afe afes[AFE_COUNT] = {Afe(D2), Afe(D7), Afe(D8)};
  TxLine txLines[TX_CAPACITY];
  size_t txHead = 0;
  size_t txTail = 0;
  uint32_t txLost = 0;
  uint32_t session = 0;
  uint32_t epoch = 0;
  bool running = false;
  int64_t lastStatus = 0;
  int64_t lastInvalidCommand = -1000000;
  int64_t retryAfter = 0;
  const char *fault = nullptr;
  uint8_t faultAfe = 0;
};
