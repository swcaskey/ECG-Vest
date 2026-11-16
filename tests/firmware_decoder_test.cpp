#include "firmware_decoder.h"

#include <assert.h>
#include <stddef.h>
#include <stdint.h>

int main() {
  // Literal SPI records, independent expected voltages (including 18-bit
  // edges).
  const uint32_t words[] = {0x000000, 0x000040, 0x7FFFC0,
                            0x800000, 0xFFFFC0, 0xFE1DC0};
  const int32_t expected[] = {0, 1, 131071, -131072, -1, -1929};
  for (size_t i = 0; i < sizeof(words) / sizeof(words[0]); ++i) {
    // PTAG bits must not leak into the voltage or ETAG interpretation.
    const FifoEntry entry = decodeFifo(words[i] | 7);
    assert(entry.value == expected[i]);
    assert(entry.voltageValid && entry.advancesTime);
    assert(!entry.last && !entry.empty && !entry.overflow && !entry.reserved);
  }

  for (uint8_t tag = 0; tag < 8; ++tag) {
    const FifoEntry entry = decodeFifo(0xFFFFC0 | (uint32_t(tag) << 3));
    assert(entry.value == -1);
    assert(entry.tag == tag);
    assert(entry.advancesTime == (tag <= 3));
    assert(entry.voltageValid == (tag == 0 || tag == 2));
    assert(entry.last == (tag == 2 || tag == 3));
    assert(entry.empty == (tag == 6));
    assert(entry.overflow == (tag == 7));
    assert(entry.reserved == (tag == 4 || tag == 5));
  }

  // Invalid settling voltages still consume FIFO sequence/time positions.
  const uint32_t records[] = {0x000040, 0x000088, 0x0000D8};
  unsigned positions = 0;
  unsigned valid = 0;
  for (size_t i = 0; i < 3; ++i) {
    const FifoEntry entry = decodeFifo(records[i]);
    positions += entry.advancesTime;
    valid += entry.voltageValid;
  }
  assert(positions == 3 && valid == 1);

  // EINT identifies the eighth unread position, independent of callback count.
  FifoAnchorState fifoTiming;
  FifoThresholdEdge threshold = fifoTiming.edge();
  assert(threshold.position == 8 && fifoAnchorMatches(threshold, 0));
  assert(!fifoAnchorMatches(threshold, 1));
  fifoTiming.beginDrain();
  assert(fifoTiming.edge().duringDrain);
  for (unsigned i = 0; i < 8; ++i) {
    assert(fifoTiming.completeRead() == i + 1);
  }
  threshold = fifoTiming.edge();
  assert(threshold.position == 16 && !fifoAnchorMatches(threshold, 8));
  fifoTiming.endDrain();
  assert(fifoAnchorMatches(fifoTiming.edge(), 8));
  // A stalled service can drain backlog without inventing intervening edges.
  fifoTiming.beginDrain();
  for (unsigned i = 0; i < 24; ++i) {
    fifoTiming.completeRead();
  }
  fifoTiming.endDrain();
  threshold = fifoTiming.edge();
  assert(threshold.position == 40 && fifoAnchorMatches(threshold, 32));
  assert(!fifoAnchorMatches(threshold, 16));
  fifoTiming.reset();
  assert(fifoAnchorMatches(fifoTiming.edge(), 0));

  ModeCommand command;
  assert(command.feed('\n') == -1); // No automatic start on a blank line.
  assert(command.feed('1') == -1);  // Fragmented command waits for newline.
  assert(command.feed('\r') == 1);
  assert(command.feed('\n') == -1); // CRLF must not select another mode.
  assert(command.feed('0') == -1);
  assert(command.feed('\n') == 0); // Mode switch uses the same parser.
  assert(command.feed('1') == -1);
  assert(command.feed('0') == -1);
  assert(command.feed('\n') == -2); // Multiple digits never choose a mode.
  for (unsigned i = 0; i < 10000; ++i) {
    assert(command.feed('x') == -1); // Bounded storage, no input buffer growth.
  }
  assert(command.feed('\n') == -2);
  assert(command.feed(' ') == -1);
  assert(command.feed('1') == -1);
  assert(command.feed('\t') == -1);
  assert(command.feed('\n') == 1); // Invalid input does not poison next line.

  // A new USB client must not inherit an unfinished or invalid command.
  assert(command.feed('1') == -1);
  command.reset();
  assert(command.feed('0') == -1);
  assert(command.feed('\n') == 0);
  assert(command.feed('x') == -1);
  command.reset();
  assert(command.feed('1') == -1);
  assert(command.feed('\n') == 1);

  const char *timingFaults[] = {"TIMING_STALE",
                                "TIMING_COUNT",
                                "TIMING_STARTUP",
                                "TIMING_MISSED",
                                "TIMING_GLITCH_D181_P7813_R7812",
                                "TIMING_DRAIN_OVERLAP",
                                "TIMING_DUPLICATE_IRQ",
                                "TIMING_THRESHOLD_ABSENT",
                                "TIMING_IRQ_MISSING",
                                "IRQ_QUEUE_OVERFLOW"};
  for (const char *code : timingFaults) {
    assert(timingOnlyFault(code));
  }
  const char *acquisitionFaults[] = {
      "FIFO_OVERFLOW",     "PLL_UNLOCK", "DATA_STALE",     "RESERVED_TAG",
      "TX_QUEUE_OVERFLOW", "CHIP_ID",    "CONFIG_READBACK"};
  for (const char *code : acquisitionFaults) {
    assert(!timingOnlyFault(code));
  }
  assert(!timingOnlyFault(nullptr));

  TimingContext context;
  assert(sizeof(context) <= 9 * sizeof(TimingDiagnostic) + 16);
  for (uint64_t pulse = 1; pulse <= 10; ++pulse) {
    context.observe({pulse, int64_t(pulse * 1000), 1000, pulse * 8, 0});
  }
  const TimingDiagnostic shortEdge = {11, 10181, 181, 80, 0};
  assert(context.begin(&shortEdge));
  assert(!context.begin(&shortEdge)); // One bounded window per epoch.
  assert(context.size() == 5 && !context.complete());
  TimingDiagnostic observed = {};
  assert(context.pop(observed) && observed.pulseCount == 7);
  for (uint64_t pulse = 12; pulse <= 15; ++pulse) {
    context.observe({pulse, int64_t(pulse * 1000), 1000, pulse * 8, 0});
  }
  assert(context.complete() && context.size() == 9);
  for (uint64_t pulse = 16; pulse < 5000; ++pulse) {
    context.observe({pulse, int64_t(pulse * 1000), 1000, pulse * 8, 0});
  }
  assert(context.size() == 9); // Later edges cannot overwrite the evidence.
  for (uint64_t pulse = 8; pulse <= 15; ++pulse) {
    assert(context.pop(observed) && observed.pulseCount == pulse);
    if (pulse == 11) {
      assert(observed.time == 10181 && observed.interval == 181);
      assert(observed.fifoReads == 80 && observed.irqLost == 0);
    }
  }
  assert(!context.pop(observed));

  context.reset();
  context.observe({1, 1000, 0, 0, 0});
  assert(context.begin(nullptr)); // Stale timing has no observed fault edge.
  for (uint64_t pulse = 2; pulse <= 5; ++pulse) {
    context.observe({pulse, int64_t(pulse * 1000), 1000, pulse * 8, 0});
  }
  assert(context.complete() && context.size() == 5);
  for (uint64_t pulse = 1; pulse <= 5; ++pulse) {
    assert(context.pop(observed) && observed.pulseCount == pulse);
  }
  assert(!context.pending());

  context.reset();
  const TimingDiagnostic lostEdge = {129, 10000, -2, 32, 1};
  assert(context.begin(&lostEdge));
  context.stopPost(); // Queue overflow stops capture immediately.
  context.observe({130, 11000, 1000, 40, 2});
  assert(context.complete() && context.size() == 1);
  assert(context.pop(observed) && observed.pulseCount == 129);
  assert(observed.interval == -2 && observed.irqLost == 1);
  assert(!context.pop(observed));
  return 0;
}
