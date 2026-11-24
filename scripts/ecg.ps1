param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("build", "upload", "monitor", "plot", "boards", "setup", "test", "replay")]
    [string]$Action,

    [ValidateSet("pcb_usb", "pcb_ble_tx", "ble_receiver")]
    [string]$Firmware,

    [ValidateSet("usb", "ble")]
    [string]$Transport = "usb",

    [string]$Port = "",

    [string]$RecordingPath = "",

    [string]$RecordPath = "",

    [ValidateRange(1, 1000000)]
    [double]$Range,

    [switch]$Headless
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Toolchain = Get-Content (Join-Path $ProjectRoot "toolchain.json") -Raw | ConvertFrom-Json
$Board = $Toolchain.board
$BuildDir = Join-Path $ProjectRoot ".build"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$FirmwareDir = Join-Path $ProjectRoot "firmware"
$FirmwareTargets = [ordered]@{
    pcb_usb      = "PCB Nano for direct USB"
    pcb_ble_tx   = "PCB Nano as BLE transmitter"
    ble_receiver = "Separate Nano as BLE receiver"
}

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
                $staging = (New-Item -ItemType Directory -Path $staging -Force).FullName
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
                if (Test-Path -LiteralPath $staging -PathType Container) {
                    $resolved = (Resolve-Path -LiteralPath $staging).ProviderPath
                    $tempRoot = (Resolve-Path -LiteralPath ([System.IO.Path]::GetTempPath())).ProviderPath.TrimEnd('\')
                    if ($resolved -eq $staging -and (Split-Path -Parent $resolved) -eq $tempRoot) {
                        Remove-Item -LiteralPath $resolved -Recurse -Force
                    }
                }
            }
        }
    }
    # Verify discovery by name, as format.ps1 will use it in a separate process.
    Import-Module PSScriptAnalyzer -RequiredVersion $version -Force -ErrorAction Stop
    Write-Host "READY  PSScriptAnalyzer $version"
}

function Update-IntelliSenseDatabase {
    param([string]$Target)

    $targetBuild = Join-Path $BuildDir $Target
    $databasePath = Join-Path $targetBuild "compile_commands.json"
    $outputPath = Join-Path $targetBuild "intellisense_compile_commands.json"
    $sketchPath = Join-Path (Join-Path $FirmwareDir $Target) "$Target.ino"
    $generatedPath = Join-Path (Join-Path $targetBuild "sketch") "$Target.ino.cpp"

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
}

function Merge-IntelliSenseDatabases {
    $entries = @(
        foreach ($target in $FirmwareTargets.Keys) {
            $path = Join-Path (Join-Path $BuildDir $target) "intellisense_compile_commands.json"
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                $database = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
                @($database)
            }
        }
    )
    $outputPath = Join-Path $BuildDir "intellisense_compile_commands.json"
    $json = ConvertTo-Json -InputObject $entries -Depth 10
    [System.IO.File]::WriteAllText($outputPath, $json + "`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "READY  .build/intellisense_compile_commands.json ($($entries.Count) entries)"
}

function Build-Firmware {
    param([string]$Target)

    Write-Host "Building $Target ($($FirmwareTargets[$Target]))"
    $compileArguments = @(
        "compile",
        "--fqbn", $Board,
        "--build-path", (Join-Path $BuildDir $Target),
        "--libraries", (Join-Path $FirmwareDir "libraries"),
        "--warnings", "all",
        (Join-Path $FirmwareDir $Target)
    )
    Invoke-Checked "arduino-cli" $compileArguments
    # Refresh all commands, including files reused from the build cache.
    Invoke-Checked "arduino-cli" ($compileArguments + "--only-compilation-database")
    Update-IntelliSenseDatabase -Target $Target
}

