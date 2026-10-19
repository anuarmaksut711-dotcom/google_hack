param(
    [ValidateRange(1024,65535)][int]$Port = 8959,
    [Parameter(Mandatory)][ValidateRange(1,2147483647)][int]$UserId,
    [string]$UrlFile = '.campuspulse-local/moodle-calendar-url.txt',
    [string]$ChatTarget,
    [switch]$PromptGeminiKey,
    [switch]$Background
)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$moodlePreviousGeminiKey = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'Process')
$moodleGeminiKeyChanged = $false
try {
    $moodleCalendarUrlFile = (Resolve-Path -LiteralPath $UrlFile).Path
    $moodleLaunchArguments = @('-I','-S','-B','-X','utf8','./scripts/run_moodle_calendar.py',
        '--url-file',$moodleCalendarUrlFile,'--user-id',[string]$UserId,'--port',[string]$Port)
    if ($PromptGeminiKey -and -not $ChatTarget) { throw '-PromptGeminiKey requires -ChatTarget.' }
    if ($ChatTarget) {
        $moodleChatTargetPath = (Resolve-Path -LiteralPath $ChatTarget).Path
        $moodleLaunchArguments += @('--chat-target',$moodleChatTargetPath)
    }
    if ($PromptGeminiKey) {
        $moodleChatSettings = Get-Content -LiteralPath $moodleChatTargetPath -Raw | ConvertFrom-Json
        if ($moodleChatSettings.provider -ne 'gemini_api') { throw 'Hidden API key input requires provider gemini_api.' }
        $moodleSecureGeminiKey = Read-Host 'Gemini API key (hidden input; only in this process memory)' -AsSecureString
        try {
            $moodlePlainGeminiKey = [System.Net.NetworkCredential]::new('', $moodleSecureGeminiKey).Password.Trim()
            if (-not $moodlePlainGeminiKey) { throw 'Gemini API key is empty.' }
            [Environment]::SetEnvironmentVariable('GEMINI_API_KEY', $moodlePlainGeminiKey, 'Process')
            $moodleGeminiKeyChanged = $true
        }
        finally { $moodlePlainGeminiKey = $null; $moodleSecureGeminiKey.Dispose() }
    }
    if ($Background) {
        $moodleBackgroundRoot = Join-Path $PSScriptRoot '.campuspulse-local'
        $moodleBackgroundArgs = @($moodleLaunchArguments | ForEach-Object { '"' + $_ + '"' })
        $moodleBackgroundProcess = Start-Process -FilePath (Join-Path $PSScriptRoot '.venv-agent/Scripts/python.exe') `
            -ArgumentList $moodleBackgroundArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $moodleBackgroundRoot ('calendar-' + $Port + '.stdout.log')) `
            -RedirectStandardError (Join-Path $moodleBackgroundRoot ('calendar-' + $Port + '.stderr.log')) -PassThru
        $moodleBackgroundProcess.Id | Set-Content -LiteralPath (Join-Path $moodleBackgroundRoot ('calendar-' + $Port + '.pid'))
        Write-Host ('Server started in background. Open https://127.0.0.1:' + $Port + '/local')
        Write-Host 'You can close this PowerShell window. The server runs separately.'
        $moodleCalendarExit = 0
    }
    else {
        & './.venv-agent/Scripts/python.exe' @moodleLaunchArguments
        $moodleCalendarExit = $LASTEXITCODE
    }
}
finally {
    if ($moodleGeminiKeyChanged) { [Environment]::SetEnvironmentVariable('GEMINI_API_KEY', $moodlePreviousGeminiKey, 'Process') }
    Pop-Location
}
exit $moodleCalendarExit
