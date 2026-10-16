
#include <SPI.h>
#include <ArduinoBLE.h>

constexpr int CS_PIN = D10;
constexpr int INT_PIN = D2;

const SPISettings MAX_SPI(1000000, MSBFIRST, SPI_MODE0);

BLEService ecgService("19B10000-E8F2-537E-4F6C-D104768A1214");

BLEStringCharacteristic ecgCharacteristic(
  "19B10001-E8F2-537E-4F6C-D104768A1214",
  BLERead | BLENotify,
  20
);

uint32_t readRegister(uint8_t address) {
  SPI.beginTransaction(MAX_SPI);
  digitalWrite(CS_PIN, LOW);

  SPI.transfer((address << 1) | 1);

  uint32_t value = uint32_t(SPI.transfer(0)) << 16;
  value |= uint32_t(SPI.transfer(0)) << 8;
  value |= SPI.transfer(0);

  digitalWrite(CS_PIN, HIGH);
  SPI.endTransaction();

  return value;
}

void writeRegister(uint8_t address, uint32_t value) {
  SPI.beginTransaction(MAX_SPI);
  digitalWrite(CS_PIN, LOW);

  SPI.transfer(address << 1);
  SPI.transfer((value >> 16) & 0xFF);
  SPI.transfer((value >> 8) & 0xFF);
  SPI.transfer(value & 0xFF);

  digitalWrite(CS_PIN, HIGH);
  SPI.endTransaction();
}

void setup() {
  Serial.begin(115200);

  pinMode(CS_PIN, OUTPUT);
  digitalWrite(CS_PIN, HIGH);

  pinMode(INT_PIN, INPUT_PULLUP);
  SPI.begin();

  // Initialize BLE.
  if (!BLE.begin()) {
    Serial.println("ERROR: BLE failed to start");
    while (true) {
      delay(1000);
    }
  }

  BLE.setLocalName("ECG_Transmitter");
  BLE.setAdvertisedService(ecgService);

  ecgService.addCharacteristic(ecgCharacteristic);
  BLE.addService(ecgService);

  ecgCharacteristic.writeValue("0");
  BLE.advertise();

  Serial.println("BLE advertising: ECG_Transmitter");

  // Reset MAX30003.
  writeRegister(0x08, 0);
  delay(100);

  readRegister(0x0F);  // Discard first ID read.
  uint32_t id = readRegister(0x0F);

  Serial.print("MAX30003 ID: 0x");
  Serial.println(id, HEX);

  if ((id & 0xF03000UL) != 0x503000UL) {
    while (true) {
      Serial.println("ERROR: MAX30003 identification failed");
      delay(1000);
    }
  }

  // Configure interrupts.
  writeRegister(0x04, 0x380004);
  writeRegister(0x02, 0xC00003);
  writeRegister(0x03, 0);

  // Enable ECG and configure clock.
  writeRegister(0x10, 0x080004);

  // Internal calibration waveform.
  writeRegister(0x14, 0x3B0000);
  writeRegister(0x12, 0x704800);

  // ECG: 128 samples/s, gain 20, filters enabled.
  writeRegister(0x15, 0x805000);

  delay(1000);

  readRegister(0x01);  // Clear latched status.
  delay(20);

  if (readRegister(0x01) & (1UL << 8)) {
    while (true) {
      Serial.println("ERROR: sampling clock PLL is not locked");
      delay(1000);
    }
  }

  // Synchronize and clear FIFO.
  writeRegister(0x09, 0);

  Serial.println("MAX30003 initialized");
}

void loop() {
  BLE.poll();

  if (digitalRead(INT_PIN) == HIGH) {
    delay(1);
    return;
  }

  // Drain available FIFO samples.
  for (int i = 0; i < 32; ++i) {
    uint32_t word = readRegister(0x21);

    // MAX30003 ETAG occupies bits 5:3.
    uint8_t tag = (word >> 3) & 0x07;

    if (tag == 6) {
      break;  // FIFO empty.
    }

    // Diagnostic output rather than resetting the FIFO.
    if (tag == 7 || tag == 4 || tag == 5) {
      Serial.print("FIFO unexpected tag: ");
      Serial.print(tag);
      Serial.print(" RAW: 0x");
      Serial.println(word, HEX);
      break;
    }

    if (tag == 0 || tag == 2) {
      // Extract signed 18-bit sample from bits 23:6.
      int32_t sample = (word >> 6) & 0x3FFFF;

      if (sample & 0x20000) {
        sample -= 0x40000;
      }

      Serial.print("TX ECG:");
      Serial.println(sample);

      // Transmit sample over BLE.
      ecgCharacteristic.writeValue(String(sample));
      BLE.poll();
    }

    // Tags 2 and 3 mark the last available sample.
    if (tag == 2 || tag == 3) {
      break;
    }
  }

  delay(5);
}
