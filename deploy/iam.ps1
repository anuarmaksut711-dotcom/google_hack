#requires -Version 7.0

[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)][string]$InputsPath,
    [ValidateSet('Inspect', 'Apply')][string]$Mode = 'Inspect',
    [switch]$BindRuntime
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
$deployer = "user:$account"
if ($account.EndsWith('.gserviceaccount.com')) { $deployer = "serviceAccount:$account" }
$saPattern = '^[a-z][a-z0-9-]{4,28}[a-z0-9]@' + [regex]::Escape($project) + '\.iam\.gserviceaccount\.com$'
foreach ($key in @('backend_service_account', 'agent_service_account', 'mcp_service_account')) { Need $key $saPattern }
if (@($cfg.backend_service_account, $cfg.agent_service_account, $cfg.mcp_service_account | Select-Object -Unique).Count -ne 3) { throw 'Runtime identities must be distinct.' }
if ($Mode -eq 'Apply') {
    Need build_service_account ('^([^\s@]+@' + [regex]::Escape($project) + '\.iam\.gserviceaccount\.com|' + $number + '-compute@developer\.gserviceaccount\.com|' + $number + '@cloudbuild\.gserviceaccount\.com)$')
    if ($cfg.build_service_account -in @($cfg.backend_service_account, $cfg.agent_service_account, $cfg.mcp_service_account)) { throw 'Build identity must be separate.' }
    Need build_region '^[a-z]+(?:-[a-z]+)+[0-9]$'
    Need artifact_registry_location '^[a-z]+(?:-[a-z]+)+[0-9]$'
    Need artifact_registry_repository '^[a-z][a-z0-9-]{0,62}$'
    Need firestore_database '^(\(default\)|[a-z][a-z0-9-]{2,61}[a-z0-9])$'
    Need staging_bucket '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'
    Need agent_region '^[a-z]+(?:-[a-z]+)+[0-9]$'
    if (-not $cfg.ContainsKey('secret_readers') -or $cfg.secret_readers -isnot [array]) { throw 'Explicit secret_readers array required (may be empty).' }
    foreach ($binding in $cfg.secret_readers) {
        if ($binding -isnot [hashtable] -or $binding['name'] -notmatch '^[a-zA-Z0-9_-]{1,255}$' -or
            $binding['identity'] -notin @($cfg.backend_service_account, $cfg.agent_service_account, $cfg.mcp_service_account)) { throw 'Invalid secret reader mapping.' }
    }
}
if ($BindRuntime) {
    if ($Mode -ne 'Apply') { throw '-BindRuntime requires -Mode Apply.' }
    Need mcp_service_name '^[a-z][a-z0-9-]{0,47}[a-z0-9]$'
    Need mcp_cloud_run_region '^[a-z]+(?:-[a-z]+)+[0-9]$'
    Need reasoning_engine_resource ('^projects/(' + [regex]::Escape($project) + '|' + $number + ')/locations/' + [regex]::Escape($cfg.agent_region) + '/reasoningEngines/[0-9]+$')
}
function Gcp([string[]]$Arguments) {
    $raw = & gcloud @Arguments "--project=$project" "--account=$account" --quiet --format=json 2>$null
    if ($LASTEXITCODE -ne 0) { throw "gcloud failed: $($Arguments[0..([Math]::Min(2,$Arguments.Count-1))] -join ' ')" }
    if ($raw) { return ($raw -join "`n" | ConvertFrom-Json -AsHashtable) }
}
function Change([string]$Target, [string[]]$Arguments) {
    if ($PSCmdlet.ShouldProcess($Target, ($Arguments -join ' '))) { $null = Gcp $Arguments }
}
function Role([string]$Id, [string[]]$Permissions) {
    if (-not $WhatIfPreference) {
        $roles = @(Gcp @('iam', 'roles', 'list', '--show-deleted'))
        $existing = @($roles | Where-Object { $_.name -eq "projects/$project/roles/$Id" })
        if ($existing.Count) {
            $role = Gcp @('iam', 'roles', 'describe', $Id)
            if ($role['deleted'] -eq $true -or $role.stage -eq 'DISABLED' -or
                (Compare-Object @($role.includedPermissions | Sort-Object) @($Permissions | Sort-Object))) { throw "Custom role drift: $Id; not overwritten." }
            return
        }
    }
    Change $Id @('iam', 'roles', 'create', $Id, "--title=$Id", '--stage=GA', "--permissions=$($Permissions -join ',')")
    if (-not $WhatIfPreference) {
        $role = Gcp @('iam', 'roles', 'describe', $Id)
        if (Compare-Object @($role.includedPermissions | Sort-Object) @($Permissions | Sort-Object)) { throw 'Role readback mismatch.' }
    }
}
function Grant([string[]]$Resource, [string]$Member, [string]$RoleName, [string]$Condition = 'None') {

    $index = if ($Resource[0] -in @('projects', 'secrets')) { 1 } else { 2 }
    $prefix = $Resource[0..($index - 1)]
    $suffix = $Resource[$index..($Resource.Count - 1)]
    Change ($Resource -join ' ') ($prefix + @('add-iam-policy-binding') + $suffix + @("--member=$Member", "--role=$RoleName", "--condition=$Condition"))
    if (-not $WhatIfPreference) {
        $policy = Gcp ($prefix + @('get-iam-policy') + $suffix)
        $matches = @($policy.bindings | Where-Object {
            $_.role -eq $RoleName -and $Member -in $_.members -and
            (($Condition -eq 'None' -and -not $_.ContainsKey('condition')) -or
                ($Condition -ne 'None' -and $_.ContainsKey('condition') -and $_.condition.expression -eq $dbExpression))
        })
        if (-not $matches.Count) { throw 'IAM readback mismatch.' }
    }
}
function PolicyBindings([hashtable]$Policy) {

    foreach ($entry in @($Policy['bindings'])) {
        if ($null -eq $entry) { throw 'Invalid null policy binding.' }
        $condition = $null
        if ($entry.ContainsKey('condition')) {
            $condition = [ordered]@{ expression = $entry.condition['expression'];
                title = $entry.condition['title']; description = $entry.condition['description'] }
        }
        [ordered]@{ role = $entry.role; members = @($entry.members | Sort-Object -Unique);
            condition = $condition } | ConvertTo-Json -Depth 8 -Compress
    }
}
if ($WhatIfPreference -and $Mode -ne 'Apply') { throw 'Use -Mode Apply -WhatIf for offline plan.' }
if (-not $WhatIfPreference) {
    if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) { throw 'gcloud not found; use official CLI with explicit authentication.' }
    $cli = Gcp @('config', 'list', '--all')
    if (($cli['auth'] -and ($cli.auth['impersonate_service_account'] -or $cli.auth['credential_file_override'])) -or
        [Environment]::GetEnvironmentVariable('CLOUDSDK_AUTH_ACCESS_TOKEN')) { throw 'Unexpected CLI impersonation/credential override.' }
    $actual = Gcp @('projects', 'describe', $project)
    if ([string]$actual.projectNumber -ne $number -or $actual.projectId -ne $project -or $actual.lifecycleState -ne 'ACTIVE') { throw 'Project identity/state mismatch.' }
    $policy = Gcp @('projects', 'get-iam-policy', $project)

    @{ status = 'actual_readback'; read_at_utc = [DateTimeOffset]::UtcNow.ToString('o'); project_id = $project;
        runtime_bindings = @($policy.bindings | Where-Object { @($_.members | Where-Object { $_ -in @(
            "serviceAccount:$($cfg.backend_service_account)", "serviceAccount:$($cfg.agent_service_account)",
            "serviceAccount:$($cfg.mcp_service_account)") }).Count } | Select-Object role, condition) } | ConvertTo-Json -Depth 5
    if ($Mode -eq 'Inspect') { return }

    $billing = Gcp @('billing', 'projects', 'describe', $project)
    if ($billing['billingEnabled'] -ne $true) { throw 'Billing disabled/unlinked; no IAM changes performed.' }
    $buildDefault = Gcp @('builds', 'get-default-service-account', "--region=$($cfg.build_region)")
    if ($cfg.build_service_account -eq "$number@cloudbuild.gserviceaccount.com" -and
        ($buildDefault | ConvertTo-Json -Compress) -notmatch [regex]::Escape($cfg.build_service_account)) { throw 'Legacy build identity not returned by discovery.' }
    if ($cfg.build_service_account -ne "$number@cloudbuild.gserviceaccount.com") {
        $build = Gcp @('iam', 'service-accounts', 'describe', $cfg.build_service_account)
        if ($build['disabled'] -eq $true) { throw 'Disabled build identity.' }
    }
    $bucket = Gcp @('storage', 'buckets', 'describe', "gs://$($cfg.staging_bucket)", '--raw')
    if ([string]$bucket.projectNumber -ne $number) { throw 'Bucket belongs to another project.' }
    $null = Gcp @('artifacts', 'repositories', 'describe', $cfg.artifact_registry_repository, "--location=$($cfg.artifact_registry_location)")
    $null = Gcp @('firestore', 'databases', 'describe', "--database=$($cfg.firestore_database)")
    foreach ($email in @($cfg.backend_service_account, $cfg.agent_service_account, $cfg.mcp_service_account)) {
        $identity = Gcp @('iam', 'service-accounts', 'describe', $email)
        if ($identity['disabled'] -eq $true) { throw 'Disabled runtime identity.' }
    }
    $token = & gcloud auth print-access-token "--account=$account" "--project=$project" --quiet 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $token) { throw 'Could not obtain deployer credential.' }
    try {
        $headers = @{ Authorization = "Bearer $token" }
        $needed = @('resourcemanager.projects.setIamPolicy', 'iam.roles.create', 'iam.roles.get',
            'iam.serviceAccounts.setIamPolicy', 'artifactregistry.repositories.setIamPolicy', 'storage.buckets.setIamPolicy')
        if ($cfg.secret_readers.Count) { $needed += 'secretmanager.secrets.setIamPolicy' }
        if ($BindRuntime) { $needed += @('run.services.setIamPolicy', 'aiplatform.reasoningEngines.setIamPolicy') }
        $uri = 'https://cloudresourcemanager.googleapis.com/v1/projects/' + $project + ':testIamPermissions'
        $tested = Invoke-RestMethod -Uri $uri -Method Post -Headers $headers -ContentType 'application/json' `
            -Body (@{ permissions = $needed } | ConvertTo-Json) -TimeoutSec 45
        if (@($needed | Where-Object { $_ -notin $tested.permissions }).Count) { throw 'Missing IAM administrator permissions.' }
    } catch { throw 'IAM authority preflight failed; no bindings changed. Resource-scoped administrators must arrange equivalent checked grants explicitly.' }
    finally { $token = $null; $headers = $null }
}
Role 'campuspulseFirestoreData' @('datastore.databases.get', 'datastore.entities.create', 'datastore.entities.get', 'datastore.entities.list', 'datastore.entities.update')
Role 'campuspulseGeminiCaller' @('aiplatform.endpoints.predict', 'serviceusage.services.use')
Role 'campuspulseApiConsumer' @('serviceusage.services.use')
Role 'campuspulseBackendQuery' @('aiplatform.reasoningEngines.query')
$dbExpression = "resource.name == 'projects/$project/databases/$($cfg.firestore_database)'"
$condition = "title=campuspulse_database_only,expression=$dbExpression"

if ($cfg.firestore_database -eq '(default)') { $condition = "title=campuspulse_default_database_only,expression=$dbExpression" }
foreach ($key in @('backend_service_account', 'mcp_service_account')) {
    Grant @('projects', $project) "serviceAccount:$($cfg[$key])" "projects/$project/roles/campuspulseFirestoreData" $condition
}
Grant @('projects', $project) "serviceAccount:$($cfg.agent_service_account)" "projects/$project/roles/campuspulseGeminiCaller"
Grant @('projects', $project) "serviceAccount:$($cfg.mcp_service_account)" "projects/$project/roles/campuspulseApiConsumer"
foreach ($key in @('backend_service_account', 'agent_service_account', 'mcp_service_account', 'build_service_account')) {
    if ($key -eq 'build_service_account' -and $cfg[$key] -eq "$number@cloudbuild.gserviceaccount.com") { continue }
    Grant @('iam', 'service-accounts', $cfg[$key]) $deployer 'roles/iam.serviceAccountUser'
}
Grant @('artifacts', 'repositories', $cfg.artifact_registry_repository, "--location=$($cfg.artifact_registry_location)") `
    "serviceAccount:$($cfg.build_service_account)" 'roles/artifactregistry.writer'
Grant @('projects', $project) "serviceAccount:$($cfg.build_service_account)" 'roles/logging.logWriter'
Grant @('storage', 'buckets', "gs://$($cfg.staging_bucket)") $deployer 'roles/storage.objectAdmin'
Grant @('storage', 'buckets', "gs://$($cfg.staging_bucket)") "serviceAccount:$($cfg.build_service_account)" 'roles/storage.objectViewer'
foreach ($binding in $cfg.secret_readers) {
    Grant @('secrets', $binding.name) "serviceAccount:$($binding.identity)" 'roles/secretmanager.secretAccessor'
}
if ($BindRuntime) {
    if (-not $WhatIfPreference) {
        $service = Gcp @('run', 'services', 'describe', $cfg.mcp_service_name, "--region=$($cfg.mcp_cloud_run_region)")
        if ($service.spec.template.spec.serviceAccountName -ne $cfg.mcp_service_account -or
            $service.metadata.annotations['run.googleapis.com/invoker-iam-disabled'] -eq 'true') { throw 'MCP service identity/IAM drift.' }
        $mcpPolicy = Gcp @('run', 'services', 'get-iam-policy', $cfg.mcp_service_name, "--region=$($cfg.mcp_cloud_run_region)")
        $currentProjectPolicy = Gcp @('projects', 'get-iam-policy', $project)
        foreach ($p in @($currentProjectPolicy, $mcpPolicy)) {
            if ($p -isnot [hashtable]) { throw 'Unreadable MCP access policy; stop for explicit review.' }


            $entries = @()
            if ($p.ContainsKey('bindings') -and $null -ne $p['bindings']) {
                if ($p['bindings'] -isnot [array]) { throw 'Unreadable MCP access policy; stop for explicit review.' }
                $entries = $p['bindings']
            }
            foreach ($entry in $entries) {
                if ($entry -isnot [hashtable] -or -not $entry.ContainsKey('members') -or
                    $entry['members'] -isnot [array] -or $entry['role'] -isnot [string]) {
                    throw 'Unreadable MCP access policy; stop for explicit review.'
                }
                if (@($entry.members | Where-Object { $_ -in @('allUsers', 'allAuthenticatedUsers') }).Count) {
                    throw 'Public MCP access detected; any public-principal binding requires explicit owner remediation.'
                }
            }
        }
    }
    Grant @('run', 'services', $cfg.mcp_service_name, "--region=$($cfg.mcp_cloud_run_region)") `
        "serviceAccount:$($cfg.agent_service_account)" 'roles/run.invoker'
    $resource = $cfg.reasoning_engine_resource
    if ($PSCmdlet.ShouldProcess($resource, 'Merge exact backend query role on actual Agent Engine; preserve etag')) {
        $token = & gcloud auth print-access-token "--account=$account" "--project=$project" --quiet 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $token) { throw 'Could not obtain deployer credential.' }
        try {
            $headers = @{ Authorization = "Bearer $token" }
            $base = "https://$($cfg.agent_region)-aiplatform.googleapis.com/v1/$resource"
            $engine = Invoke-RestMethod -Uri $base -Method Get -Headers $headers -TimeoutSec 45
            if ($engine.spec.serviceAccount -ne $cfg.agent_service_account) { throw 'Engine runtime identity mismatch.' }

            $getPolicyUri = $base + ':getIamPolicy?options.requestedPolicyVersion=3'
            $enginePolicy = Invoke-RestMethod -Uri $getPolicyUri -Method Post -Headers $headers -TimeoutSec 45
            $ep = $enginePolicy | ConvertTo-Json -Depth 20 | ConvertFrom-Json -AsHashtable
            if (-not $ep['etag']) { throw 'Missing IAM concurrency etag.' }
            if (-not $ep.ContainsKey('bindings') -or $null -eq $ep['bindings']) { $ep['bindings'] = @() }
            $roleName = "projects/$project/roles/campuspulseBackendQuery"
            $member = "serviceAccount:$($cfg.backend_service_account)"
            $binding = @($ep['bindings'] | Where-Object { $_.role -eq $roleName -and -not $_.ContainsKey('condition') })
            if ($binding.Count) { $binding[0].members = @(@($binding[0].members) + $member | Select-Object -Unique) }
            else { $ep['bindings'] = @($ep['bindings']) + @{ role = $roleName; members = @($member) } }
            $null = Invoke-RestMethod -Uri ($base + ':setIamPolicy') -Method Post -Headers $headers `
                -ContentType 'application/json' -Body (@{ policy = $ep } | ConvertTo-Json -Depth 20) -TimeoutSec 45
            $readback = Invoke-RestMethod -Uri $getPolicyUri -Method Post -Headers $headers -TimeoutSec 45
            $verified = $readback | ConvertTo-Json -Depth 20 | ConvertFrom-Json -AsHashtable
            if (-not $verified['etag'] -or -not @($verified['bindings'] | Where-Object {
                $_.role -eq $roleName -and $member -in $_.members -and -not $_.ContainsKey('condition')
            }).Count) { throw 'Agent IAM readback mismatch.' }
            if (@($ep.bindings | Where-Object { $_.ContainsKey('condition') }).Count -and $verified['version'] -ne 3) {
                throw 'Conditional Agent IAM policy version mismatch.'
            }
            if (Compare-Object @(PolicyBindings $ep | Sort-Object) @(PolicyBindings $verified | Sort-Object)) {
                throw 'Agent IAM binding contents differ after write; reconcile concurrent/unknown outcome.'
            }
        } catch { throw 'Agent IAM update/readback failed; reconcile exact resource policy before retry. No project-wide query fallback.' }
        finally { $token = $null; $headers = $null }
    }
}
if ($WhatIfPreference) { Write-Output 'OFFLINE prepared-only; no IAM read or write performed.' }
else { Write-Output 'Selected IAM bindings read back; runtime tokens/Workspace/session permissions NOT verified. Missing runtime bindings require -BindRuntime after deployment.' }
