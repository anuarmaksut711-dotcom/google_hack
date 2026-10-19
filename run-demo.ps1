param([int]$Port = 8947)
$ErrorActionPreference = 'Stop'
$demoPython = Join-Path $PSScriptRoot '.venv-web/Scripts/python.exe'
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.venv-demo/Scripts/python.exe')) {
    $demoPython = Join-Path $PSScriptRoot '.venv-demo/Scripts/python.exe'
}
if (-not (Test-Path -LiteralPath $demoPython)) {
    throw 'Python 3.12 environment missing. Create .venv-demo and install this project with the demo extra: python -m pip install .[demo]'
}
Push-Location $PSScriptRoot
try { & $demoPython -I -S -B -X utf8 (Join-Path $PSScriptRoot 'scripts/run_demo.py') --demo --port $Port }
finally { Pop-Location }
exit $LASTEXITCODE
