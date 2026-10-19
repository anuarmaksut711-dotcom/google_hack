<#
.SYNOPSIS
Read-only CampusPulse AI environment and Google Cloud access checks.
.DESCRIPTION
Reads file names, tool versions and existing credential metadata. With a project
and active CLI account, calls only projects describe, services list --enabled,
and billing projects describe. Never installs, logs in, enables APIs or reads keys.

Exit codes: 0 = requested checks passed; 1 = warnings only; 2 = no source found;
3 = gcloud unavailable; 4 = project ID or active CLI credentials missing;
5 = a cloud access/readiness check failed; 6 = Python/uv environment incomplete.
If several blockers exist, the lowest
blocking code wins; every blocker is still printed. ADC existence is not an
authentication test. Project access does not prove application integration.
.EXAMPLE
pwsh -File ./setup/Check-Environment.ps1
.EXAMPLE
pwsh -File ./setup/Check-Environment.ps1 -ProjectId my-gcp-project -Location europe-west4
#>
[CmdletBinding()]
param(
    [string]$ProjectId = $env:GOOGLE_CLOUD_PROJECT,
    [string]$Location = $env:GOOGLE_CLOUD_LOCATION,
    [string]$GcloudPath
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$results = [System.Collections.Generic.List[object]]::new()

function Write-Check {
    param(
        [ValidateSet('PASS', 'WARN', 'BLOCKED')][string]$Status,
        [string]$Message,
        [int]$ExitCode = 0
    )
    $results.Add([pscustomobject]@{ Status = $Status; ExitCode = $ExitCode })
    Write-Host "[$Status] $Message"
}

function Invoke-ReadOnlyCommand {
    param([string]$Command, [string[]]$Arguments)
    try {
        # Capture all native output: gcloud errors can contain account identifiers.
        $ErrorActionPreference = 'Continue'
        $outputLines = & $Command @Arguments 2>&1
        $commandExitCode = $LASTEXITCODE
        return [pscustomobject]@{
            Success = ($commandExitCode -eq 0)
            Text = (($outputLines | ForEach-Object { $_.ToString() }) -join "`n").Trim()
        }
    }
    catch {
        return [pscustomobject]@{ Success = $false; Text = '' }
    }
}

Write-Host 'CampusPulse AI: проверка окружения (только чтение)'
Write-Host "Рабочая папка: $workspace"

foreach ($toolName in @('git', 'node', 'npm')) {
    $toolLabel = if ($toolName -in @('node', 'npm')) { "$toolName (optional для Python-приложения)" } else { $toolName }
    $lookupName = if ($toolName -eq 'npm') { 'npm.cmd' } else { $toolName }
    $tool = Get-Command $lookupName -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($tool) {
        $versionResult = Invoke-ReadOnlyCommand -Command $tool.Source -Arguments @('--version')
        if ($versionResult.Success -and $versionResult.Text -match '^((?:git version )?v?\d+\.\d+[^\r\n]*)') {
            Write-Check PASS "${toolLabel}: $($Matches[1])"
        }
        else {
            Write-Check WARN "Не удалось получить версию $toolLabel."
        }
    }
    else {
        Write-Check WARN "$toolLabel отсутствует в PATH."
    }
}

$pyprojectPath = Join-Path $workspace 'pyproject.toml'
if (Test-Path -LiteralPath $pyprojectPath -PathType Leaf) {
    $pyprojectText = Get-Content -LiteralPath $pyprojectPath -Raw
    if ($pyprojectText -match '(?m)^\s*requires-python\s*=\s*"(?<pythonSpec>[^"]+)"' -and
        ($Matches.pythonSpec -replace '\s', '') -eq '>=3.12,<3.13') {
        Write-Check PASS 'pyproject.toml найден и требует Python >=3.12,<3.13.'
    }
    else { Write-Check BLOCKED 'Ожидаемое требование Python >=3.12,<3.13 в pyproject.toml не подтверждено.' 6 }
}
else { Write-Check BLOCKED 'Обязательный pyproject.toml отсутствует.' 2 }

# Use explicit repository paths only; never execute python/python3 from PATH or
# a WindowsApps app-execution alias. --version imports no application code.
$pythonExecutables = [ordered]@{
    'managed Python' = '.python/cpython-3.12.13-windows-x86_64-none/python.exe'
    '.venv-agent' = '.venv-agent/Scripts/python.exe'
    '.venv-mcp' = '.venv-mcp/Scripts/python.exe'
    '.venv-web' = '.venv-web/Scripts/python.exe'
    '.venv-build' = '.venv-build/Scripts/python.exe'
}
foreach ($pythonEntry in $pythonExecutables.GetEnumerator()) {
    $pythonPath = Join-Path $workspace $pythonEntry.Value
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        Write-Check BLOCKED "$($pythonEntry.Key): Python не найден в ожидаемом локальном пути." 6
        continue
    }
    $pythonVersion = Invoke-ReadOnlyCommand $pythonPath @('--version')
    if ($pythonVersion.Success -and $pythonVersion.Text -match '^Python (3\.12\.\d+)\s*$') {
        Write-Check PASS "$($pythonEntry.Key): Python $($Matches[1]); это проверка интерпретатора, не зависимостей."
    }
    else { Write-Check BLOCKED "$($pythonEntry.Key): рабочий Python 3.12 не подтверждён." 6 }
}

