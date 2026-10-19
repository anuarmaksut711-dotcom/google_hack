#requires -Version 7.0
# Local Windows x64 setup; no cloud, login, tests, demo or device access.
[CmdletBinding()]
param([switch]$IncludeVoice)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$root = Split-Path -Parent $PSScriptRoot
$logRoot = Join-Path $root '.validation/setup'
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$uvExe = Join-Path $root '.tools/uv/bin/uv.exe'
$savedEnvironment = @{}
foreach ($name in @('UV_PYTHON_INSTALL_DIR', 'UV_CACHE_DIR')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
function Invoke-Step([string]$Name, [string]$File, [string[]]$Arguments) {
    $log = Join-Path $logRoot "$stamp-$Name.log"
    Write-Host "[SETUP] $Name"
    & $File @Arguments *> $log
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Get-Content -LiteralPath $log -Tail 30 | ForEach-Object { Write-Host $_ }
        throw "$Name failed (exit $code). Full log: $log"
    }
}
function Get-ComponentPython([string]$Name, [string]$BasePython) {
    $directory = Join-Path $root ".venv-$Name"
    $python = Join-Path $directory 'Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $directory)) {
        Invoke-Step "venv-$Name" $uvExe @('--no-config', 'venv', '--python', $BasePython, $directory)
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Existing environment is incomplete; left unchanged: $directory"
    }
    Invoke-Step "python-$Name" $python @('-I', '-B', '-c',
        'import sys; print(sys.version); assert sys.version_info[:2] == (3, 12)')
    return $python
}
Push-Location $root
try {
    if (-not [OperatingSystem]::IsWindows() -or [Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') {
        throw 'This installer requires Windows x64.'
    }
    $null = New-Item -ItemType Directory -Path $logRoot -Force
    foreach ($file in @('pyproject.toml', 'requirements/dev.lock', 'requirements/smoke.py',
        'requirements/agent-windows.lock', 'requirements/mcp-windows.lock', 'requirements/web-windows.lock')) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $file) -PathType Leaf)) { throw "Required file missing: $file" }
    }
    if ($IncludeVoice) {
        foreach ($file in @('requirements/voice-client-local-windows.lock', 'requirements/voice-client-audio-windows.lock')) {
            if (-not (Test-Path -LiteralPath (Join-Path $root $file) -PathType Leaf)) { throw "Required file missing: $file" }
        }
    }
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $root '.python'
    $env:UV_CACHE_DIR = Join-Path $root '.uv-cache'
    if (-not (Test-Path -LiteralPath $uvExe -PathType Leaf)) {
        $archive = Join-Path $logRoot "$stamp-uv-0.13.1.zip"
        $url = 'https://github.com/astral-sh/uv/releases/download/0.13.1/uv-x86_64-pc-windows-msvc.zip'
        Write-Host '[SETUP] Download official uv 0.13.1 archive'
        Invoke-WebRequest -Uri $url -OutFile $archive -TimeoutSec 120
        $expected = '3b2661a463a5baea7f5076ea2b25ab71d77f36a6c0d809e5350651792f80a672'
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expected) {
            throw "uv archive SHA256 mismatch; archive retained for inspection: $archive"
        }
        $null = New-Item -ItemType Directory -Path (Split-Path -Parent $uvExe) -Force
        Expand-Archive -LiteralPath $archive -DestinationPath (Split-Path -Parent $uvExe) -Force
    }
    $version = & $uvExe --version 2>&1
    if ($LASTEXITCODE -ne 0 -or ($version -join '') -notmatch '^uv 0\.13\.1(?:\s|$)') {
        throw 'Existing uv must be version 0.13.1; it was not replaced.'
    }
    Invoke-Step 'python-install' $uvExe @('--no-config', 'python', 'install', '3.12.13', '--no-bin', '--no-registry')
    $basePython = Join-Path $env:UV_PYTHON_INSTALL_DIR 'cpython-3.12.13-windows-x86_64-none/python.exe'
    if (-not (Test-Path -LiteralPath $basePython -PathType Leaf)) { throw 'Pinned Python installation not found.' }
    $buildPython = Get-ComponentPython 'build' $basePython
    $devLock = Join-Path $root 'requirements/dev.lock'
    Invoke-Step 'build-dependencies' $uvExe @('--no-config', 'pip', 'sync', '--python', $buildPython, '--require-hashes', $devLock)
    $wheelDirectory = Join-Path $logRoot "$stamp-wheels"
    Invoke-Step 'build-wheel' $buildPython @('-I', '-B', '-m', 'build', '--wheel', '--no-isolation', '--outdir', $wheelDirectory)
    $wheels = @(Get-ChildItem -LiteralPath $wheelDirectory -Filter '*.whl' -File)
    if ($wheels.Count -ne 1) { throw 'Build must produce exactly one product wheel.' }
    $wheel = $wheels[0].FullName
    foreach ($component in @('agent', 'mcp', 'web')) {
        $python = Get-ComponentPython $component $basePython
        $lock = Join-Path $root "requirements/$component-windows.lock"
        Invoke-Step "$component-dependencies" $uvExe @('--no-config', 'pip', 'sync', '--python', $python, '--require-hashes', $lock)
        Invoke-Step "$component-dev" $uvExe @('--no-config', 'pip', 'install', '--python', $python, '--require-hashes', '-r', $devLock)
        Invoke-Step "$component-wheel" $uvExe @('--no-config', 'pip', 'install', '--python', $python, '--no-deps', '--reinstall', $wheel)
        Invoke-Step "$component-check" $uvExe @('--no-config', 'pip', 'check', '--python', $python)
        Invoke-Step "$component-smoke" $python @('-I', '-B', (Join-Path $root 'requirements/smoke.py'), $component)
    }
    if ($IncludeVoice) {
        $python = Get-ComponentPython 'voice' $basePython
        $localLock = Join-Path $root 'requirements/voice-client-local-windows.lock'
        $audioLock = Join-Path $root 'requirements/voice-client-audio-windows.lock'
        Invoke-Step 'voice-dependencies' $uvExe @('--no-config', 'pip', 'sync', '--python', $python, '--require-hashes', $localLock)
        Invoke-Step 'voice-audio' $uvExe @('--no-config', 'pip', 'install', '--python', $python, '--require-hashes', '-r', $audioLock)
        Invoke-Step 'voice-wheel' $uvExe @('--no-config', 'pip', 'install', '--python', $python, '--no-deps', '--reinstall', $wheel)
        Invoke-Step 'voice-check' $uvExe @('--no-config', 'pip', 'check', '--python', $python)
    }
    Write-Host "[PASS] Local component environments ready. Logs and wheel: $logRoot"
}
catch {
    Write-Host "[FAIL] $($_.Exception.Message)"
    exit 1
}
finally {
    foreach ($name in $savedEnvironment.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process') }
    Pop-Location
}
