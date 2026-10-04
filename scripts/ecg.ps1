param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("build", "upload", "monitor", "plot", "boards", "setup")]
    [string]$Action,

    [string]$Port = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Board = "arduino:esp32:nano_nora"
$BuildDir = Join-Path $ProjectRoot ".build"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

Set-Location $ProjectRoot

# PowerShell 5 does not automatically stop on native-command failures.
function Invoke-Checked {
    param(
        [string]$Program,
        [string[]]$Arguments
    )

    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE."
    }
}

function Build-Firmware {
    Invoke-Checked "arduino-cli" @(
        "compile",
        "--fqbn", $Board,
        "--build-path", $BuildDir,
        $ProjectRoot
    )
}

function Find-BoardPort {
    if ($Port) {
        return $Port
    }

    $raw = & arduino-cli board list --json
    if ($LASTEXITCODE -ne 0) {
        throw "Could not list Arduino boards."
    }

    $result = ($raw -join "`n") | ConvertFrom-Json
    $ports = @(
        foreach ($entry in $result.detected_ports) {
            if ($entry.port.protocol -eq "serial") {
                foreach ($device in $entry.matching_boards) {
                    if ($device.fqbn -eq $Board) {
                        $entry.port.address
                    }
                }
            }
        }
    )
    $ports = @($ports | Sort-Object -Unique)

    if ($ports.Count -eq 0) {
        throw "No Nano ESP32 serial port found. Connect it and run ECG: List Boards."
    }

    if ($ports.Count -gt 1) {
        Write-Host "Connected Nano ESP32 ports: $($ports -join ', ')"
        $selection = Read-Host "Enter the COM port to use"
        if ($selection -notin $ports) {
            throw "Port was not one of the detected Nano ESP32 boards."
        }
        return $selection
    }

    return $ports[0]
}

try {
    if (-not (Get-Command arduino-cli -ErrorAction SilentlyContinue)) {
        throw "Install Arduino CLI and restart VS Code first."
    }

    switch ($Action) {
        "setup" {
            Invoke-Checked "arduino-cli" @("core", "update-index")
            Invoke-Checked "arduino-cli" @(
                "core", "install",
                "arduino:esp32@2.0.18-arduino.5"
            )

            if (-not (Test-Path $Python)) {
                Invoke-Checked "py" @("-m", "venv", ".venv")
            }

            Invoke-Checked $Python @(
                "-m", "pip", "install", "-r", "requirements.txt"
            )
        }

        "build" {
            Build-Firmware
        }

        "upload" {
            $SelectedPort = Find-BoardPort
            Write-Host "Target: $SelectedPort. Close the plot/monitor first."
            Build-Firmware

            Invoke-Checked "arduino-cli" @(
                "upload",
                "--port", $SelectedPort,
                "--fqbn", $Board,
                "--input-dir", $BuildDir,
                $ProjectRoot
            )
        }

        "monitor" {
            $SelectedPort = Find-BoardPort
            Invoke-Checked "arduino-cli" @(
                "monitor",
                "--port", $SelectedPort,
                "--config", "baudrate=115200"
            )
        }

        "plot" {
            if (-not (Test-Path $Python)) {
                throw "Python environment missing. Run ECG: Setup Environment."
            }

            $SelectedPort = Find-BoardPort
            $env:ECG_PORT = $SelectedPort
            Write-Host "Opening graph on $SelectedPort"

            Invoke-Checked $Python @(
                (Join-Path $ProjectRoot "plot_ecg.py")
            )
        }

        "boards" {
            Invoke-Checked "arduino-cli" @("board", "list")
        }
    }
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}