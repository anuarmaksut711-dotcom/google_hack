#requires -Version 7.0
<# Build and deploy the three explicit Cloud Run compositions. Prepare is offline.
   No Gemini/Workspace functional tests are run by this script. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InputsPath,
    [Parameter(Mandatory)][string]$SettingsPath,
    [ValidateSet('Prepare', 'Deploy')][string]$Mode = 'Prepare',
    [switch]$MoodleOnly,
    [ValidatePattern('^$|^[a-zA-Z0-9_-]+:[1-9][0-9]*$')][string]$GeminiKeySecret,
    [ValidatePattern('^$|^[a-z0-9.-]+/[a-z0-9_./-]+@sha256:[a-f0-9]{64}$')][string]$ImageDigest
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$cfg = Get-Content -Raw -LiteralPath $InputsPath | ConvertFrom-Json -AsHashtable
$settingsFile = (Resolve-Path -LiteralPath $SettingsPath).Path
$settings = Get-Content -Raw -LiteralPath $settingsFile | ConvertFrom-Json -AsHashtable
$project = $cfg.project_id
$account = $cfg.deployer_account
$number = $cfg.project_number
if ($project -notmatch '^[a-z][a-z0-9-]{4,28}[a-z0-9]$' -or $number -notmatch '^[1-9][0-9]+$' -or
    $account -notmatch '^[^\s@]+@[^\s@]+$') { throw 'Fill the project ID, project number and deployer account.' }
if ($settings.application.mode -ne 'cloud' -or $settings.application.project_id -ne $project) { throw 'Cloud settings/project mismatch.' }
$services = @(
    @{ role='mcp'; name='campuspulse-mcp'; region=$cfg.mcp_cloud_run_region; identity=$cfg.mcp_service_account; origin=$settings.mcp_origin },
    @{ role='agent'; name='campuspulse-agent'; region=$cfg.agent_region; identity=$cfg.agent_service_account; origin=$settings.agent_origin },
    @{ role='backend'; name='campuspulse-web'; region=$cfg.web_cloud_run_region; identity=$cfg.backend_service_account; origin=$settings.backend_origin }
)
foreach ($service in $services) {
    if ($service.region -notmatch '^[a-z]+(?:-[a-z]+)+[0-9]$' -or
        $service.identity -cne $settings.application.identities["$($service.role)_service_account"] -or
        $service.identity -notmatch ('^[a-z][a-z0-9-]{4,28}[a-z0-9]@' + [regex]::Escape($project) + '\.iam\.gserviceaccount\.com$')) {
        throw 'Explicit matching runtime identities/regions required.'
    }
    $expected = "https://$($service.name)-$number.$($service.region).run.app"
    if ($service.origin -cne $expected) { throw "Use the deterministic service origin: $expected" }
}
if (@($services.identity | Select-Object -Unique).Count -ne 3) { throw 'Distinct runtime identities required.' }
if ($MoodleOnly) {
    if (-not $settings.ContainsKey('moodle') -or $null -eq $settings.moodle) { throw '-MoodleOnly requires Moodle settings.' }
    $services = @($services | Where-Object { $_.role -eq 'backend' })
}
if ($settings.application.firestore_database -ne $cfg.firestore_database -or
    $settings.application.agent_region -ne $cfg.agent_region -or
    $settings.application.web_cloud_run_region -ne $cfg.web_cloud_run_region -or
    $settings.application.mcp_cloud_run_region -ne $cfg.mcp_cloud_run_region) { throw 'Application/deployment resource mismatch.' }
if ($settings.google_signin_client_id -notmatch '^[A-Za-z0-9._-]+\.apps\.googleusercontent\.com$' -or
    (-not $MoodleOnly -and ($settings.working_hours -isnot [array] -or $settings.working_hours.Count -eq 0))) { throw 'Fill Google sign-in client ID and explicit working hours.' }
