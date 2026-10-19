#include <SPI.h>

constexpr int CS_PIN = D10;
constexpr int INT_PIN = D2;
const SPISettings MAX_SPI(1000000, MSBFIRST, SPI_MODE0);

// Input mode: 0 = internal test, 1 = electrodes; send Enter in Serial Monitor.
// Live Plot prompts automatically. See README for wiring and safety details.
enum class InputMode { InternalTest, Electrodes };
bool acquisitionStarted = false;

// ECG enabled, 32.768 kHz clock; internal bias and lead-off disabled.
// Electrode mode uses the WING's external VCM body-bias connection.
constexpr uint32_t CNFG_GEN_VALUE = 0x080004;
// Readback masks cover documented fields only, ignoring reserved bits.
constexpr uint32_t CNFG_GEN_MASK = 0xF83FFF;
constexpr uint32_t CNFG_EMUX_MASK = 0xBF0000;
constexpr uint32_t CNFG_CAL_MASK = 0x707FFF;

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

bool readInputMode(InputMode &mode) {
  // Accept exactly one digit per line; invalid input never changes the mode.
  static char choice = 0;
  static bool invalid = false;
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\r' || c == '\n') {
      if (choice == 0 && !invalid) {
        continue; // Ignore blank lines and the LF following CR.
      }
      bool valid = !invalid;
      char selected = choice;
      choice = 0;
      invalid = false;
      if (valid) {
        mode =
            selected == '0' ? InputMode::InternalTest : InputMode::Electrodes;
        return true;
      }
      Serial.println("ERROR: enter 0 or 1, then press Enter");
    } else if (c != ' ' && c != '\t') {
      if (choice == 0 && (c == '0' || c == '1')) {
        choice = c;
      } else {
        invalid = true;
      }
    }
  }
  return false;
}

void haltWithError(const char *message) {
  while (true) {
    Serial.println(message);
    delay(1000);
  }
}

void startAcquisition(InputMode mode) {
  const uint32_t emux = mode == InputMode::InternalTest ? 0x3B0000 : 0x000000;
  const uint32_t calibration =
      mode == InputMode::InternalTest ? 0x704800 : 0x000000;

  writeRegister(0x08, 0); // Software reset
  delay(100);

  readRegister(0x0F); // Discard first read after reset
  uint32_t id = readRegister(0x0F);

  if ((id & 0xF03000UL) != 0x503000UL) {
    haltWithError("ERROR: MAX30003 identification failed");
  }

  pinMode(INT_PIN, INPUT_PULLUP);
  writeRegister(0x04, 0x380004); // Interrupt when 8 samples are available
  writeRegister(0x02, 0xC00003); // Enable sample-ready and overflow on INTB
  writeRegister(0x03, 0);        // INT2B disabled
  writeRegister(0x10, CNFG_GEN_VALUE);
  writeRegister(0x12, 0);           // Disable calibration before routing
  writeRegister(0x14, emux);        // CNFG_EMUX: select input source
  writeRegister(0x12, calibration); // CNFG_CAL: enable only for InternalTest
  writeRegister(0x15, 0x805000);    // 128 samples/s, gain 20, filters enabled

  delay(1000);        // Allow clock and analog circuits to settle
  readRegister(0x01); // Clear latched status
  delay(20);

  if (readRegister(0x01) & (1UL << 8)) {
    haltWithError("ERROR: sampling clock PLL is not locked");
  }

  if ((readRegister(0x10) & CNFG_GEN_MASK) != CNFG_GEN_VALUE ||
      (readRegister(0x14) & CNFG_EMUX_MASK) != emux ||
      (readRegister(0x12) & CNFG_CAL_MASK) != calibration) {
    haltWithError("ERROR: input/bias register readback mismatch");
  }

  writeRegister(0x09, 0); // Synchronize and clear FIFO
  Serial.println(mode == InputMode::InternalTest
                     ? "INFO: input=internal calibration"
                     : "INFO: input=electrodes; bias=external VCM");
}

void setup() {
  Serial.begin(115200);
  pinMode(CS_PIN, OUTPUT);
  digitalWrite(CS_PIN, HIGH);
  SPI.begin();
  writeRegister(0x08, 0); // Keep acquisition disabled until a mode is selected.
  delay(100);

  while (!Serial) {
    delay(10);
  }
  Serial.println("Select input mode, then press Enter:");
  Serial.println("0 = Internal test signal");
  Serial.println("1 = Electrodes (external VCM body bias)");
}

void loop() {
  InputMode requestedMode;
  if (readInputMode(requestedMode)) {
    startAcquisition(requestedMode);
    acquisitionStarted = true;
  }
  if (!acquisitionStarted) {
    delay(1);
    return;
  }

  if (digitalRead(INT_PIN) == HIGH) {
    delay(1);
    return; // Wait until MAX pulls INTB LOW
  }

  // Drain available samples, with a bounded loop.
  for (int i = 0; i < 32; ++i) {
    uint32_t word = readRegister(0x21);
    uint8_t tag = (word >> 3) & 0x07;

    if (tag == 6) {
      break; // FIFO empty
    }

    if (tag == 7 || tag == 4 || tag == 5) {
      Serial.println("ERROR: FIFO overflow or invalid tag; resetting");
      writeRegister(0x0A, 0);
      break;
    }

    if (tag == 0 || tag == 2) {
      // Extract signed 18-bit sample from bits 23:6.
      int32_t sample = word >> 6;
      if (sample & 0x20000) {
        sample -= 0x40000;
      }

      Serial.print("ECG:");
      Serial.println(sample);
    }
    // Tags 1 and 3 are settling samples: discard their voltage.

    if (tag == 2 || tag == 3) {
      break; // Last available sample
    }
  }

  delay(5);
}
