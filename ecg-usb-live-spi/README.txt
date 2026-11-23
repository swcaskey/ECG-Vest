Three-lead ECG over USB with SPI timing

The Nano on the ECG PCB reads the three MAX ECG AFEs and sends their
electrode-input samples and timestamps directly to the MacBook. The Python
viewer plots the three measured leads on a shared five-second time axis.
This build uses SPI FIFO status for timing and requires no INTB connections.
Use the sketch and plot_ecg.py from this package together.

1. Flash the sketch with Arduino IDE
   Open ecg_usb_live/ecg_usb_live.ino from its extracted folder. Keep
   firmware_decoder.h beside it. Select Arduino Nano ESP32 and the port
   associated with the PCB Nano. The sketch was built with Arduino ESP32
   core 2.0.18-arduino.5 and default native USB and Arduino pin numbering.

2. Set up Python from the extracted ecg-usb-live folder

     python3 -m venv .venv
     source .venv/bin/activate
     python -m pip install -r requirements.txt

3. Connect an isolated ECG simulator
   Connect the simulator to the PCB's RA, LA, and LL inputs. The firmware
   routes the external electrode inputs and leaves internal electrode bias
   disabled. Check the project's input bias arrangement if that is needed.
   Use verified electrical isolation for any body-connected measurement.

4. Run the viewer
   Close Arduino Serial Monitor and Serial Plotter. Find the Nano's port:

     python -m serial.tools.list_ports

   Choose the /dev/cu.usbmodem entry for the PCB Nano and substitute it in:

     python plot_ecg.py /dev/cu.usbmodem101

   Type go and press Enter. The plot waits until it can fit all three clock
   models. The shared x-axis is in seconds and the vertical scale is in
   ADC counts. AFE1, AFE2, and AFE3 are labeled I, II, and III by default.
   Verify those labels and signs with the simulator before interpreting them.

The Nano reads each AFE's FIFO status about once per millisecond. It
timestamps the eighth unread sample at the midpoint between its last read
below the threshold and its first read at or above it. Each accepted
interval is at most 2 ms wide, so its midpoint is within 1 ms of that
observed threshold transition. The viewer fits the separate clocks and
interpolates onto one time axis. Check end-to-end alignment with an
isolated ECG simulator on the actual PCB. Status packet interrupt counters
are zero in this build. E2,A records carry the estimated timestamps.
