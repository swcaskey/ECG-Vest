#include <SPI.h>

constexpr int CS_PIN = D10;
const SPISettings MAX_SPI(1000000, MSBFIRST, SPI_MODE0);

uint32_t readRegister(uint8_t address)
{
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

void writeRegister(uint8_t address, uint32_t value)
{
    SPI.beginTransaction(MAX_SPI);
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(address << 1);
    SPI.transfer((value >> 16) & 0xFF);
    SPI.transfer((value >> 8) & 0xFF);
    SPI.transfer(value & 0xFF);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
}

void setup()
{
    Serial.begin(115200);
    pinMode(CS_PIN, OUTPUT);
    digitalWrite(CS_PIN, HIGH);
    SPI.begin();

    // Wait for the terminal before starting acquisition.
    while (!Serial)
    {
        delay(10);
    }

    writeRegister(0x08, 0); // Software reset
    delay(100);

    readRegister(0x0F); // Discard first read after reset
    uint32_t id = readRegister(0x0F);

    if ((id & 0xF03000UL) != 0x503000UL)
    {
        while (true)
        {
            Serial.println("ERROR: MAX30003 identification failed");
            delay(1000);
        }
    }

    writeRegister(0x02, 0);        // INTB disabled: polling test
    writeRegister(0x03, 0);        // INT2B disabled
    writeRegister(0x10, 0x080004); // ECG enabled; 32.768 kHz FCLK
    writeRegister(0x14, 0x3B0000); // Disconnect electrodes; select test inputs
    writeRegister(0x12, 0x704800); // Internal bipolar 1 Hz calibration
    writeRegister(0x15, 0x805000); // 128 samples/s, gain 20, filters enabled

    delay(1000);        // Allow clock and analog circuits to settle
    readRegister(0x01); // Clear latched status
    delay(20);

    if (readRegister(0x01) & (1UL << 8))
    {
        while (true)
        {
            Serial.println("ERROR: sampling clock PLL is not locked");
            delay(1000);
        }
    }

    writeRegister(0x09, 0); // Synchronize and clear FIFO
}

void loop()
{
    // Drain available samples, with a bounded loop.
    for (int i = 0; i < 32; ++i)
    {
        uint32_t word = readRegister(0x21);
        uint8_t tag = (word >> 3) & 0x07;

        if (tag == 6)
        {
            break; // FIFO empty
        }

        if (tag == 7 || tag == 4 || tag == 5)
        {
            Serial.println("ERROR: FIFO overflow or invalid tag; resetting");
            writeRegister(0x0A, 0);
            break;
        }

        if (tag == 0 || tag == 2)
        {
            // Extract signed 18-bit sample from bits 23:6.
            int32_t sample = word >> 6;
            if (sample & 0x20000)
            {
                sample -= 0x40000;
            }

            Serial.print("ECG:");
            Serial.println(sample);
        }
        // Tags 1 and 3 are settling samples: discard their voltage.

        if (tag == 2 || tag == 3)
        {
            break; // Last available sample
        }
    }

    delay(5);
}