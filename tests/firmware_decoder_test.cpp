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
  return 0;
}
