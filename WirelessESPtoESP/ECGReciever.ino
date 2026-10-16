
#include <ArduinoBLE.h>

const char* SERVICE_UUID =
  "19B10000-E8F2-537E-4F6C-D104768A1214";

const char* ECG_CHAR_UUID =
  "19B10001-E8F2-537E-4F6C-D104768A1214";

void setup() {
  Serial.begin(115200);

  if (!BLE.begin()) {
    Serial.println("ERROR: BLE failed to start");
    while (true) {
      delay(1000);
    }
  }

  Serial.println("Scanning for ECG transmitter...");
  BLE.scanForUuid(SERVICE_UUID);
}

void loop() {
  BLEDevice peripheral = BLE.available();

  if (!peripheral) {
    return;
  }

  Serial.print("Found device: ");
  Serial.println(peripheral.localName());

  BLE.stopScan();

  if (!peripheral.connect()) {
    Serial.println("Connection failed");
    BLE.scanForUuid(SERVICE_UUID);
    return;
  }

  Serial.println("Connected!");

  if (!peripheral.discoverService(SERVICE_UUID)) {
    Serial.println("ECG service not found");
    peripheral.disconnect();
    BLE.scanForUuid(SERVICE_UUID);
    return;
  }

  BLECharacteristic ecgCharacteristic =
    peripheral.characteristic(ECG_CHAR_UUID);

  if (!ecgCharacteristic || !ecgCharacteristic.canSubscribe()) {
    Serial.println("ECG characteristic unavailable");
    peripheral.disconnect();
    BLE.scanForUuid(SERVICE_UUID);
    return;
  }

  if (!ecgCharacteristic.subscribe()) {
    Serial.println("Subscription failed");
    peripheral.disconnect();
    BLE.scanForUuid(SERVICE_UUID);
    return;
  }

  Serial.println("Receiving ECG samples...");

  while (peripheral.connected()) {
    BLE.poll();

    if (ecgCharacteristic.valueUpdated()) {
      char value[21] = {0};

      int length = ecgCharacteristic.readValue(
        (uint8_t*)value,
        sizeof(value) - 1
      );

      if (length > 0) {
        value[length] = '\0';

        Serial.print("ECG:");
        Serial.println(value);
      }
    }
  }

  Serial.println("Disconnected. Scanning again...");
  BLE.scanForUuid(SERVICE_UUID);
}
