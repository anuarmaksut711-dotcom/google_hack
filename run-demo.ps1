param(
    [ValidateRange(1024, 65535)][int]$Port = 8947,
    [string]$ChatTarget,
    [switch]$PromptGeminiKey
)
$ErrorActionPreference = 'Stop'
$demoPython = Join-Path $PSScriptRoot '.venv-web/Scripts/python.exe'
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.venv-demo/Scripts/python.exe')) {
    $demoPython = Join-Path $PSScriptRoot '.venv-demo/Scripts/python.exe'
}
if (-not (Test-Path -LiteralPath $demoPython)) {
    throw 'Python 3.12 environment missing. Create .venv-demo and install this project with the demo extra: python -m pip install .[demo]'
}
$launchArguments = @('-I', '-S', '-B', '-X', 'utf8', (Join-Path $PSScriptRoot 'scripts/run_demo.py'), '--demo', '--port', [string]$Port)
if ($ChatTarget) {
    $targetPath = (Resolve-Path -LiteralPath $ChatTarget -ErrorAction Stop).Path
    $launchArguments += @('--chat-target', $targetPath)
}
if ($PromptGeminiKey -and -not $ChatTarget) { throw '-PromptGeminiKey requires -ChatTarget.' }
$previousKey = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'Process')
$keyChanged = $false
$demoExitCode = 1
Push-Location $PSScriptRoot
try {
    if ($PromptGeminiKey) {
        try { $targetSettings = Get-Content -LiteralPath $targetPath -Raw | ConvertFrom-Json }
        catch { throw 'Chat settings must be valid JSON without credentials.' }
        if ($targetSettings.provider -ne 'gemini_api') { throw '-PromptGeminiKey is only for provider gemini_api.' }
        $secureKey = Read-Host 'Gemini API key (hidden; used only for this process)' -AsSecureString
        try {
            $plainKey = [System.Net.NetworkCredential]::new('', $secureKey).Password.Trim()
            if (-not $plainKey) { throw 'Gemini API key is empty.' }
            [Environment]::SetEnvironmentVariable('GEMINI_API_KEY', $plainKey, 'Process')
            $keyChanged = $true
        }
        finally { $plainKey = $null; $secureKey.Dispose() }
    }
    & $demoPython @launchArguments
    $demoExitCode = $LASTEXITCODE
}
finally {
    if ($keyChanged) { [Environment]::SetEnvironmentVariable('GEMINI_API_KEY', $previousKey, 'Process') }
    Pop-Location
}
exit $demoExitCode
