#requires -Version 7.0

[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)][string]$InputsPath,
    [ValidateSet('Inspect', 'Provision')][string]$Mode = 'Inspect',
    [switch]$PolicyAndQuotaReviewed
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$cfg = Get-Content -Raw -LiteralPath $InputsPath | ConvertFrom-Json -AsHashtable
function Need([string]$Key, [string]$Pattern) {
    if (-not $cfg.ContainsKey($Key) -or $cfg[$Key] -isnot [string] -or $cfg[$Key] -notmatch $Pattern) {
        throw "Missing/invalid deployment input: $Key"
    }
}
Need project_id '^[a-z][a-z0-9-]{4,28}[a-z0-9]$'
Need project_number '^[1-9][0-9]+$'
Need deployer_account '^[^\s@]+@[^\s@]+$'
$project = $cfg.project_id
$number = $cfg.project_number
$account = $cfg.deployer_account
$regional = '^[a-z]+(?:-[a-z]+)+[0-9]$'
$apiNames = @('aiplatform.googleapis.com', 'run.googleapis.com', 'cloudbuild.googleapis.com',
    'artifactregistry.googleapis.com', 'firestore.googleapis.com', 'secretmanager.googleapis.com',
    'iam.googleapis.com', 'iamcredentials.googleapis.com', 'cloudresourcemanager.googleapis.com',
    'serviceusage.googleapis.com', 'orgpolicy.googleapis.com', 'storage.googleapis.com', 'logging.googleapis.com',
    'sheets.googleapis.com', 'calendar-json.googleapis.com')
