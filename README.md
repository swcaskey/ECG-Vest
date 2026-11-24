# ECG Vest

The PCB Nano ESP32 measures ECG Leads I, II, and III at 128 samples/s.
The live plot shows ADC counts over a rolling five-second window.

- **USB** uses `pcb_usb` on the PCB Nano. Open its USB port.
- **BLE** uses `pcb_ble_tx` on the PCB Nano and `ble_receiver` on a second Nano.
  Open the receiver's USB port.

Install the versions in [toolchain.json](toolchain.json). In VS Code, open
**Terminal > Run Task**. Close the plot or monitor before uploading. Select the
transport in the live task and type `go` to start.

| VS Code task | Purpose |
| --- | --- |
| `ECG: * Setup Environment` | Install the Nano core and development dependencies. |
| `ECG: 1. Format Code` | Format project source. |
| `ECG: 2. Check Syntax` | Check syntax and formatting without changing source. |
| `ECG: 3. Build` | Compile all three sketches and refresh IntelliSense. |
| `ECG: 4. Run Unit Tests` | Run automated checks without hardware. |
| `ECG: 5. Upload` | Select the firmware role and its Nano port. |
| `ECG: 6. List Boards` | List attached board ports. |
| `ECG: 7. Serial Monitor` | Read raw records from the selected port. |
| `ECG: 8. Live Plot` | Show three measured traces. |
| `ECG: 9. Start Recording` | Name a CSV and close the plot to stop recording. |
| `ECG: 10. Replay Recording` | Select a saved capture by number. |

Recordings stay local in `recordings/`. [PCB pin map and bench checks](docs/timing.md)
