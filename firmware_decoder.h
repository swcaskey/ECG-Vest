#ifndef ECG_FIRMWARE_DECODER_H
#define ECG_FIRMWARE_DECODER_H

#include <stdint.h>
#include <string.h>

constexpr uint8_t FIFO_THRESHOLD = 8;

struct FifoThresholdEdge {
  uint64_t position; // One-based position of the threshold-crossing sample.
  bool duringDrain;
};

// Call under the same ISR/main lock; SPI itself stays outside that lock.
class FifoAnchorState {
public:
  void reset() {
    reads = 0;
    draining = false;
  }
  FifoThresholdEdge edge() const { return {reads + FIFO_THRESHOLD, draining}; }
  void beginDrain() { draining = true; }
  uint64_t completeRead() { return ++reads; }
  void endDrain() { draining = false; }
  bool isDraining() const { return draining; }

private:
  uint64_t reads = 0;
  bool draining = false;
};

inline bool fifoAnchorMatches(const FifoThresholdEdge &edge,
                              uint64_t firstUnread) {
  return !edge.duringDrain && edge.position == firstUnread + FIFO_THRESHOLD;
}

inline bool timingOnlyFault(const char *code) {
  return code && (strncmp(code, "TIMING_", 7) == 0 ||
                  strcmp(code, "IRQ_QUEUE_OVERFLOW") == 0);
}

struct TimingDiagnostic {
  uint64_t pulseCount;
  int64_t time;
  int64_t interval;
  uint64_t fifoReads;
  uint32_t irqLost;
};

class TimingContext {
public:
  void reset() {
    beforeCount = beforeHead = used = cursor = postLeft = 0;
    faulted = false;
  }

  void observe(const TimingDiagnostic &entry) {
    if (!faulted) {
      entries[beforeHead] = entry;
      beforeHead = (beforeHead + 1) % 4;
      if (beforeCount < 4) {
        ++beforeCount;
      }
    } else if (postLeft) {
      entries[used++] = entry;
      --postLeft;
    }
  }

  bool begin(const TimingDiagnostic *edge) {
    if (faulted) {
      return false;
    }
    TimingDiagnostic before[4] = {};
    for (uint8_t i = 0; i < beforeCount; ++i) {
      before[i] = entries[((beforeCount == 4 ? beforeHead : 0) + i) % 4];
    }
    for (uint8_t i = 0; i < beforeCount; ++i) {
      entries[i] = before[i];
    }
    used = beforeCount;
    cursor = 0;
    faulted = true;
    if (edge) {
      entries[used++] = *edge;
    }
    postLeft = 4;
    return true;
  }

  void stopPost() { postLeft = 0; }
  bool complete() const { return faulted && postLeft == 0; }
  bool pending() const { return faulted && cursor < used; }
  uint8_t size() const { return used; }

  bool pop(TimingDiagnostic &entry) {
    if (!pending()) {
      return false;
    }
    entry = entries[cursor++];
    return true;
  }

private:
  // Four preceding entries, optional fault edge, and four following entries.
  TimingDiagnostic entries[9] = {};
  uint8_t beforeCount = 0;
  uint8_t beforeHead = 0;
  uint8_t used = 0;
  uint8_t cursor = 0;
  uint8_t postLeft = 0;
  bool faulted = false;
};

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
  void reset() {
    choice = 0;
    invalid = false;
  }

  // -1: incomplete/blank line; -2: invalid line; 0/1: valid mode choice.
  int feed(char c) {
    if (c == '\r' || c == '\n') {
      if (choice == 0 && !invalid) {
        return -1;
      }
      const int result = invalid ? -2 : choice - '0';
      reset();
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