if ($Mode -eq 'Provision') {
    foreach ($key in @('agent_region', 'web_cloud_run_region', 'mcp_cloud_run_region',
        'artifact_registry_location', 'build_region')) { Need $key $regional }
    Need model_location '^(global|[a-z]+(?:-[a-z]+)+[0-9])$'
    Need firestore_location '^[a-z][a-z0-9-]+$'
    Need firestore_database '^(\(default\)|[a-z][a-z0-9-]{2,61}[a-z0-9])$'
    Need artifact_registry_repository '^[a-z][a-z0-9-]{0,62}$'
    Need staging_bucket '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'
    Need staging_bucket_location '^[a-zA-Z][a-zA-Z0-9-]+$'
    foreach ($key in @('backend_service_account', 'agent_service_account', 'mcp_service_account')) {
        Need $key ('^[a-z][a-z0-9-]{4,28}[a-z0-9]@' + [regex]::Escape($project) + '\.iam\.gserviceaccount\.com$')
    }
    Need build_service_account ('^([^\s@]+@' + [regex]::Escape($project) + '\.iam\.gserviceaccount\.com|' + $number + '-compute@developer\.gserviceaccount\.com|' + $number + '@cloudbuild\.gserviceaccount\.com)$')
    if (@($cfg.backend_service_account, $cfg.agent_service_account, $cfg.mcp_service_account,
        $cfg.build_service_account | Select-Object -Unique).Count -ne 4) { throw 'Build and runtime identities must be distinct.' }
    if (-not $cfg.ContainsKey('secrets') -or $cfg.secrets -isnot [array]) { throw 'Explicit secrets array required (may be empty).' }
    foreach ($secret in $cfg.secrets) {
        if ($secret -isnot [string] -or $secret -notmatch '^[a-zA-Z0-9_-]{1,255}$') { throw 'Invalid secret metadata name.' }
    }
    if ($cfg.secrets.Count) { Need secret_manager_location $regional }
}
function Gcp([string[]]$Arguments) {
    $raw = & gcloud @Arguments "--project=$project" "--account=$account" --quiet --format=json 2>$null
    if ($LASTEXITCODE -ne 0) { throw "gcloud failed: $($Arguments[0..([Math]::Min(2,$Arguments.Count-1))] -join ' '); inspect locally without logging credentials." }
    if ($raw) { return ($raw -join "`n" | ConvertFrom-Json -AsHashtable) }
}
function Change([string]$Target, [string[]]$Arguments) {
    if ($PSCmdlet.ShouldProcess($Target, ($Arguments -join ' '))) { $null = Gcp $Arguments }
}
if ($WhatIfPreference) {
    if ($Mode -ne 'Provision') { throw 'Use -Mode Provision -WhatIf for an offline plan.' }
    Change $project (@('services', 'enable') + $apiNames)
    Change $project @('beta', 'services', 'identity', 'create', '--service=aiplatform.googleapis.com')
    foreach ($key in @('backend_service_account', 'agent_service_account', 'mcp_service_account')) {
        Change $cfg[$key] @('iam', 'service-accounts', 'create', ($cfg[$key].Split('@')[0]))
    }
    if ($cfg.build_service_account.EndsWith("@$project.iam.gserviceaccount.com")) {
        Change $cfg.build_service_account @('iam', 'service-accounts', 'create', ($cfg.build_service_account.Split('@')[0]))
    }
    Change $cfg.artifact_registry_repository @('artifacts', 'repositories', 'create', $cfg.artifact_registry_repository,
        "--location=$($cfg.artifact_registry_location)", '--repository-format=docker')
    Change $cfg.firestore_database @('firestore', 'databases', 'create', "--database=$($cfg.firestore_database)",
        "--location=$($cfg.firestore_location)", '--type=firestore-native', '--delete-protection')
    Change $cfg.staging_bucket @('storage', 'buckets', 'create', "gs://$($cfg.staging_bucket)",
        "--location=$($cfg.staging_bucket_location)", '--uniform-bucket-level-access', '--public-access-prevention')
    foreach ($secret in $cfg.secrets) { Change $secret @('secrets', 'create', $secret,
        '--replication-policy=user-managed', "--locations=$($cfg.secret_manager_location)") }
    Write-Output 'OFFLINE prepared-only; existence, IAM, billing, quota, policies and locations NOT verified.'
    return
}
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) { throw 'gcloud not found; install official Google Cloud CLI and authenticate explicitly.' }
$cli = Gcp @('config', 'list', '--all')
if (($cli['auth'] -and ($cli.auth['impersonate_service_account'] -or $cli.auth['credential_file_override'])) -or
    [Environment]::GetEnvironmentVariable('CLOUDSDK_AUTH_ACCESS_TOKEN')) {
    throw 'Unexpected CLI impersonation/credential override; verify explicit deployer account first.'
}
$actual = Gcp @('projects', 'describe', $project)
if ([string]$actual.projectNumber -ne $number -or $actual.projectId -ne $project -or $actual.lifecycleState -ne 'ACTIVE') {
    throw 'Project identity/state mismatch.'
}
$billing = Gcp @('billing', 'projects', 'describe', $project)
$services = @(Gcp @('services', 'list', '--enabled'))
$identities = @(Gcp @('iam', 'service-accounts', 'list'))

[ordered]@{ status = 'actual_readback'; read_at_utc = [DateTimeOffset]::UtcNow.ToString('o');
    project_id = $project; project_number = $number; billing_enabled = ($billing['billingEnabled'] -eq $true);
    enabled_apis = @($services | ForEach-Object { $_.config.name });
    runtime_identities = @($identities | Where-Object { $_.email -match '^campuspulse-(backend|agent|mcp)@' } |
        ForEach-Object { @{ email = $_.email; disabled = ($_.ContainsKey('disabled') -and $_.disabled -eq $true) } }) } | ConvertTo-Json -Depth 5
if ($Mode -eq 'Inspect') { return }
if ($billing['billingEnabled'] -ne $true) { throw 'Billing disabled/unlinked. No mutations performed; script never links an account.' }


