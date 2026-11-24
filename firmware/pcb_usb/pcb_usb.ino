#include <EcgPcb.h>
#include <start_command.h>

constexpr uint32_t BAUD_RATE = 115200;
portMUX_TYPE usbMux = portMUX_INITIALIZER_UNLOCKED;
bool usbDisconnectPending = false; // Protected by usbMux.
bool serialWasConnected = false;
StartCommand inputCommand;

void onUsbDisconnect(void *, esp_event_base_t, int32_t, void *) {
  // The callback marks the disconnect; the main loop owns SPI cleanup.
  portENTER_CRITICAL(&usbMux);
  usbDisconnectPending = true;
  portEXIT_CRITICAL(&usbMux);
}

bool serialClientActive() {
  portENTER_CRITICAL(&usbMux);
  const bool disconnected = usbDisconnectPending;
  portEXIT_CRITICAL(&usbMux);
  return Serial && !disconnected;
}

EcgPcb capture(serialClientActive);

void pumpSerial() {
  uint8_t bytes[320];
  for (size_t i = 0; i < 4 && serialClientActive(); ++i) {
    const int available = Serial.availableForWrite();
    if (available <= 0) {
      return;
    }
    const size_t count =
        capture.copyPending(bytes, min<size_t>(available, sizeof(bytes)));
    if (!count) {
      return;
    }
    const size_t sent = Serial.write(bytes, count);
    if (!sent) {
      return;
    }
    capture.consume(sent);
  }
}

void setup() {
  Serial.onEvent(ARDUINO_USB_CDC_DISCONNECTED_EVENT, onUsbDisconnect);
  Serial.begin(BAUD_RATE);
  // Native USB CDC must not hold up FIFO servicing.
  Serial.setTxTimeoutMs(0);
  capture.begin();
  while (!Serial) {
    delay(10);
  }
  Serial.println("INFO: Ready. Send go to start.");
}

void loop() {
  portENTER_CRITICAL(&usbMux);
  const bool disconnected = usbDisconnectPending;
  usbDisconnectPending = false;
  portEXIT_CRITICAL(&usbMux);
  if (disconnected || !Serial) {
    if (serialWasConnected || disconnected) {
      capture.stop();
    }
    inputCommand.reset();
    serialWasConnected = false;
    if (!Serial) {
      // Drain old client input only while disconnected.
      for (size_t i = 0; i < 64 && Serial.available() > 0; ++i) {
        Serial.read();
      }
      delay(1);
      return;
    }
  }
  serialWasConnected = true;
  pumpSerial();
  for (size_t i = 0; i < 64 && Serial.available() > 0; ++i) {
    const int command = inputCommand.feed(Serial.read());
    if (command == 1) {
      capture.start();
      break;
    }
    if (command == -2) {
      capture.reportInvalidCommand();
    }
  }
  capture.service();
  pumpSerial();
  delay(1);
}
