$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Toolchain = Get-Content (Join-Path $ProjectRoot "toolchain.json") -Raw | ConvertFrom-Json
$AppSource = Join-Path $ProjectRoot "ecg\desktop_app.py"
$AppIcon = Join-Path $ProjectRoot "ecg\assets\icon.png"
$HeartData = Join-Path $ProjectRoot "ecg\data\heart_reference.npz"
$OutputDir = Join-Path $ProjectRoot ".build\app"
$PackageDir = Join-Path $ProjectRoot ".build\pyinstaller"

try {
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Run ECG: * Setup Environment first."
    }
    if (-not (Test-Path -LiteralPath $AppSource -PathType Leaf)) {
        throw "Desktop app source is missing."
    }
    if (-not (Test-Path -LiteralPath $AppIcon -PathType Leaf)) {
        throw "Desktop app icon is missing."
    }
    & $Python -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements.txt") "pyinstaller==$($Toolchain.pyInstaller)"
    if ($LASTEXITCODE -ne 0) { throw "Could not install the desktop build dependencies." }

    New-Item -ItemType Directory -Path $PackageDir -Force | Out-Null
    $WindowIcon = Join-Path $PackageDir "icon.png"
    & $Python -c "from PIL import Image; import sys; Image.open(sys.argv[1]).resize((64, 64), Image.Resampling.LANCZOS).save(sys.argv[2])" $AppIcon $WindowIcon
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the window icon." }

    $env:PYINSTALLER_CONFIG_DIR = Join-Path $PackageDir "cache"
    $arguments = @(
        "-m", "PyInstaller",
        "--name", "ECG Vest",
        "--icon", $AppIcon,
        "--onefile", "--windowed", "--noupx", "--noconfirm",
        "--paths", $ProjectRoot,
        "--hidden-import", "matplotlib.backends.backend_tkagg",
        # The desktop app only exports PNG plots.
        "--exclude-module", "PIL.AvifImagePlugin",
        "--exclude-module", "PIL.WebPImagePlugin",
        "--add-data", "${WindowIcon}:assets",
        "--add-data", "${HeartData}:ecg/data",
        "--distpath", $OutputDir,
        "--workpath", (Join-Path $PackageDir "work"),
        "--specpath", $PackageDir,
        $AppSource
    )
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Desktop app build failed." }
    $executable = Join-Path $OutputDir "ECG Vest.exe"
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "Desktop app executable is missing."
    }
    Write-Host "READY  $executable"
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
