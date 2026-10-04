# ECG Vest

Arduino Nano ESP32 + MAX30003WING bench prototype. The current firmware acquires
**internal calibration/test data**, not body-connected ECG. The Python graph
shows that test signal. This development-environment update does not change the
acquisition configuration or establish readiness for human testing.

## Windows development setup

1. Install Python 3.14 and Arduino CLI 1.5.1; make both available on PATH.
   Restart VS Code after installation. Verify with `python --version` and
   `arduino-cli version`.
2. Open this repository in a folder named `ECG-Vest` so Arduino can find
   `ECG-Vest.ino`. Keep only one sketch's `setup()` and `loop()` in this folder.
3. Run **Terminal > Run Task > ECG: 8. Setup Environment**. This installs the
   pinned Arduino core, creates `.venv` if needed, installs runtime and formatter
   packages, and installs the pinned PSScriptAnalyzer module for your user.
   If NuGet/module installation fails, setup downloads and validates the pinned
   module directly from PowerShell Gallery. Temporary downloads are cleaned up.
   Internet access is required. No administrator shell is required.
4. Run **Format Code**, **Check Syntax**, then **Build**. Review source changes
   before committing. A board is not needed for these tasks.
5. Connect the board, then run **Upload**. Upload builds first and automatically
   detects the serial port. Use **Live Plot** or **Serial Monitor**, one at a time;
   close either before uploading. **List Boards** helps diagnose port detection.

Tasks use Windows PowerShell (`powershell.exe`), matching CI. Setup rejects other
PowerShell editions with the exact command to run in the correct shell. Run scripts manually
from the root using `powershell -NoProfile -ExecutionPolicy Bypass -File` followed
by the script path and arguments. For example:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/ecg.ps1 -Action setup
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/format.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check-syntax.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/ecg.ps1 -Action build
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/ecg.ps1 -Action upload -Port COM7
```

## What is checked

- **Format Code** rewrites root-level `.py`, `.ino`, `.c`, `.cpp`, `.h`, `.hpp`
  files and `scripts/**/*.ps1`, reporting each changed/unchanged file. It uses
  Black, clang-format (LLVM style), and PSScriptAnalyzer's Invoke-Formatter.
- **Check Syntax** compiles Python source without executing it, validates
  PowerShell parsing, and runs those same formatters on temporary copies to
  detect differences. It does not rewrite project files. JSON/YAML are not
  formatted or syntax-checked by this task.
- **Build** compiles Arduino/C++ with warnings enabled. Formatters cannot repair
  arbitrary syntax errors or determine whether the firmware behaves correctly.
- **CI** runs the same setup, check, and build scripts on Windows. It never
  uploads to hardware or opens the graph. Successful firmware artifacts are
  retained for 14 days. Check Syntax alone cannot guarantee CI passes: run Build
  locally as well.

## Version control and maintenance

`toolchain.json` owns the board, core, Arduino CLI, Python minor version, and
PSScriptAnalyzer version. `requirements.txt` retains the working runtime package
pins; `requirements-dev.txt` adds pinned formatters. Change pins deliberately,
rerun Setup Environment, then Format Code, Check Syntax, and Build.

If an existing `.venv` uses another Python minor version, close programs using it,
remove that generated environment, and run Setup Environment again. Do not
commit `.venv`, `.build`, or Python cache files. `.gitattributes` keeps source
line endings consistent across checkout environments.

The plot assumes 128 samples/second and does not mark FIFO losses as time gaps.
Hardware behavior, FIFO recovery, and live-electrode acquisition still need
bench validation; CI only validates source formatting, syntax, and compilation.

## Validation of this update

- Parsed JSON/YAML and Python source; checked all PowerShell scripts with the
  PowerShell parser.
- Ran formatting and then the shared checker successfully with the pinned
  formatters (PowerShell 7.4 on Linux, using temporary executable adapters).
- Confirmed that malformed Python/PowerShell and unformatted Python fail checks
  without changing source files.
- Resolved/downloaded all requirements for Windows x64 / Python 3.14.
- Simulated NuGet failure and verified fallback installation plus repeat-run
  reuse with the official cached module package on Linux. The Windows-only
  Unblock-File operation was mocked for this test.
- Full Windows PowerShell 5.1 setup, Arduino compilation, and GitHub Actions have
  not been executed for this update. The review environment could not download
  board indexes through Arduino CLI. Confirm Setup, Check Syntax, and Build on
  Windows, then push and verify the first CI run.
