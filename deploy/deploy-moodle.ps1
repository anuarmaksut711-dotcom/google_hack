#requires -Version 7.0
<# Deploy the private LMS worker and a durable Cloud Scheduler trigger.
   Prepare is offline; Deploy requires an existing image and pinned settings.
   No student password/token is accepted on the command line. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InputsPath,
    [Parameter(Mandatory)][string]$SettingsPath,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_-]+:[1-9][0-9]*$')][string]$SettingsSecret,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9.-]+/[a-z0-9_./-]+@sha256:[a-f0-9]{64}$')][string]$ImageDigest,
    [ValidateSet('Prepare', 'Deploy')][string]$Mode = 'Prepare'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$cfg = Get-Content -Raw -LiteralPath $InputsPath | ConvertFrom-Json -AsHashtable
$settings = Get-Content -Raw -LiteralPath $SettingsPath | ConvertFrom-Json -AsHashtable
$project = $cfg.project_id
$number = $cfg.project_number
$account = $cfg.deployer_account
$region = $cfg.web_cloud_run_region
if ($project -notmatch '^[a-z][a-z0-9-]{4,28}[a-z0-9]$' -or $number -notmatch '^[1-9][0-9]+$' -or
    $account -notmatch '^[^\s@]+@[^\s@]+$' -or $region -notmatch '^[a-z]+(?:-[a-z]+)+[0-9]$') {
    throw 'Explicit project, number, deployer and region required.'
}
if ($settings.application.mode -ne 'cloud' -or $settings.application.project_id -ne $project -or
    $settings.application.firestore_database -ne $cfg.firestore_database -or
    -not $settings.ContainsKey('moodle') -or $null -eq $settings.moodle) { throw 'Matching cloud Moodle settings required.' }
$m = $settings.moodle
$origin = "https://campuspulse-moodle-$number.$region.run.app"
if ($m.site_url -cne 'https://lms.astanait.edu.kz' -or $m.sync_origin -cne $origin -or
    $m.expected_user_id -isnot [long] -and $m.expected_user_id -isnot [int] -or $m.expected_user_id -le 0) {
    throw 'Explicit AITU Moodle site, positive user ID and deterministic private origin required.'
}
$identityPattern = '^[a-z][a-z0-9-]{4,28}[a-z0-9]@' + [regex]::Escape($project) + '\.iam\.gserviceaccount\.com$'
$runtimeIdentities = @($settings.application.identities.Values)
foreach ($identity in @($m.sync_service_account, $m.scheduler_service_account)) {
    if ($identity -notmatch $identityPattern -or $identity -in $runtimeIdentities -or $identity -eq $cfg.build_service_account) {
        throw 'Separate same-project worker/scheduler identities required.'
    }
}
if ($m.sync_service_account -eq $m.scheduler_service_account) { throw 'Worker and scheduler identities must differ.' }
if ($m.token_secret_reference -notmatch ('^projects/' + [regex]::Escape($project) + '/secrets/([A-Za-z0-9_-]+)/versions/([1-9][0-9]*)$')) {
    throw 'Pinned same-project Moodle Secret Manager reference required.'
}
$tokenName, $tokenVersion = $Matches[1], $Matches[2]
$settingsName, $settingsVersion = $SettingsSecret.Split(':')
if ($tokenName -eq $settingsName) { throw 'Token and configuration must be separate secrets.' }
if ($m.interval_minutes -notin @(5,10,15,20,30,60) -or $m.stale_after_minutes -lt $m.interval_minutes) {
    throw 'Use a supported interval (5,10,15,20,30,60 minutes) and a longer stale window.'
}
if (-not $ImageDigest.StartsWith("$($cfg.artifact_registry_location)-docker.pkg.dev/$project/$($cfg.artifact_registry_repository)/")) {
    throw 'Image must belong to the configured repository.'
}
$cron = if ($m.interval_minutes -eq 60) { '0 * * * *' } else { "*/$($m.interval_minutes) * * * *" }
$plan = [ordered]@{status='prepared_not_deployed'; service='campuspulse-moodle'; origin=$origin;
    worker=$m.sync_service_account; scheduler=$m.scheduler_service_account; schedule=$cron;
    timezone='Etc/UTC'; token_secret_reference=$m.token_secret_reference; settings_secret=$SettingsSecret;
    image=$ImageDigest; note='Run deploy-cloud.ps1 with these Moodle settings first; no LMS password required.'}
