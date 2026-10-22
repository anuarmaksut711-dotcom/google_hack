param(
    [switch]$ReplaceWebhook,
    [switch]$Check,
    [string]$TokenFile = (Join-Path $PSScriptRoot 'token.txt'),
    [switch]$FileOnly
)
$ErrorActionPreference = 'Stop'
$VerbosePreference = 'SilentlyContinue'
$DebugPreference = 'SilentlyContinue'
$projectId = 'project-e25873c6-e889-40bb-86d'
$regionId = 'us-central1'
$origin = 'https://campuspulse-403025736340.us-central1.run.app'
$tokenSecret = 'campuspulse-telegram-bot-token'
$webhookSecretName = 'campuspulse-telegram-webhook-secret'

function Stop-Setup([string]$Message) {
    $failure = [InvalidOperationException]::new($Message)
    $failure.Data['CampusPulseSafeMessage'] = $true
    throw $failure
}
function Find-Gcloud {
    $command = Get-Command gcloud.cmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    foreach ($root in @($env:LOCALAPPDATA, ${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not $root) { continue }
        $candidate = Join-Path $root 'Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    Stop-Setup 'Не найден Google Cloud CLI. Установите его и повторите настройку.'
}
function Invoke-Gcloud([string[]]$Arguments, [string]$InputValue, [switch]$AllowFailure) {
    # PowerShell 5.1 treats native stderr warnings as ErrorRecords.
    # Do not display native errors that might contain secret stdin.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        if ($PSBoundParameters.ContainsKey('InputValue')) {
            $raw = $InputValue | & $gcloudPath --configuration=campuspulse-t11 @Arguments "--project=$projectId" --quiet --format=json 2>$null
        } else {
            $raw = & $gcloudPath --configuration=campuspulse-t11 @Arguments "--project=$projectId" --quiet --format=json 2>$null
        }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($code -ne 0) {
        if ($AllowFailure) { return $null }
        Stop-Setup "Операция Google Cloud завершилась с кодом $code. Проверьте вход: & '$gcloudPath' --configuration=campuspulse-t11 auth login"
    }
    if ($raw) {
        try { return (($raw -join "`n") | ConvertFrom-Json -ErrorAction Stop) }
        catch { Stop-Setup 'Google Cloud CLI вернул неожиданный ответ вместо JSON. Проверьте установку CLI.' }
    }
}
function Gcp([string[]]$Arguments) { return Invoke-Gcloud -Arguments $Arguments }
function Read-Service { return Gcp @('run','services','describe','campuspulse',"--region=$regionId") }
function Assert-Serving([string]$Expected) {
    $traffic = @((Read-Service).status.traffic | Where-Object percent -GT 0)
    if ($traffic.Count -ne 1 -or $traffic[0].percent -ne 100 -or $traffic[0].revisionName -ne $Expected) {
        Stop-Setup 'За время настройки изменилась версия сайта. Запустите скрипт заново.'
    }
}
function Add-SecretVersion([string]$Name, [string]$Value, [string]$Identity) {
    $secret = Invoke-Gcloud -Arguments @('secrets','describe',$Name) -AllowFailure
    if (-not $secret) {
        $null = Gcp @('secrets','create',$Name,'--replication-policy=automatic')
    }
    $null = Gcp @('secrets','add-iam-policy-binding',$Name,"--member=serviceAccount:$Identity",'--role=roles/secretmanager.secretAccessor')
    # Standard input keeps the value out of command-line arguments and files.
    $result = Invoke-Gcloud -Arguments @('secrets','versions','add',$Name,'--data-file=-') -InputValue $Value
    $version = $result.name.Split('/')[-1]
    if ($version -notmatch '^[1-9][0-9]*$') { Stop-Setup 'Не удалось проверить версию секрета.' }
    return $version
}

$secureToken = $null; $token = $null; $pointer = [IntPtr]::Zero; $webhookSecret = $null
$tokenFileUsed = $false
$setupFailed = $false
$stage = 'поиск Google Cloud CLI'
try {
    $gcloudPath = Find-Gcloud
    $stage = 'проверка доступа к Cloud Run'
    $service = Read-Service
    $traffic = @($service.status.traffic | Where-Object percent -GT 0)
    if ($traffic.Count -ne 1 -or $traffic[0].percent -ne 100) { Stop-Setup 'Сначала завершите текущий деплой сайта.' }
    $baseRevision = $traffic[0].revisionName
    if ($service.status.latestReadyRevisionName -ne $baseRevision) { Stop-Setup 'Обнаружена другая кандидатная версия. Сначала завершите её деплой.' }
    $support = @($service.spec.template.spec.containers[0].env | Where-Object name -EQ 'CAMPUSPULSE_TELEGRAM_SUPPORT')
    if ($support.Count -ne 1 -or $support[0].value -ne 'v1') { Stop-Setup 'Сначала задеплойте версию сайта с Telegram-уведомлениями.' }
    $identity = $service.spec.template.spec.serviceAccountName
    $image = $service.spec.template.spec.containers[0].image
    if ($identity -ne "campuspulse-calendar@$projectId.iam.gserviceaccount.com") { Stop-Setup 'Неожиданная серверная учётная запись.' }
    if ($Check) {
        Write-Output 'Проверка пройдена: Google Cloud CLI найден, доступ к Cloud Run есть, Telegram поддерживается. Можно запускать без -Check.'
        return
    }
    $stage = 'чтение токена BotFather'
    if (Test-Path -LiteralPath $TokenFile -PathType Leaf) {
        $token = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $TokenFile).ProviderPath, [Text.Encoding]::UTF8).Trim()
        $tokenFileUsed = -not [string]::IsNullOrWhiteSpace($token)
    }
    if (-not $tokenFileUsed) {
        if ($FileOnly) { Stop-Setup 'Откройте token.txt, вставьте только токен бота, сохраните файл и снова запустите подключение.' }
        $secureToken = Read-Host 'Вставьте токен бота из BotFather (ввод скрыт)' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Trim()
    }
    if ($token -notmatch '^[0-9]{5,16}:[A-Za-z0-9_-]{20,128}$') { Stop-Setup 'Неверный формат токена BotFather.' }
    $stage = 'проверка токена в Telegram'
    try {
        $me = Invoke-RestMethod -Method Post -Uri ("https://api.telegram.org/bot" + $token + '/getMe') -TimeoutSec 20
        $existing = Invoke-RestMethod -Method Post -Uri ("https://api.telegram.org/bot" + $token + '/getWebhookInfo') -TimeoutSec 20
    } catch { Stop-Setup 'Telegram не подтвердил токен. Проверьте токен и соединение.' }
    if ($me.ok -ne $true -or $me.result.is_bot -ne $true -or $me.result.username -notmatch '^[A-Za-z][A-Za-z0-9_]{4,31}$') { Stop-Setup 'Telegram не подтвердил бота.' }
    if ($existing.result.url -and $existing.result.url -ne "$origin/telegram/webhook" -and -not $ReplaceWebhook) {
        Stop-Setup 'У бота уже настроен другой webhook. Используйте отдельного бота или повторите с -ReplaceWebhook, если хотите перенести именно его.'
    }
    $randomBytes = [byte[]]::new(32)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($randomBytes) } finally { $generator.Dispose() }
    $webhookSecret = [Convert]::ToBase64String($randomBytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    $stage = 'сохранение секретов в Secret Manager'
    $tokenVersion = Add-SecretVersion $tokenSecret $token $identity
    $webhookVersion = Add-SecretVersion $webhookSecretName $webhookSecret $identity
    Assert-Serving $baseRevision
    $suffix = 'telegram-ready-' + [DateTime]::UtcNow.ToString('yyyyMMddHHmmss')
    $revision = 'campuspulse-' + $suffix
    $stage = 'подключение секретов к Cloud Run'
    $null = Gcp @('run','deploy','campuspulse',"--region=$regionId","--image=$image", "--revision-suffix=$suffix",
        '--no-traffic','--tag=telegram-ready',
        "--update-secrets=CAMPUSPULSE_TELEGRAM_BOT_TOKEN=${tokenSecret}:$tokenVersion,CAMPUSPULSE_TELEGRAM_WEBHOOK_SECRET=${webhookSecretName}:$webhookVersion",
        "--update-env-vars=CAMPUSPULSE_TELEGRAM_BOT_USERNAME=$($me.result.username)")
    $stage = 'проверка новой версии сервера'
    $candidate = ((Read-Service).status.traffic | Where-Object tag -EQ 'telegram-ready').url
    $health = Invoke-RestMethod "$candidate/health" -TimeoutSec 45
    if ($health.status -ne 'ok') { Stop-Setup 'Новая версия не прошла проверку готовности.' }
    Assert-Serving $baseRevision
    $stage = 'переключение сервера на новую версию'
    $null = Gcp @('run','services','update-traffic','campuspulse',"--region=$regionId","--to-revisions=$revision=100")
    $stage = 'проверка webhook сервера'
    $probe = Invoke-RestMethod -Method Post -Uri "$origin/telegram/webhook" -ContentType 'application/json' -Body '{"update_id":0}' `
        -Headers @{'X-Telegram-Bot-Api-Secret-Token'=$webhookSecret} -TimeoutSec 30
    if ($probe.ok -ne $true) { Stop-Setup 'Сервер не подтвердил webhook.' }
    $stage = 'регистрация webhook в Telegram'
    try {
        $body = @{url="$origin/telegram/webhook";secret_token=$webhookSecret;allowed_updates=@('message');max_connections=5} | ConvertTo-Json
        $registered = Invoke-RestMethod -Method Post -Uri ("https://api.telegram.org/bot" + $token + '/setWebhook') `
            -ContentType 'application/json' -Body $body -TimeoutSec 30
        $verified = Invoke-RestMethod -Method Post -Uri ("https://api.telegram.org/bot" + $token + '/getWebhookInfo') -TimeoutSec 20
    } catch { Stop-Setup 'Не удалось зарегистрировать webhook в Telegram. Повторите настройку.' }
    if ($registered.ok -ne $true -or $verified.result.url -ne "$origin/telegram/webhook") { Stop-Setup 'Telegram не подтвердил webhook.' }
    if ($tokenFileUsed -and [IO.File]::ReadAllText($TokenFile, [Text.Encoding]::UTF8).Trim() -ceq $token) {
        [IO.File]::WriteAllText($TokenFile, '', [Text.Encoding]::ASCII)
    }
    Write-Output "Бот @$($me.result.username) настроен."
    Write-Output "Откройте $origin/settings, нажмите «Подключить Telegram», затем Start у бота и подтвердите подключение на сайте."
} catch {
    $setupFailed = $true
    # Do not print HTTP exception objects: Telegram URLs contain the bot token.
    $safeMessage = if ($_.Exception.Data['CampusPulseSafeMessage']) { $_.Exception.Message } else { 'Код ошибки: ' + $_.Exception.GetType().Name + '. Токен не выводится.' }
    Write-Error ("Настройка остановлена на этапе «$stage»: $safeMessage") -ErrorAction Continue
} finally {
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if ($secureToken) { $secureToken.Dispose() }
    $token = $null; $webhookSecret = $null; $body = $null
}
if ($setupFailed) { exit 1 }
