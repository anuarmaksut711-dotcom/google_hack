param(
    [ValidateRange(1024,65535)][int]$Port = 8961,
    [string]$ClientIdFile = '.campuspulse-local/google-signin-client-id.txt',
    [string]$ChatTarget = '.campuspulse-local/chat-moodle-gemini.json',
    [switch]$PromptGeminiKey,
    [switch]$Background
)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$campusPreviousKey = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'Process')
$campusKeyChanged = $false
try {
    $campusClientPath = (Resolve-Path -LiteralPath $ClientIdFile).Path
    $campusChatPath = (Resolve-Path -LiteralPath $ChatTarget).Path
    $campusArguments = @('-I','-S','-B','-X','utf8','./scripts/run_multiuser.py',
        '--client-id-file',$campusClientPath,'--port',[string]$Port,'--chat-target',$campusChatPath)
    if ($PromptGeminiKey) {
        $campusSecureKey = Read-Host 'Gemini API key (hidden input; process memory only)' -AsSecureString
        try {
            $campusPlainKey = [System.Net.NetworkCredential]::new('', $campusSecureKey).Password.Trim()
            if (-not $campusPlainKey) { throw 'Gemini API key is empty.' }
            [Environment]::SetEnvironmentVariable('GEMINI_API_KEY', $campusPlainKey, 'Process')
            $campusKeyChanged = $true
        }
        finally { $campusPlainKey = $null; $campusSecureKey.Dispose() }
    }
    if (-not [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'Process')) {
        throw 'Use -PromptGeminiKey to enter the key without saving it.'
    }
    if ($Background) {
        $campusRuntime = Join-Path $PSScriptRoot '.campuspulse-local'
        $campusQuotedArguments = @($campusArguments | ForEach-Object { '"' + $_ + '"' })
        $campusProcess = Start-Process -FilePath (Join-Path $PSScriptRoot '.venv-agent/Scripts/python.exe') `
            -ArgumentList $campusQuotedArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $campusRuntime ('multiuser-' + $Port + '.stdout.log')) `
            -RedirectStandardError (Join-Path $campusRuntime ('multiuser-' + $Port + '.stderr.log')) -PassThru
        $campusProcess.Id | Set-Content -LiteralPath (Join-Path $campusRuntime ('multiuser-' + $Port + '.pid'))
        Write-Host ('Server started separately. Open https://127.0.0.1:' + $Port + '/login')
        $campusExit = 0
    }
    else {
        & './.venv-agent/Scripts/python.exe' @campusArguments
        $campusExit = $LASTEXITCODE
    }
}
finally {
    if ($campusKeyChanged) { [Environment]::SetEnvironmentVariable('GEMINI_API_KEY', $campusPreviousKey, 'Process') }
    Pop-Location
}
exit $campusExit