function Select-Firmware {
    $targets = @($FirmwareTargets.Keys)
    for ($i = 0; $i -lt $targets.Count; $i++) {
        Write-Host "$($i + 1). $($FirmwareTargets[$targets[$i]])"
    }
    $selection = Read-Host "Select firmware to upload (q to cancel)"
    if ($selection -eq "q") { return $null }
    $number = 0
    if (-not [int]::TryParse($selection, [ref]$number) -or $number -lt 1 -or $number -gt $targets.Count) {
        throw "Choose a firmware number from the list."
    }
    return $targets[$number - 1]
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
    param([string]$ExpectedDevice)

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
    $serialPorts = @(
        $result.detected_ports |
            Where-Object { $_.port.protocol -eq "serial" -and $_.port.address }
    )
    $ports = @(
        foreach ($entry in $serialPorts) {
            foreach ($device in $entry.matching_boards) {
                if ($device.fqbn -eq $Board) {
                    $entry.port.address
                }
            }
        }
    )
    $ports = @($ports | Sort-Object -Unique)
    if ($ports.Count -eq 1) {
        return $ports[0]
    }

    if ($ports.Count -eq 0) {
        $ports = @($serialPorts | ForEach-Object { $_.port.address } | Sort-Object -Unique)
        if ($ports.Count -eq 0) {
            throw "No serial ports found. Connect the Nano and run ECG: List Boards."
        }
        Write-Host "No ports identified as Nano ESP32. Detected serial ports: $($ports -join ', ')"
    }
    else {
        Write-Host "Connected Nano ESP32 ports: $($ports -join ', ')"
    }
    $selection = Read-Host "Enter the COM port for $ExpectedDevice (q to cancel)"
    if ($selection -eq "q") { return $null }
    if ($selection -notin $ports) {
        throw "Choose one of the listed ports."
    }
    return $selection
}

try {
    if ($Headless -and $Action -ne "replay") {
        throw "Headless mode requires -Action replay."
    }
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
            $targets = @($FirmwareTargets.Keys)
            if ($Firmware) { $targets = @($Firmware) }
            foreach ($target in $targets) {
                Build-Firmware -Target $target
            }
            Merge-IntelliSenseDatabases
        }

        "upload" {
            $target = $Firmware
            if (-not $target) { $target = Select-Firmware }
            if (-not $target) { return }
            $device = $FirmwareTargets[$target]
            $SelectedPort = Find-BoardPort -ExpectedDevice $device
            if (-not $SelectedPort) { return }
            Write-Host "Uploading $target to $SelectedPort ($device). Close the plot/monitor first."
            Build-Firmware -Target $target
            Merge-IntelliSenseDatabases

            Invoke-Checked "arduino-cli" @(
                "upload",
                "--port", $SelectedPort,
                "--fqbn", $Board,
                "--input-dir", (Join-Path $BuildDir $target),
                (Join-Path $FirmwareDir $target)
            )
        }

        { $_ -in @("monitor", "plot") } {
            if (-not (Test-Path $Python)) {
                throw "Python environment missing. Run ECG: Setup Environment."
            }

            $device = if ($Transport -eq "ble") { $FirmwareTargets.ble_receiver } else { $FirmwareTargets.pcb_usb }
            $SelectedPort = Find-BoardPort -ExpectedDevice $device
            if (-not $SelectedPort) { return }
            Write-Host "Opening $SelectedPort ($device)"

            $plotArguments = @((Join-Path $ProjectRoot "plot_ecg.py"), "--port", $SelectedPort, "--transport", $Transport)
            if ($Action -eq "monitor") { $plotArguments += "--monitor" }
            if ($PSBoundParameters.ContainsKey("Range")) {
                $plotArguments += @("--range", $Range.ToString([Globalization.CultureInfo]::InvariantCulture))
            }
            if ($RecordPath) { $plotArguments += @("--record", $RecordPath) }
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
            $replayArguments = @((Join-Path $ProjectRoot "plot_ecg.py"), "--replay", $SelectedRecording)
            if ($PSBoundParameters.ContainsKey("Range")) {
                $replayArguments += @("--range", $Range.ToString([Globalization.CultureInfo]::InvariantCulture))
            }
            if ($Headless) { $replayArguments += "--headless" }
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