$uvPath = Join-Path $workspace '.tools/uv/bin/uv.exe'
if (Test-Path -LiteralPath $uvPath -PathType Leaf) {
    $uvVersion = Invoke-ReadOnlyCommand $uvPath @('--version')
    if ($uvVersion.Success -and $uvVersion.Text -match '^uv (\d+\.\d+\.\d+)(?:\s|$)') {
        if ($Matches[1] -eq '0.13.1') { Write-Check PASS "Локальный uv: $($Matches[1])." }
        else { Write-Check WARN "Локальный uv: $($Matches[1]); при настройке использовался 0.13.1." }
    }
    else { Write-Check BLOCKED 'Локальный uv найден, но его версия не подтверждена.' 6 }
}
else { Write-Check BLOCKED 'Локальный uv отсутствует: .tools/uv/bin/uv.exe.' 6 }

# Read the literal service list through the PowerShell AST; never execute the
# deployment script. SafeGetValue rejects executable/dynamic expressions.
$deploymentServices = @()
$preparePath = Join-Path $workspace 'deploy/prepare-gcp.ps1'
try {
    $prepareTokens = $null
    $prepareErrors = $null
    $prepareAst = [System.Management.Automation.Language.Parser]::ParseFile($preparePath, [ref]$prepareTokens, [ref]$prepareErrors)
    if ($prepareErrors.Count) { throw 'Deployment script parse error' }
    $apiAssignment = $prepareAst.Find({
        param($astNode)
        $astNode -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $astNode.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $astNode.Left.VariablePath.UserPath -eq 'apiNames'
    }, $true)
    $apiExpression = $apiAssignment.Right.Expression.SubExpression.Statements[0].PipelineElements[0].Expression
    $deploymentServices = @($apiExpression.SafeGetValue())
    if (-not $deploymentServices.Count -or @($deploymentServices | Where-Object { $_ -isnot [string] -or $_ -notmatch '^[a-z][a-z0-9.-]+\.googleapis\.com$' }).Count) {
        throw 'Invalid literal API list'
    }
    Write-Check PASS "Прочитан точный набор API из deploy/prepare-gcp.ps1: $($deploymentServices.Count)."
}
catch { Write-Check BLOCKED 'Не удалось безопасно прочитать apiNames из deploy/prepare-gcp.ps1; список обязательных API не подтверждён.' 5 }

# Inspect names only, to a bounded depth. Do not traverse dependency, credential,
# setup, hidden or linked directories, and do not open any source or secret file.
$excludedDirectories = @('setup', 'node_modules', 'venv', 'env', 'dist', 'build',
    'coverage', '__pycache__', 'secrets', 'credentials')
$sourceExtensions = @('.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.go',
    '.java', '.cs', '.rb', '.php', '.rs', '.kt', '.dart', '.vue', '.svelte')
