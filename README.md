# ECG Vest

Arduino Nano ESP32 firmware and a Python live graph for MAX30003 acquisition
over USB serial. The stream uses `ECG:<signed integer>` at 115200 baud;
the graph displays ADC counts over a rolling five-second window at 128 samples/s.

Choose `0` for the internal 1 Hz test signal or `1` for electrodes:

- **Live Plot** prompts in the terminal; Enter defaults to `0`.
- **Serial Monitor** accepts `0` or `1` followed by Enter.

Acquisition waits for a selection. To change modes, reopen Live Plot or send
another selection in Serial Monitor; no rebuild is needed after the initial upload.

Electrode cable: red = ECGP (+), blue = ECGN (-), black = VCM body bias, not ground.

Human-connected testing requires a separately reviewed electrical safety setup;
the direct laptop USB connection is not a patient-isolation barrier.

## Setup Environment

1. Clone the repository into a folder named `ECG-Vest`.
2. Install the Python and Arduino CLI versions specified in `toolchain.json`.
3. Open **Terminal > Run Task** and select **Setup Environment**.

## Development Workflow

Run these VS Code tasks before committing:

1. **Format Code** — apply consistent source formatting.
2. **Check Syntax** — check Python/PowerShell syntax and source formatting.
3. **Build** — compile the firmware for the configured board.

For hardware development:

- **List Boards** displays connected boards.
- **Upload** builds and uploads firmware to a detected supported board.
- **Serial Monitor** displays serial output.
- **Live Plot** opens the Python visualization.

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
