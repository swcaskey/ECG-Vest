param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("build", "upload", "monitor", "plot", "boards", "setup", "test", "replay")]
    [string]$Action,

    [string]$Port = "",

    [string]$RecordingPath = "",

    [string]$RecordPath = "",

    [ValidateRange(1, 1000000)]
    [double]$Range = 8000,

    [switch]$Headless,

    [switch]$AllowUnvalidatedTiming
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Toolchain = Get-Content (Join-Path $ProjectRoot "toolchain.json") -Raw | ConvertFrom-Json
$Board = $Toolchain.board
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

function Install-Analyzer {
    $version = $Toolchain.psScriptAnalyzer
    if (-not (Get-Module -ListAvailable PSScriptAnalyzer | Where-Object { $_.Version -eq $version })) {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        try {
            Write-Host "Installing PSScriptAnalyzer $version through PowerShell Gallery..."
            Install-PackageProvider NuGet -MinimumVersion 2.8.5.201 -Scope CurrentUser -Force -ErrorAction Stop | Out-Null
            Install-Module PSScriptAnalyzer -RequiredVersion $version -Repository PSGallery -Scope CurrentUser -Force -ErrorAction Stop
            Import-Module PSScriptAnalyzer -RequiredVersion $version -Force -ErrorAction Stop
        }
        catch {
            Write-Host "Standard module installation failed: $($_.Exception.Message)"
            Write-Host "Downloading PSScriptAnalyzer $version directly from PowerShell Gallery..."
            $staging = Join-Path ([System.IO.Path]::GetTempPath()) ("ecg-analyzer-" + [guid]::NewGuid().ToString())
            try {
                New-Item -ItemType Directory -Path $staging -Force | Out-Null
                $archive = Join-Path $staging "module.zip"
                $unpacked = Join-Path $staging "module"
                Invoke-WebRequest -UseBasicParsing -ErrorAction Stop `
                    -Uri "https://www.powershellgallery.com/api/v2/package/PSScriptAnalyzer/$version" `
                    -OutFile $archive
                Expand-Archive -Path $archive -DestinationPath $unpacked -Force
                Get-ChildItem $unpacked -Recurse -File | Unblock-File
                $manifest = Join-Path $unpacked "PSScriptAnalyzer.psd1"
                $metadata = Test-ModuleManifest -Path $manifest -ErrorAction Stop
                if ($metadata.Version -ne [version]$version) {
                    throw "Downloaded module version does not match $version."
                }
                $moduleRoot = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "WindowsPowerShell\Modules"
                $destination = Join-Path $moduleRoot "PSScriptAnalyzer\$version"
                New-Item -ItemType Directory -Path $destination -Force | Out-Null
                Copy-Item -Path (Join-Path $unpacked "*") -Destination $destination -Recurse -Force
            }
            finally {
                if (Test-Path -LiteralPath $staging) {
                    Remove-Item -LiteralPath $staging -Recurse -Force
                }
            }
        }
    }
    # Verify discovery by name, as format.ps1 will use it in a separate process.
    Import-Module PSScriptAnalyzer -RequiredVersion $version -Force -ErrorAction Stop
    Write-Host "READY  PSScriptAnalyzer $version"
}

