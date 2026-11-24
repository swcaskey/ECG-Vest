#pragma once

#include <string.h>

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
