Three-lead ECG over BLE with SPI timing

ECG PCB and transmitter Nano ESP32  -- BLE -->  receiver Nano ESP32  -- USB -->  Mac

The PCB Nano reads the three AFEs using the same SPI configuration and
FIFO timing as the working single-Nano USB build. The receiver forwards
the transmitter's records to the Mac. It needs only a USB connection.

1. Flash the two sketches in Arduino IDE

   Select Arduino Nano ESP32. Connect one Nano at a time to identify its USB
   port and upload the sketch for that board.

   PCB Nano:  ecg_ble_tx/ecg_ble_tx.ino
   Other Nano: ecg_ble_rx/ecg_ble_rx.ino

   Keep each sketch's .h files beside its .ino. Use the Arduino ESP32
   board package's native USB defaults and By Arduino pin numbering.
   Both sketches were compiled with Arduino ESP32 core 2.0.18-arduino.5.
   The BLE and SPI libraries are bundled with that core.

2. Power the transmitter and connect the receiver

   Power the ECG PCB the same way you did for the USB test. If the PCB
   supply powers only the AFEs, give the transmitter Nano its own USB
   power bank for wireless operation. Plug the receiver Nano into the
   Mac with a data cable. No wires go between the two Nanos.

   The transmitter advertises as Capstone ECG SPI. The receiver discovers
   its BLE service automatically. Run one transmitter with this firmware
   within range. No Mac Bluetooth pairing step is needed. This bench
   build uses an unpaired BLE link.

3. Start the plot

   Close Arduino Serial Monitor and Serial Plotter. Activate the existing
   Python environment from the working USB build, then change to the
   extracted ecg-ble-spi folder. If you need a fresh environment:

     python3 -m venv .venv
     source .venv/bin/activate
     python -m pip install -r requirements.txt

   Find the receiver's USB port and use its actual name in this command:

     python -m serial.tools.list_ports
     python plot_ecg.py /dev/cu.usbmodem101

   Type go and press Enter. The receiver forwards the command once the
   BLE connection is ready. The traces appear after the viewer has enough
   timing records. The packaged plot_ecg.py is a single Python file and
   uses the same dependencies as the USB build.

4. Check timing and troubleshoot

   The PCB Nano estimates the time of every eighth unread sample from two
   FIFO status reads at most 2 ms apart. It skips an estimate when that
   window is too wide. Both boards transmit those timestamps unchanged.
   Python fits the three sample clocks on one time axis. Use an isolated
   ECG simulator to check lead labels, polarity and end-to-end alignment
   on the PCB. The firmware uses external inputs with internal bias off.

   After a BLE disconnect or packet gap, the plot clears and the receiver
   reconnects and requests a fresh capture while the viewer stays open.
   If the receiver USB cable disconnects, reopen the viewer and type go.

   For raw diagnostics, close Python, open Serial Monitor on the receiver
   at 115200 and send go with a newline. INFO: BLE_READY means the receiver
   subscribed to the transmitter. E2,D records carry data and E2,A records
   carry timing. INFO: BLE_LOST indicates a wireless interruption.