if ($settings.chat.provider -notin @('gemini_api', 'vertex')) { throw 'Select an explicit model provider.' }
if ($settings.chat.provider -eq 'gemini_api' -and -not $GeminiKeySecret -and -not $MoodleOnly) { throw 'Pass a pinned Secret Manager reference with -GeminiKeySecret name:version.' }
if ($settings.chat.provider -eq 'vertex' -and $GeminiKeySecret) { throw 'Vertex uses platform credentials; omit GeminiKeySecret.' }
if ($ImageDigest -and -not $ImageDigest.StartsWith("$($cfg.artifact_registry_location)-docker.pkg.dev/$project/$($cfg.artifact_registry_repository)/")) {
    throw 'Image must belong to the explicitly configured repository.'
}
$secretName = 'campuspulse-cloud-settings'
if ($Mode -eq 'Prepare') {
    [ordered]@{ status='prepared_not_deployed'; services=$services; settings_secret=$secretName;
        model_provider=$settings.chat.provider; key_secret_reference=$GeminiKeySecret;
        image=$(if ($ImageDigest) { $ImageDigest } else { 'build_from_allowlisted_source' });
        prerequisite='prepare-gcp.ps1 + iam.ps1 -CloudRunRuntime; OAuth; resource sharing';
        tests='not_run' } | ConvertTo-Json -Depth 8
    return
}
function Gcp([string[]]$Arguments) {
    $raw = & gcloud @Arguments "--project=$project" "--account=$account" --quiet --format=json
    if ($LASTEXITCODE -ne 0) { throw "Cloud command failed: $($Arguments[0..([Math]::Min(2,$Arguments.Count-1))] -join ' ')" }
    if ($raw) { return ($raw -join "`n" | ConvertFrom-Json -AsHashtable) }
}
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) { throw 'Google Cloud CLI is required.' }
$cli = Gcp @('config', 'list', '--all')
if (($cli['auth'] -and ($cli.auth['impersonate_service_account'] -or $cli.auth['credential_file_override'])) -or
    [Environment]::GetEnvironmentVariable('CLOUDSDK_AUTH_ACCESS_TOKEN')) { throw 'Unexpected CLI credential override.' }