$token = & gcloud auth print-access-token "--account=$account" "--project=$project" --quiet 2>$null
if ($LASTEXITCODE -ne 0 -or -not $token) { throw 'Could not obtain deployer credential.' }
try {
    $headers = @{ Authorization = "Bearer $token" }
    $required = @('serviceusage.services.enable', 'iam.serviceAccounts.create',
        'artifactregistry.repositories.create', 'datastore.databases.create',
        'storage.buckets.create', 'secretmanager.secrets.create')
    $permissionUri = 'https://cloudresourcemanager.googleapis.com/v1/projects/' + $project + ':testIamPermissions'
    $test = Invoke-RestMethod -Uri $permissionUri -Method Post -Headers $headers -ContentType 'application/json' `
        -Body (@{ permissions = $required } | ConvertTo-Json) -TimeoutSec 45
    if (@($required | Where-Object { $_ -notin $test.permissions }).Count) { throw 'Missing provisioning permissions.' }


    Change $project (@('services', 'enable') + $apiNames)
    foreach ($service in @('aiplatform.googleapis.com', 'run.googleapis.com', 'cloudbuild.googleapis.com')) {
        $next = ''
        do {
            $quotaUri = "https://serviceusage.googleapis.com/v1beta1/projects/$number/services/$service/consumerQuotaMetrics?view=FULL&pageSize=200"
            if ($next) { $quotaUri += '&pageToken=' + [Uri]::EscapeDataString($next) }
            $quota = Invoke-RestMethod -Uri $quotaUri -Headers $headers -TimeoutSec 45
            @{ status = 'actual_quota_metadata'; service = $service; page = $quota } | ConvertTo-Json -Depth 30
            $next = if ($quota.PSObject.Properties['nextPageToken']) { [string]$quota.nextPageToken } else { '' }
        } while ($next)
    }
} catch { throw 'Readiness API read/permission check failed. APIs may have been enabled; no data resources provisioned. Inspect quota/permissions locally.' }
finally { $token = $null; $headers = $null }
foreach ($constraint in @('gcp.resourceLocations', 'iam.disableServiceAccountCreation',
    'iam.allowedPolicyMemberDomains', 'run.allowedIngress', 'run.allowedVPCEgress',
    'iam.disableServiceAccountKeyCreation', 'iam.automaticIamGrantsForDefaultServiceAccounts',
    'iam.managed.disableServiceAccountCreation', 'run.managed.requireInvokerIam')) {
    $effective = Gcp @('org-policies', 'describe', $constraint, '--effective')
    @{ status = 'actual_effective_policy'; constraint = $constraint; policy = $effective } | ConvertTo-Json -Depth 15
}
if (-not $PolicyAndQuotaReviewed) { throw 'Review effective policies and location/model quotas first; then pass -PolicyAndQuotaReviewed. Only API enablement may have occurred.' }

Change $project @('beta', 'services', 'identity', 'create', '--service=aiplatform.googleapis.com')
$buildDefault = Gcp @('builds', 'get-default-service-account', "--region=$($cfg.build_region)")
Write-Output (@{ status = 'actual_readback'; cloud_build_default = $buildDefault;
    selected_build_identity = $cfg.build_service_account; build_region = $cfg.build_region } | ConvertTo-Json -Depth 4)

if ($cfg.build_service_account -eq "$number@cloudbuild.gserviceaccount.com") {
    if (($buildDefault | ConvertTo-Json -Compress) -notmatch [regex]::Escape($cfg.build_service_account)) { throw 'Legacy build identity was not returned by discovery.' }
} else {
    if ($cfg.build_service_account.EndsWith("@$project.iam.gserviceaccount.com") -and
        $cfg.build_service_account -notin @($identities | ForEach-Object { $_.email })) {
        Change $cfg.build_service_account @('iam', 'service-accounts', 'create', ($cfg.build_service_account.Split('@')[0]),
            '--display-name=CampusPulse build')
    }
    $build = Gcp @('iam', 'service-accounts', 'describe', $cfg.build_service_account)
    if ($build['disabled'] -eq $true) { throw 'Selected build identity disabled.' }
}
foreach ($key in @('backend_service_account', 'agent_service_account', 'mcp_service_account')) {
    $email = $cfg[$key]
    if ($email -notin @($identities | ForEach-Object { $_.email })) {
        Change $email @('iam', 'service-accounts', 'create', ($email.Split('@')[0]), '--display-name=CampusPulse runtime')
    }
    $sa = Gcp @('iam', 'service-accounts', 'describe', $email)
    if ($sa['disabled'] -eq $true) { throw 'Runtime identity disabled; no implicit re-enable.' }
}
$repos = @(Gcp @('artifacts', 'repositories', 'list', "--location=$($cfg.artifact_registry_location)"))
$repoName = "projects/$project/locations/$($cfg.artifact_registry_location)/repositories/$($cfg.artifact_registry_repository)"
if ($repoName -notin @($repos | ForEach-Object { $_.name })) {
    Change $repoName @('artifacts', 'repositories', 'create', $cfg.artifact_registry_repository,
        "--location=$($cfg.artifact_registry_location)", '--repository-format=docker')
}
$repo = Gcp @('artifacts', 'repositories', 'describe', $cfg.artifact_registry_repository, "--location=$($cfg.artifact_registry_location)")
if ($repo.format -ne 'DOCKER' -or $repo.name -ne $repoName -or ($repo.ContainsKey('mode') -and $repo.mode -ne 'STANDARD_REPOSITORY')) { throw 'Repository configuration drift.' }
$databases = @(Gcp @('firestore', 'databases', 'list'))
$dbName = "projects/$project/databases/$($cfg.firestore_database)"
if ($dbName -notin @($databases | ForEach-Object { $_.name })) {
    Change $dbName @('firestore', 'databases', 'create', "--database=$($cfg.firestore_database)",
        "--location=$($cfg.firestore_location)", '--type=firestore-native', '--delete-protection')
}
$db = Gcp @('firestore', 'databases', 'describe', "--database=$($cfg.firestore_database)")
if ($db.locationId -ne $cfg.firestore_location -or $db.type -ne 'FIRESTORE_NATIVE' -or $db.deleteProtectionState -ne 'DELETE_PROTECTION_ENABLED') {
    throw 'Firestore configuration drift; location/type/protection not changed automatically.'
}
$buckets = @(Gcp @('storage', 'buckets', 'list'))
if ($cfg.staging_bucket -notin @($buckets | ForEach-Object { $_.name })) {
    Change $cfg.staging_bucket @('storage', 'buckets', 'create', "gs://$($cfg.staging_bucket)",
        "--location=$($cfg.staging_bucket_location)", '--uniform-bucket-level-access', '--public-access-prevention')
}
$bucket = Gcp @('storage', 'buckets', 'describe', "gs://$($cfg.staging_bucket)", '--raw')
if ([string]$bucket.projectNumber -ne $number -or $bucket.location -ine $cfg.staging_bucket_location -or
    $bucket.iamConfiguration.uniformBucketLevelAccess.enabled -ne $true -or
    $bucket.iamConfiguration.publicAccessPrevention -ne 'enforced') { throw 'Staging bucket configuration drift.' }
$knownSecrets = @(Gcp @('secrets', 'list'))
foreach ($secret in $cfg.secrets) {
    if ("projects/$number/secrets/$secret" -notin @($knownSecrets | ForEach-Object { $_.name })) {
        Change $secret @('secrets', 'create', $secret, '--replication-policy=user-managed', "--locations=$($cfg.secret_manager_location)")
    }
    $metadata = Gcp @('secrets', 'describe', $secret)
    if (@($metadata.replication.userManaged.replicas).Count -ne 1 -or
        $metadata.replication.userManaged.replicas[0].location -ne $cfg.secret_manager_location) { throw 'Secret replication drift.' }
}
$enabledAfter = @(Gcp @('services', 'list', '--enabled') | ForEach-Object { $_.config.name })
if (@($apiNames | Where-Object { $_ -notin $enabledAfter }).Count) { throw 'API readback incomplete.' }
[ordered]@{ status = 'foundation_verified'; read_at_utc = [DateTimeOffset]::UtcNow.ToString('o');
    project_id = $project; project_number = $number; repository = $repo.name; database = $db.name;
    staging_bucket = $cfg.staging_bucket; secret_metadata = $cfg.secrets;
    runtime_deployment = 'not_performed'; iam = 'run iam.ps1 separately';
    cost_limits = 'prepared-only; enforce max instances/timeouts in T12/T13 deployments' } | ConvertTo-Json -Depth 4
