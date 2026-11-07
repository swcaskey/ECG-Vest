# ECG Vest

Arduino Nano ESP32 firmware and a Python live graph for two MAX30003WING boards
over USB serial using the E2 protocol at 115200 baud. The graph shows measured
Lead I and Lead III, plus estimated Lead II, in ADC counts over a rolling
five-second window at 128 samples/s. Lead II is unavailable by default until
physical timing validation is complete.

Choose `0` for both boards' internal 1 Hz test signals or `1` for electrodes:

- **Live Plot** prompts in the terminal; Enter defaults to `0`.
- **Serial Monitor** prompts for `0` or `1` at startup.

Acquisition waits for a selection. To change modes, reopen Live Plot or Serial
Monitor; no rebuild is needed after the initial upload.

Electrode cable: red = ECGP (+), blue = ECGN (-), black = VCM body bias, not ground.
Use Board 1's black lead for body bias; keep Board 2's black lead insulated.

Human-connected testing requires a separately reviewed electrical safety setup;
the direct laptop USB connection is not a patient-isolation barrier.

## Setup Environment

1. Clone the repository into a folder named `ECG-Vest`.
2. Install the Python and Arduino CLI versions specified in `toolchain.json`.
3. Open **Terminal > Run Task** and select **Setup Environment**.

## Development Workflow

Open **Terminal > Run Task**. Run tasks 1–4 before committing.

| Task | Purpose |
| --- | --- |
| `ECG: 1. Format Code` | Format source files. |
| `ECG: 2. Check Syntax` | Check Python/PowerShell syntax and formatting. |
| `ECG: 3. Build` | Compile firmware. |
| `ECG: 4. Run Unit Tests` | Run automated software checks without hardware. |
| `ECG: 5. Upload` | Build and upload firmware to the Nano. |
| `ECG: 6. List Boards` | List connected boards and ports. |
| `ECG: 7. Serial Monitor` | Choose a mode at startup and view serial output. |
| `ECG: 8. Live Plot` | Show live graphs; choose mode `0` or `1`. |
| `ECG: 9. Start Recording` | Name a CSV and plot while saving data to `recordings/`. |
| `ECG: 10. Replay Recording` | Select a saved recording by number and graph it. |
| `ECG: * Setup Environment` | Install the board core and development dependencies. |

Close the serial monitor or plot before uploading. Only one application should
use the same serial port at a time.

## Configuration and Dependencies

- `toolchain.json` — board target and tool versions.
- `requirements.txt` — Python runtime dependencies.
- `requirements-dev.txt` — Python development dependencies.
- `.vscode/tasks.json` — VS Code task definitions.
- `.vscode/c_cpp_properties.json` — IntelliSense compilation database configuration.
- `.github/workflows/ci.yml` — continuous integration workflow.

After successful compilation, Build regenerates
`.build/intellisense_compile_commands.json`, including an entry for the original
`ECG-Vest.ino`. Run Build to refresh IntelliSense after changing the board,
libraries, or build configuration.

When changing tools or dependencies, update the appropriate configuration,
rerun setup, and verify formatting, syntax checks, and compilation.

Do not commit generated environments, build output, or Python cache files.
These are excluded through `.gitignore`.