if ($Mode -eq 'Prepare') { $plan | ConvertTo-Json -Depth 5; return }
function Gcp([string[]]$Arguments) {
    $raw = & gcloud @Arguments "--project=$project" "--account=$account" --quiet --format=json 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Cloud command failed: $($Arguments[0..([Math]::Min(2,$Arguments.Count-1))] -join ' ')" }
    if ($raw) { return ($raw -join "`n" | ConvertFrom-Json -AsHashtable) }
}
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) { throw 'Google Cloud CLI required.' }
$cli = Gcp @('config','list','--all')
if (($cli['auth'] -and ($cli.auth['impersonate_service_account'] -or $cli.auth['credential_file_override'])) -or
    [Environment]::GetEnvironmentVariable('CLOUDSDK_AUTH_ACCESS_TOKEN')) { throw 'Unexpected CLI credential override.' }
$actual = Gcp @('projects','describe',$project)
if ([string]$actual.projectNumber -ne $number -or $actual.lifecycleState -ne 'ACTIVE') { throw 'Project identity/state mismatch.' }
$null = Gcp @('services','enable','cloudscheduler.googleapis.com','run.googleapis.com','secretmanager.googleapis.com','iamcredentials.googleapis.com')
$null = Gcp @('firestore','databases','describe',"--database=$($cfg.firestore_database)")
$identities = @(Gcp @('iam','service-accounts','list') | ForEach-Object { $_.email })
foreach ($identity in @($m.sync_service_account, $m.scheduler_service_account)) {
    if ($identity -notin $identities) {
        $null = Gcp @('iam','service-accounts','create',$identity.Split('@')[0])
    }
    $actualIdentity = Gcp @('iam','service-accounts','describe',$identity)
    if ($actualIdentity['disabled'] -eq $true) { throw 'Service identity is disabled.' }
    $deployerMember = if ($account.EndsWith('.gserviceaccount.com')) { "serviceAccount:$account" } else { "user:$account" }
    $null = Gcp @('iam','service-accounts','add-iam-policy-binding',$identity,
        "--member=$deployerMember",'--role=roles/iam.serviceAccountUser','--condition=None')
}
foreach ($secret in @(@{name=$tokenName;version=$tokenVersion}, @{name=$settingsName;version=$settingsVersion})) {
    $version = Gcp @('secrets','versions','describe',$secret.version,"--secret=$($secret.name)")
    if ($version.state -ne 'ENABLED') { throw 'Secret version disabled or missing.' }
    $null = Gcp @('secrets','add-iam-policy-binding',$secret.name,
        "--member=serviceAccount:$($m.sync_service_account)",'--role=roles/secretmanager.secretAccessor','--condition=None')
}
# Verify the non-secret settings version is the exact file used for this plan.
# Never read the Moodle token version in the deployment process.
$readbackDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) '.validation'
$null = New-Item -ItemType Directory -Path $readbackDirectory -Force
$settingsReadback = Join-Path $readbackDirectory ('moodle-settings-' + [guid]::NewGuid().ToString('N') + '.json')
try {
    $null = Gcp @('secrets','versions','access',$settingsVersion,"--secret=$settingsName","--out-file=$settingsReadback")
    if ((Get-FileHash -LiteralPath $settingsReadback -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $SettingsPath -Algorithm SHA256).Hash) { throw 'Pinned settings version differs from SettingsPath.' }
} finally {
    if (Test-Path -LiteralPath $settingsReadback) { Remove-Item -LiteralPath $settingsReadback }
}
# The worker reads/writes only the configured database; the token is never stored there.
$dbResource = "projects/$project/databases/$($cfg.firestore_database)"
$dbCondition = "expression=resource.name == '$dbResource' || resource.name.startsWith('$dbResource/'),title=moodle-configured-database"
$null = Gcp @('projects','add-iam-policy-binding',$project,"--member=serviceAccount:$($m.sync_service_account)",
    '--role=roles/datastore.user',"--condition=$dbCondition")