$actual = Gcp @('projects', 'describe', $project)
if ([string]$actual.projectNumber -ne $number -or $actual.lifecycleState -ne 'ACTIVE') { throw 'Project identity/state mismatch.' }
$null = Gcp @('firestore', 'databases', 'describe', "--database=$($cfg.firestore_database)")
foreach ($service in $services) {
    $null = Gcp @('iam', 'service-accounts', 'describe', $service.identity)
}
$null = Gcp @('secrets', 'describe', $secretName)
if ($GeminiKeySecret) {
    $keyParts = $GeminiKeySecret.Split(':')
    $keyVersion = Gcp @('secrets', 'versions', 'describe', $keyParts[1], "--secret=$($keyParts[0])")
    if ($keyVersion.state -ne 'ENABLED') { throw 'Gemini key secret version is not enabled.' }
}
if (-not $ImageDigest) {
    # Upload only source assets and pinned dependency/build files. Local settings,
    # screenshots, credentials, copied repositories and virtualenvs cannot enter.
    $stage = Join-Path $root ('.validation/cloud-build-' + [guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $stage -Force
    $files = @('pyproject.toml', 'requirements/LICENSE', 'requirements/dev.lock',
        'requirements/cloud-linux.lock', 'deploy/Dockerfile.cloud')
    $files += @(Get-ChildItem -LiteralPath (Join-Path $root 'src') -File -Recurse |
        Where-Object { $_.Extension -in @('.py', '.js', '.png', '.txt') -and $_.FullName -notmatch '[\\/]__pycache__[\\/]' } |
        ForEach-Object { [System.IO.Path]::GetRelativePath($root, $_.FullName) })
    foreach ($relative in $files) {
        $destination = Join-Path $stage $relative
        $null = New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force
        Copy-Item -LiteralPath (Join-Path $root $relative) -Destination $destination
    }
    $tag = "$($cfg.artifact_registry_location)-docker.pkg.dev/$project/$($cfg.artifact_registry_repository)/campuspulse:cloud-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))"
    $buildFile = Join-Path $stage 'cloudbuild.json'
    @{ steps=@(@{ name='gcr.io/cloud-builders/docker'; args=@('build', '-f', 'deploy/Dockerfile.cloud', '-t', $tag, '.') });
        images=@($tag); timeout='1200s'; options=@{ logging='CLOUD_LOGGING_ONLY' };
        serviceAccount="projects/$project/serviceAccounts/$($cfg.build_service_account)" } |
        ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $buildFile -Encoding utf8NoBOM
    $build = Gcp @('builds', 'submit', $stage, "--config=$buildFile", "--region=$($cfg.build_region)",
        "--gcs-source-staging-dir=gs://$($cfg.staging_bucket)/cloud-source")
    if ($build.status -ne 'SUCCESS' -or $build.results.images[0].digest -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Image build digest unconfirmed.' }
    $ImageDigest = $tag.Split(':')[0] + '@' + $build.results.images[0].digest
}
$version = Gcp @('secrets', 'versions', 'add', $secretName, "--data-file=$settingsFile")
$configVersion = $version.name.Split('/')[-1]
if ($configVersion -notmatch '^[1-9][0-9]*$') { throw 'Settings version unconfirmed.' }
foreach ($service in $services) {
    $secrets = "/var/run/campuspulse/settings.json=${secretName}:$configVersion"
    if ($service.role -ne 'mcp' -and $GeminiKeySecret) { $secrets += ",GEMINI_API_KEY=$GeminiKeySecret" }
    $visibility = if ($service.role -eq 'backend') { '--allow-unauthenticated' } else { '--no-allow-unauthenticated' }
    $null = Gcp @('run', 'deploy', $service.name, "--region=$($service.region)", "--image=$ImageDigest",
        "--service-account=$($service.identity)", '--execution-environment=gen2', '--port=8080',
        '--cpu=1', '--memory=1Gi', '--concurrency=4', '--min-instances=0', '--max-instances=2',
        '--timeout=240', '--ingress=all', '--invoker-iam-check', $visibility,
        "--add-custom-audiences=$($service.origin)", "--set-secrets=$secrets",
        "--set-env-vars=CAMPUSPULSE_ROLE=$($service.role),CAMPUSPULSE_CLOUD_SETTINGS=/var/run/campuspulse/settings.json")
    $saved = Gcp @('run', 'services', 'describe', $service.name, "--region=$($service.region)")
    if ($saved.spec.template.spec.serviceAccountName -ne $service.identity) { throw 'Runtime service identity mismatch.' }
}
if (-not $MoodleOnly) {
    $null = Gcp @('run', 'services', 'add-iam-policy-binding', 'campuspulse-agent', "--region=$($cfg.agent_region)",
        "--member=serviceAccount:$($cfg.backend_service_account)", '--role=roles/run.invoker', '--condition=None')
    $null = Gcp @('run', 'services', 'add-iam-policy-binding', 'campuspulse-mcp', "--region=$($cfg.mcp_cloud_run_region)",
        "--member=serviceAccount:$($cfg.agent_service_account)", '--role=roles/run.invoker', '--condition=None')
}
foreach ($service in $services | Where-Object { $_.role -ne 'backend' }) {
    $policy = Gcp @('run', 'services', 'get-iam-policy', $service.name, "--region=$($service.region)")
    if (@($policy['bindings'] | Where-Object { $_.members -contains 'allUsers' -or $_.members -contains 'allAuthenticatedUsers' }).Count) {
        throw 'Private service has a public IAM binding; deployment is not ready.'
    }
}
[ordered]@{ status='deployed_functional_verification_pending'; backend=$settings.backend_origin;
    image=$ImageDigest; settings_version=$configVersion; tests='not_run' } | ConvertTo-Json
