$ErrorActionPreference = 'Stop'
$productRoot = Split-Path $PSScriptRoot -Parent
Set-Location $productRoot
$env:UV_PYTHON_INSTALL_DIR = Join-Path $productRoot '.python'
$env:UV_CACHE_DIR = Join-Path $productRoot '.uv-cache'
$uvPath = Join-Path $productRoot '.tools/uv/bin/uv.exe'
foreach ($component in @('agent', 'mcp', 'web')) {
    foreach ($target in @('windows', 'x86_64-manylinux_2_28')) {
        $label = if ($target -eq 'windows') { 'windows' } else { 'linux' }
        & $uvPath --no-config pip compile pyproject.toml --extra $component `
            --python-version 3.12.13 --python-platform $target --generate-hashes `
            --no-header --output-file "requirements/$component-$label.lock" --quiet
        if ($LASTEXITCODE -ne 0) { throw "Resolution failed: $component/$label" }
    }
}
& $uvPath --no-config pip compile pyproject.toml --python-version 3.12.13 `
    --universal --generate-hashes --no-header --output-file requirements/core.lock --quiet
if ($LASTEXITCODE -ne 0) { throw 'Core resolution failed' }
& $uvPath --no-config pip compile requirements/dev.in --python-version 3.12.13 `
    --universal --generate-hashes --no-header --output-file requirements/dev.lock --quiet
if ($LASTEXITCODE -ne 0) { throw 'Dev resolution failed' }
& $uvPath --no-config pip compile requirements/bootstrap.in --python-version 3.10 `
    --universal --generate-hashes --no-header --output-file requirements/bootstrap.lock --quiet
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap resolution failed' }
& $uvPath --no-config pip compile pyproject.toml --all-extras --python-version 3.12.13 `
    --universal --constraint requirements/agent-linux.lock --constraint requirements/dev.lock `
    --generate-hashes --no-header --no-annotate --output-file requirements/test.lock --quiet
if ($LASTEXITCODE -ne 0) { throw 'Combined test environment resolution failed' }