$pending = [System.Collections.Generic.Queue[object]]::new()
$pending.Enqueue([pscustomobject]@{ Path = $workspace; Depth = 0 })
$sourceFound = $false
$scanIncomplete = $false
$visitedDirectories = 0
while ($pending.Count -gt 0 -and -not $sourceFound) {
    $directory = $pending.Dequeue()
    $visitedDirectories++
    if ($visitedDirectories -gt 300) { $scanIncomplete = $true; break }
    try { $entries = @(Get-ChildItem -LiteralPath $directory.Path -Force) }
    catch { $scanIncomplete = $true; continue }
    foreach ($entry in $entries) {
        if ($entry.Name.StartsWith('.')) { continue }
        if ($entry.PSIsContainer) {
            if ($entry.Name -in $excludedDirectories -or $entry.LinkType -in @('SymbolicLink', 'Junction')) { continue }
            if ($directory.Depth -lt 4) {
                $pending.Enqueue([pscustomobject]@{ Path = $entry.FullName; Depth = $directory.Depth + 1 })
            }
            else { $scanIncomplete = $true }
        }
        elseif ($entry.Extension.ToLowerInvariant() -in $sourceExtensions) {
            $sourceFound = $true
            break
        }
    }
}
if ($sourceFound) {
    Write-Check PASS 'Найдены файлы с расширениями исходного кода; содержимое и интеграции не проверялись.'
}
elseif ($scanIncomplete) {
    Write-Check BLOCKED 'Исходники не найдены в ограниченной проверке; часть каталогов недоступна или глубже 4 уровней. Нужен путь к приложению.' 2
}
else {
    Write-Check BLOCKED 'Исходники приложения не найдены. Нужен существующий репозиторий или путь к коду.' 2
}

if ([string]::IsNullOrWhiteSpace($GcloudPath)) {
    $gcloudCommand = Get-Command gcloud.cmd, gcloud -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($gcloudCommand) { $GcloudPath = $gcloudCommand.Source }
    elseif ($env:LOCALAPPDATA) {
        $localSdk = Join-Path $env:LOCALAPPDATA 'Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd'
        if (Test-Path -LiteralPath $localSdk -PathType Leaf) { $GcloudPath = $localSdk }
    }
}
$gcloudAvailable = -not [string]::IsNullOrWhiteSpace($GcloudPath) -and (Test-Path -LiteralPath $GcloudPath -PathType Leaf)
$cliAuthPresent = $false
if ($gcloudAvailable) {
    $sdkVersion = Invoke-ReadOnlyCommand $GcloudPath @('version', '--format=json', '--quiet')
    if ($sdkVersion.Success) {
        try {
            $sdkDetails = $sdkVersion.Text | ConvertFrom-Json
            $coreVersion = $sdkDetails.'Google Cloud SDK'
            if (-not $coreVersion) { throw 'Missing SDK version' }
            Write-Check PASS "Google Cloud SDK: $coreVersion"
        }
        catch { Write-Check WARN 'gcloud запускается, но его версию не удалось разобрать.' }
    }
    else {
        $gcloudAvailable = $false
        Write-Check BLOCKED 'gcloud найден, но его запуск пока не подтверждён. Повторите после завершения установки SDK.' 3
    }
    if ($gcloudAvailable) {
        # gcloud's value(status) formatter renders ACTIVE as '*'; use JSON data.
        $authResult = Invoke-ReadOnlyCommand $GcloudPath @('auth', 'list', '--filter=status:ACTIVE', '--format=json(status)', '--quiet')
        if ($authResult.Success) {
            try {
                $activeAccounts = @($authResult.Text | ConvertFrom-Json)
                $cliAuthPresent = @($activeAccounts | Where-Object { $_.status -eq 'ACTIVE' }).Count -gt 0
            }
            catch { $cliAuthPresent = $false }
        }
        if ($cliAuthPresent) {
            Write-Check PASS 'Найдена активная учётная запись CLI. Это только наличие записи; действительность доступа проверяется ниже.'
        }
        else { Write-Check BLOCKED 'Активная авторизация gcloud CLI не подтверждена. Идентификаторы и ошибки авторизации скрыты.' 4 }
    }
}
else { Write-Check BLOCKED 'gcloud не найден в PATH или стандартной папке LocalAppData. Можно передать -GcloudPath.' 3 }

$adcPath = $env:GOOGLE_APPLICATION_CREDENTIALS
if ([string]::IsNullOrWhiteSpace($adcPath)) {
    $adcDirectory = $env:CLOUDSDK_CONFIG
    if ([string]::IsNullOrWhiteSpace($adcDirectory) -and $env:APPDATA) { $adcDirectory = Join-Path $env:APPDATA 'gcloud' }
    if ($adcDirectory) { $adcPath = Join-Path $adcDirectory 'application_default_credentials.json' }
}
$adcExists = -not [string]::IsNullOrWhiteSpace($adcPath) -and (Test-Path -LiteralPath $adcPath -PathType Leaf)
if ($adcExists) { Write-Check PASS "Файл ADC существует: true; путь: $adcPath. Содержимое и работоспособность не проверялись." }
else { Write-Check WARN 'Файл локальных ADC существует: false. Локальный доступ приложения к Google APIs не подтверждён.' }

