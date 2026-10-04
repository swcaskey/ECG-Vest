param([switch]$Check)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ClangFormat = Join-Path $ProjectRoot ".venv\Scripts\clang-format.exe"

Set-Location $ProjectRoot

function Get-RelativeName {
    param([string]$Path)
    return $Path.Substring($ProjectRoot.Length + 1)
}

try {
    if (!(Test-Path $Python) -or !(Test-Path $ClangFormat)) {
        throw "Install the Python environment and formatters first."
    }

    $toolchain = Get-Content (Join-Path $ProjectRoot "toolchain.json") -Raw | ConvertFrom-Json
    Import-Module PSScriptAnalyzer -RequiredVersion $toolchain.psScriptAnalyzer -ErrorAction Stop

    $rootFiles = @(Get-ChildItem $ProjectRoot -File)

    $pythonFiles = @(
        $rootFiles | Where-Object { $_.Extension -eq ".py" }
    )

    $cppFiles = @(
        $rootFiles | Where-Object {
            $_.Extension -in @(".ino", ".c", ".cpp", ".h", ".hpp")
        }
    )

    $psFiles = @(
        Get-ChildItem $PSScriptRoot -Filter *.ps1 -File -Recurse
    )

    $allFiles = @($pythonFiles) + @($cppFiles) + @($psFiles)
    $before = @{}

    Write-Host "File types and locations:"
    Write-Host "  Python:     *.py in the project root"
    Write-Host "  Arduino/C:  *.ino, *.c, *.cpp, *.h, *.hpp in the project root"
    Write-Host "  PowerShell: scripts/**/*.ps1"
    Write-Host "  JSON/YAML:  not formatted"
    Write-Host ""

    foreach ($file in $allFiles) {
        $before[$file.FullName] = (
            Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256
        ).Hash
    }

    # Reject PowerShell parse errors before formatting.
    foreach ($file in $psFiles) {
        $tokens = $null
        $parseErrors = $null

        [System.Management.Automation.Language.Parser]::ParseFile(
            $file.FullName,
            [ref]$tokens,
            [ref]$parseErrors
        ) | Out-Null

        if ($parseErrors.Count -gt 0) {
            $details = ($parseErrors | ForEach-Object {
                    $_.Message
                }) -join "; "

            throw "$(Get-RelativeName $file.FullName): $details"
        }
    }

    $changed = 0
    $unchanged = 0
    $utf8 = New-Object System.Text.UTF8Encoding($false)

    foreach ($file in $allFiles) {
        $name = Get-RelativeName $file.FullName
        $target = $file.FullName
        if ($Check) {
            $tempPath = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString() + $file.Extension)
            Copy-Item -LiteralPath $target -Destination $tempPath
            $target = $tempPath
        }

        switch ($file.Extension.ToLowerInvariant()) {
            ".py" {
                # Quiet suppresses success chatter; errors remain visible.
                & $Python -m black --quiet $target
                if ($LASTEXITCODE -ne 0) {
                    throw "Python formatter failed: $name"
                }
            }

            ".ps1" {
                $original = [System.IO.File]::ReadAllText($target)
                $formatted = (Invoke-Formatter -ScriptDefinition $original).Replace("`r`n", "`n")

                if ($formatted -ne $original) {
                    [System.IO.File]::WriteAllText(
                        $target, $formatted, $utf8
                    )
                }
            }

            default {
                & $ClangFormat -i --style=LLVM `
                    --assume-filename=source.cpp $target

                if ($LASTEXITCODE -ne 0) {
                    throw "C/C++ formatter failed: $name"
                }
            }
        }

        $after = (
            Get-FileHash -LiteralPath $target -Algorithm SHA256
        ).Hash

        if ($Check) { Remove-Item -LiteralPath $tempPath; $tempPath = $null }
        if ($after -ne $before[$file.FullName]) {
            if ($Check) { Write-Host "NEEDS FORMAT  $name" } else { Write-Host "CHANGED    $name" }
            $changed++
        }
        else {
            Write-Host "UNCHANGED  $name"
            $unchanged++
        }
    }

    Write-Host ""
    if ($Check) {
        Write-Host "Checked: $changed need formatting, $unchanged unchanged."
        if ($changed -gt 0) { throw "Run ECG: Format Code, review changes, then check again." }
    }
    else {
        Write-Host "Finished: $changed changed, $unchanged unchanged."
    }
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    if (-not $Check) { Write-Host "Files processed before this error may already be formatted." }
    exit 1
}
finally {
    if ($tempPath -and (Test-Path -LiteralPath $tempPath)) { Remove-Item -LiteralPath $tempPath }
}
