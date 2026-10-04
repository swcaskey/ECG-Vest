$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
try {
    if (-not (Test-Path $Python)) { throw "Run ECG: Setup Environment first." }
    foreach ($file in Get-ChildItem $ProjectRoot -Filter *.py -File) {
        # Compile without running the graph or creating __pycache__ files.
        & $Python -c "import pathlib,sys; p=pathlib.Path(sys.argv[1]); compile(p.read_bytes(), str(p), 'exec')" $file.FullName
        if ($LASTEXITCODE -ne 0) { throw "Python syntax failed: $($file.Name)" }
        Write-Host "SYNTAX OK  $($file.Name)"
    }
    # The same formatter validates PowerShell syntax and checks all supported files.
    & (Join-Path $PSScriptRoot "format.ps1") -Check
    if ($LASTEXITCODE -ne 0) { throw "Formatting check failed." }
    Write-Host "Syntax and formatting passed. Arduino/C++ syntax is checked by Build."
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
