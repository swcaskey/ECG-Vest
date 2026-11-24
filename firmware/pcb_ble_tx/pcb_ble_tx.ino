#include <EcgPcb.h>
#include <ble_link.h>

EcgPcb capture(bleClientActive);

void pumpBle() {
  uint8_t packet[BLE_HEADER + BLE_BODY];
  for (size_t i = 0; i < 4; ++i) {
    const BleLink current = getBleLink();
    const size_t capacity = bleSendCapacity(current);
    if (!capacity) {
      return;
    }
    const size_t count = capture.copyPending(packet + BLE_HEADER, capacity);
    if (!count || !notifyBle(current, packet, count)) {
      return;
    }
    capture.consume(count);
  }
}

void setup() {
  capture.begin();
  setupBle();
}

void loop() {
  if (takeBleChange()) {
    capture.stop();
    packetNumber = 0;
    if (!getBleLink().connected) {
      BLEDevice::startAdvertising();
    }
  }
  if (bleClientActive()) {
    if (takeStartRequest()) {
      capture.start();
    }
    capture.service();
    pumpBle();
  }
  delay(1);
}
