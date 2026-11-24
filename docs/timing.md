# PCB pin map and bench checks

Arduino pins: D13 SCK, D11 COPI, D12 CIPO, and D2/D7/D8 select AFE1/2/3.
Capture uses external inputs at 128 samples/s with internal bias disabled.

The firmware assumes a shared FCLK. After all three PLLs lock, one
write-only SYNCH starts capture. FIFO reads select one AFE at a time.
The plot estimates alignment from FIFO timestamps.

1. Confirm FCLK routing, chip selects, electrode connections, bias, and power
   against the assembled PCB.
2. With an isolated ECG simulator, verify lead order, polarity, and relative
   timing across repeated starts and a longer run. Compare raw sample positions.
3. Record USB and BLE captures, check reconnect behavior, and replay both files.

References: [MAX30003 datasheet](01_MAX30003_Datasheet.pdf),
[Nano ESP32 pinout](04_Nano_ESP32_Full_Pinout.pdf),
[Nano ESP32 schematic](05_Nano_ESP32_Schematic.pdf),
and [Analog Devices shared-clock synchronization procedure](https://ez.analog.com/mems/a/documents/c/max30003cti-faq/DO19121/can-i-use-two-or-more-max30003-ics-to-create-a-multi-lead-ecg-design).
