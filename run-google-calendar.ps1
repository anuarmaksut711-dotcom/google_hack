param(
    [string]$Project = 'project-e25873c6-e889-40bb-86d',
    [ValidateRange(1024,65535)][int]$Port = 8981,
    [string]$ClientFile,
    [switch]$Background
)
$ErrorActionPreference = 'Stop'
$campusPythonCandidates = @(
    (Join-Path $PSScriptRoot '.venv-calendar/Scripts/python.exe'),
    (Join-Path $PSScriptRoot '.venv-agent/Scripts/python.exe'),
    (Join-Path $PSScriptRoot '../campuspulse/recovery_app/.venv-release-test/Scripts/python.exe')
)
$campusPython = $campusPythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $campusPython) { throw 'Install Python 3.12 and .[local-calendar] into .venv-calendar first.' }
$campusArguments = @('-X','utf8',(Join-Path $PSScriptRoot 'scripts/run_google_calendar.py'),'--project',$Project,'--port',[string]$Port)
if ($ClientFile) { $campusArguments += @('--client-file',(Resolve-Path -LiteralPath $ClientFile).Path) }
if ($Background) {
    $campusLogs = Join-Path $env:LOCALAPPDATA ('CampusPulse/google-calendar/' + $Project)
    New-Item -ItemType Directory -Force -Path $campusLogs | Out-Null
    $campusQuoted = @($campusArguments | ForEach-Object { '"' + $_ + '"' })
    $campusProcess = Start-Process -FilePath $campusPython -ArgumentList $campusQuoted -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $campusLogs 'server.stdout.log') `
        -RedirectStandardError (Join-Path $campusLogs 'server.stderr.log') -PassThru
    $campusProcess.Id | Set-Content -LiteralPath (Join-Path $campusLogs 'server.pid')
    Write-Host ('Local Calendar: https://127.0.0.1:' + $Port + '/google-calendar')
} else {
    & $campusPython @campusArguments
    exit $LASTEXITCODE
}