$null = Gcp @('run','deploy','campuspulse-moodle',"--region=$region","--image=$ImageDigest",
    "--service-account=$($m.sync_service_account)",'--execution-environment=gen2','--port=8080',
    '--cpu=1','--memory=512Mi','--concurrency=4','--min-instances=0','--max-instances=2','--timeout=120',
    '--ingress=all','--invoker-iam-check','--no-allow-unauthenticated',"--add-custom-audiences=$origin",
    "--set-secrets=/var/run/campuspulse-settings/settings.json=${settingsName}:$settingsVersion,/var/run/campuspulse-moodle/token=${tokenName}:$tokenVersion",
    '--set-env-vars=CAMPUSPULSE_ROLE=moodle-sync,CAMPUSPULSE_CLOUD_SETTINGS=/var/run/campuspulse-settings/settings.json,CAMPUSPULSE_MOODLE_TOKEN_FILE=/var/run/campuspulse-moodle/token')
foreach ($caller in @($settings.application.identities.backend_service_account, $m.scheduler_service_account)) {
    $null = Gcp @('run','services','add-iam-policy-binding','campuspulse-moodle',"--region=$region",
        "--member=serviceAccount:$caller",'--role=roles/run.invoker','--condition=None')
}
$policy = Gcp @('run','services','get-iam-policy','campuspulse-moodle',"--region=$region")
if (@($policy.bindings | Where-Object { $_.members -contains 'allUsers' -or $_.members -contains 'allAuthenticatedUsers' }).Count) {
    throw 'Worker has public IAM access; remove public bindings before using it.'
}
$jobs = @(Gcp @('scheduler','jobs','list',"--location=$region"))
$operation = if (@($jobs | Where-Object { $_.name.EndsWith('/campuspulse-moodle-sync') }).Count) { 'update' } else { 'create' }
$null = Gcp @('scheduler','jobs',$operation,'http','campuspulse-moodle-sync',"--location=$region",
    "--schedule=$cron",'--time-zone=Etc/UTC',"--uri=$origin/internal/moodle/sync",'--http-method=POST',
    '--headers=Content-Type=application/json','--message-body={}',"--oidc-service-account-email=$($m.scheduler_service_account)",
    "--oidc-token-audience=$origin",'--attempt-deadline=120s','--max-retry-attempts=0','--max-retry-duration=0s')
$job = Gcp @('scheduler','jobs','describe','campuspulse-moodle-sync',"--location=$region")
$service = Gcp @('run','services','describe','campuspulse-moodle',"--region=$region")
if ($job.schedule -ne $cron -or $job.httpTarget.uri -ne "$origin/internal/moodle/sync" -or
    $job.httpTarget.oidcToken.audience -ne $origin -or $job.httpTarget.oidcToken.serviceAccountEmail -ne $m.scheduler_service_account -or
    $service.spec.template.spec.serviceAccountName -ne $m.sync_service_account) { throw 'Deployment readback mismatch.' }
if ($job.state -eq 'PAUSED') { $null = Gcp @('scheduler','jobs','resume','campuspulse-moodle-sync',"--location=$region") }
[ordered]@{status='deployed_real_account_verification_pending';origin=$origin;schedule=$cron;image=$ImageDigest} | ConvertTo-Json
