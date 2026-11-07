$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
try {
    if (-not (Test-Path $Python)) { throw "Run ECG: Setup Environment first." }
    $pythonFiles = @(Get-ChildItem $ProjectRoot -Filter *.py -File)
    foreach ($directory in @("ecg", "src", "tests")) {
        $sourcePath = Join-Path $ProjectRoot $directory
        if (Test-Path -LiteralPath $sourcePath -PathType Container) {
            $pythonFiles += @(Get-ChildItem -LiteralPath $sourcePath -Filter *.py -File -Recurse | Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' })
        }
    }
    foreach ($file in $pythonFiles) {
        # Compile without running the graph or creating __pycache__ files.
        & $Python -c "import pathlib,sys; p=pathlib.Path(sys.argv[1]); compile(p.read_bytes(), str(p), 'exec')" $file.FullName
        if ($LASTEXITCODE -ne 0) { throw "Python syntax failed: $($file.Name)" }
        Write-Host "SYNTAX OK  $($file.FullName.Substring($ProjectRoot.Length + 1))"
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
