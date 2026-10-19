#requires -Version 7.0
# Dependency resolution only: does not install packages, start servers or run tests.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$uv = Join-Path $projectRoot '.tools/uv/bin/uv.exe'
if (-not (Test-Path -LiteralPath $uv)) { throw 'The project uv executable is required.' }
$previousCache = [Environment]::GetEnvironmentVariable('UV_CACHE_DIR', 'Process')
$previousPython = [Environment]::GetEnvironmentVariable('UV_PYTHON_INSTALL_DIR', 'Process')
Push-Location $projectRoot
try {
    $env:UV_CACHE_DIR = Join-Path $projectRoot '.uv-cache'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot '.python'
    & $uv --no-config pip compile pyproject.toml --extra cloud --python-version 3.12.13 `
        --python-platform x86_64-manylinux_2_28 --generate-hashes --no-header `
        --output-file requirements/cloud-linux.lock --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Cloud dependency resolution failed.' }
}
finally {
    [Environment]::SetEnvironmentVariable('UV_CACHE_DIR', $previousCache, 'Process')
    [Environment]::SetEnvironmentVariable('UV_PYTHON_INSTALL_DIR', $previousPython, 'Process')
    Pop-Location
}
