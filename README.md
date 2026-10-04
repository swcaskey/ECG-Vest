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
- `.github/workflows/ci.yml` — continuous integration workflow.

When changing tools or dependencies, update the appropriate configuration,
rerun setup, and verify formatting, syntax checks, and compilation.

Do not commit generated environments, build output, or Python cache files.
These are excluded through `.gitignore`.