function Update-IntelliSenseDatabase {
    $databasePath = Join-Path $BuildDir "compile_commands.json"
    $outputPath = Join-Path $BuildDir "intellisense_compile_commands.json"
    $sketchPath = Join-Path $ProjectRoot "ECG-Vest.ino"
    $generatedPath = Join-Path $BuildDir "sketch\ECG-Vest.ino.cpp"

    # Assign before wrapping: PowerShell 5.1 does not enumerate JSON arrays.
    $database = Get-Content -LiteralPath $databasePath -Raw | ConvertFrom-Json
    $entries = @($database)
    $matches = @($entries | Where-Object {
            $_.file.Replace('\', '/') -eq $generatedPath.Replace('\', '/')
        })
    if ($matches.Count -ne 1 -or -not $matches[0].arguments) {
        throw "Compilation database must contain one generated sketch entry with arguments."
    }

    $sketchEntry = $matches[0].PSObject.Copy()
    $sketchEntry.file = $sketchPath.Replace('\', '/')
    $sketchEntry.arguments = @(
        $matches[0].arguments[0]
        # The original .ino needs C++ mode and Arduino's implicit header.
        "-x"
        "c++"
        "-include"
        "Arduino.h"
        foreach ($argument in ($matches[0].arguments | Select-Object -Skip 1)) {
            if ($argument.Replace('\', '/') -eq $generatedPath.Replace('\', '/')) {
                $sketchEntry.file
            }
            else {
                $argument
            }
        }
    )

    # -InputObject preserves the top-level array, even with only one entry.
    $json = ConvertTo-Json -InputObject @($entries + $sketchEntry) -Depth 10
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($outputPath, $json + "`n", $utf8)
    Write-Host "READY  .build/intellisense_compile_commands.json ($($entries.Count + 1) entries)"
}

function Build-Firmware {
    $compileArguments = @(
        "compile",
        "--fqbn", $Board,
        "--build-path", $BuildDir,
        "--warnings", "all",
        $ProjectRoot
    )
    Invoke-Checked "arduino-cli" $compileArguments
    # Refresh all commands, including files reused from the build cache.
    Invoke-Checked "arduino-cli" ($compileArguments + "--only-compilation-database")
    Update-IntelliSenseDatabase
}

function Select-Recording {
    $recordingsRoot = Join-Path $ProjectRoot "recordings"
    $recordings = @()
    if (Test-Path -LiteralPath $recordingsRoot -PathType Container) {
        $recordings = @(Get-ChildItem -LiteralPath $recordingsRoot -Filter *.csv -File -Recurse | Sort-Object FullName)
    }
    if ($recordings.Count -eq 0) {
        throw "No CSV recordings found in recordings/. Use Start Recording first."
    }

    Write-Host "Saved recordings:"
    for ($i = 0; $i -lt $recordings.Count; $i++) {
        Write-Host ("{0}. {1}" -f ($i + 1), $recordings[$i].FullName.Substring($recordingsRoot.Length + 1))
    }
    while ($true) {
        $selection = Read-Host "Choose a recording number (q to cancel)"
        if ($selection -eq "q") { return $null }
        $number = 0
        if ([int]::TryParse($selection, [ref]$number) -and $number -ge 1 -and $number -le $recordings.Count) {
            return $recordings[$number - 1].FullName
        }
        Write-Host "Enter a number from 1 to $($recordings.Count), or q to cancel."
    }
}

function Find-BoardPort {
    if ($Port) {
        return $Port
    }
    if ($env:ECG_PORT) {
        return $env:ECG_PORT
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
    if ($Action -notin @("test", "replay") -and -not (Get-Command arduino-cli -ErrorAction SilentlyContinue)) {
        throw "Install Arduino CLI and restart VS Code first."
    }

    switch ($Action) {
        "setup" {
            if ($PSVersionTable.PSEdition -ne "Desktop" -or $PSVersionTable.PSVersion -lt [version]"5.1") {
                throw "Run Setup Environment from the VS Code task, or run: powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/ecg.ps1 -Action setup"
            }
            $cli = (& arduino-cli version --format json | Out-String) | ConvertFrom-Json
            if ($LASTEXITCODE -ne 0 -or $cli.VersionString -ne $Toolchain.arduinoCli) {
                throw "Install Arduino CLI $($Toolchain.arduinoCli) to match CI."
            }
            $pythonVersion = & python -c "import sys; print(str(sys.version_info.major) + '.' + str(sys.version_info.minor))"
            if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne $Toolchain.python) {
                throw "Put Python $($Toolchain.python) on PATH and restart VS Code."
            }
            Install-Analyzer
            Invoke-Checked "arduino-cli" @("core", "update-index")
            Invoke-Checked "arduino-cli" @(
                "core", "install",
                $Toolchain.core
            )

            if (-not (Test-Path $Python)) {
                Invoke-Checked "python" @("-m", "venv", ".venv")
            }

            Invoke-Checked $Python @("-c", "import sys; assert str(sys.version_info.major) + '.' + str(sys.version_info.minor) == '$($Toolchain.python)', 'Recreate .venv with the required Python version'")
            Invoke-Checked $Python @(
                "-m", "pip", "install", "-r", "requirements-dev.txt"
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
            if (-not (Test-Path $Python)) { throw "Run ECG: Setup Environment first." }
            $SelectedPort = Find-BoardPort
            Invoke-Checked $Python @(
                (Join-Path $ProjectRoot "plot_ecg.py"),
                "--monitor",
                "--port", $SelectedPort
            )
        }

        "plot" {
            if (-not (Test-Path $Python)) {
                throw "Python environment missing. Run ECG: Setup Environment."
            }

            $SelectedPort = Find-BoardPort
            $env:ECG_PORT = $SelectedPort
            Write-Host "Opening graph on $SelectedPort"

            $plotArguments = @((Join-Path $ProjectRoot "plot_ecg.py"), "--range", $Range.ToString([Globalization.CultureInfo]::InvariantCulture))
            if ($RecordPath) { $plotArguments += @("--record", $RecordPath) }
            if ($Headless) { $plotArguments += "--headless" }
            if ($AllowUnvalidatedTiming) { $plotArguments += "--allow-unvalidated-timing" }
            Invoke-Checked $Python $plotArguments
        }

        "test" {
            if (-not (Test-Path $Python)) { throw "Run ECG: Setup Environment first." }
            Invoke-Checked $Python @("-B", "-m", "unittest", "discover", "-s", "tests", "-v")
        }

        "replay" {
            if (-not (Test-Path $Python)) { throw "Run ECG: Setup Environment first." }
            $SelectedRecording = $RecordingPath
            if (-not $SelectedRecording) {
                if ($Headless) { throw "Headless replay requires -RecordingPath." }
                $SelectedRecording = Select-Recording
                if (-not $SelectedRecording) { return }
            }
            if (-not (Test-Path -LiteralPath $SelectedRecording -PathType Leaf)) {
                throw "Provide an existing CSV recording with -RecordingPath."
            }
            $replayArguments = @((Join-Path $ProjectRoot "plot_ecg.py"), "--replay", $SelectedRecording, "--range", $Range.ToString([Globalization.CultureInfo]::InvariantCulture))
            if ($Headless) { $replayArguments += "--headless" }
            if ($AllowUnvalidatedTiming) { $replayArguments += "--allow-unvalidated-timing" }
            Invoke-Checked $Python $replayArguments
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