if ([string]::IsNullOrWhiteSpace($Location)) { Write-Check WARN 'Регион не задан (-Location). Региональные и платные API не вызываются.' }
else { Write-Check PASS "Регион указан: $Location. Доступность модели и развёртывание в нём не проверялись." }

if ([string]::IsNullOrWhiteSpace($ProjectId)) {
    Write-Check BLOCKED 'Не задан GCP Project ID (-ProjectId или GOOGLE_CLOUD_PROJECT). Облачные проверки пропущены.' 4
}
elseif ($gcloudAvailable -and $cliAuthPresent) {
    $projectResult = Invoke-ReadOnlyCommand $GcloudPath @('projects', 'describe', $ProjectId, '--format=value(lifecycleState)', '--quiet')
    if (-not $projectResult.Success) {
        Write-Check BLOCKED 'Чтение указанного GCP проекта не удалось. Проверьте Project ID, права, авторизацию и сеть.' 5
    }
    elseif ($projectResult.Text -ne 'ACTIVE') {
        Write-Check BLOCKED 'Проект доступен для чтения, но его состояние ACTIVE не подтверждено.' 5
    }
    else {
        Write-Check PASS "Реальный запрос projects describe успешен: $ProjectId, состояние ACTIVE."
        $servicesResult = Invoke-ReadOnlyCommand $GcloudPath @('services', 'list', '--enabled', "--project=$ProjectId", '--format=value(config.name)', '--quiet')
        if ($servicesResult.Success) {
            $enabledServices = @($servicesResult.Text -split '\r?\n' | Where-Object { $_ })
            Write-Check PASS "Список включённых API прочитан: $($enabledServices.Count)."
            # prepare-gcp.ps1 provisions and verifies its entire apiNames list.
            # No service in that list is optional for that deployment procedure.
            foreach ($service in $deploymentServices) {
                if ($service -in $enabledServices) { Write-Check PASS "API required для deploy/prepare-gcp.ps1 включён: $service" }
                else { Write-Check BLOCKED "API required для deploy/prepare-gcp.ps1 не включён: $service" 5 }
            }
            Write-Check PASS 'Optional API вне набора prepare-gcp.ps1 не требуются и не проверяются; сервисы не включались.'
        }
        else { Write-Check BLOCKED 'Список включённых API прочитать не удалось; готовность обязательных облачных сервисов не подтверждена.' 5 }

        $billingResult = Invoke-ReadOnlyCommand $GcloudPath @('billing', 'projects', 'describe', $ProjectId, '--format=value(billingEnabled)', '--quiet')
        if ($billingResult.Success -and $billingResult.Text -eq 'True') { Write-Check PASS 'Биллинг проекта включён (только чтение статуса; расходы не создавались).' }
        elseif ($billingResult.Success -and $billingResult.Text -eq 'False') { Write-Check BLOCKED 'Биллинг проекта отключён; облачное развёртывание не готово.' 5 }
        else { Write-Check WARN 'Статус биллинга не подтверждён: нет доступа, API недоступен или ответ не распознан.' }
    }
}
else { Write-Check BLOCKED 'Облачные проверки пропущены: требуется работающий gcloud и активная авторизация CLI.' 4 }

$blockers = @($results | Where-Object Status -eq 'BLOCKED')
$warnings = @($results | Where-Object Status -eq 'WARN')
$exitCode = if ($blockers.Count) { ($blockers.ExitCode | Sort-Object | Select-Object -First 1) } elseif ($warnings.Count) { 1 } else { 0 }
Write-Host ''
Write-Host "Итог: PASS=$(@($results | Where-Object Status -eq 'PASS').Count); WARN=$($warnings.Count); BLOCKED=$($blockers.Count); exit=$exitCode"
Write-Host 'Проверка окружения не подтверждает интеграцию приложения, вызов Gemini или развёртывание Agent Engine.'
exit $exitCode
