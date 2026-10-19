#requires -Version 7.0
<#
.SYNOPSIS
Offline T12 command preparation. This revision CANNOT deploy.
.DESCRIPTION
InputsPath is a NEW non-secret T12 input document, not gcp-foundation.json.
It includes the explicit T11 fields validated below, plus image_tag,
image_digest, mcp_app_factory and app_config_secret_reference. AppConfigPath
is an explicit T04 AppConfig file. No defaults, credential discovery or imports.

Prepare returns data, never executes its command vectors. Deploy deliberately
fails before subprocess/HTTP: accepted full4 production composition still lacks
concrete authentication, server-owned run binding and durable stopped evidence
adapters. A future reviewed wiring change must remove this gate; input booleans,
proof JSON, a two-read factory or a caller-supplied script cannot open it.
The proposed APP_CONFIG path mount/env contract below requires that wiring too.
Neither preparation nor synthetic tests prove cloud readiness or IAM.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InputsPath,
    [Parameter(Mandatory)][string]$AppConfigPath,
    [ValidateSet('Prepare', 'Deploy')][string]$Mode = 'Prepare'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-McpJson([string]$Path) {
    try {
        $value = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json -AsHashtable
        if ($value -isnot [System.Collections.IDictionary]) { throw 'shape' }
        return $value
    } catch { throw 'mcp_input_document_invalid' }
}
function Require-McpField($Object, [string]$Key, [string]$Pattern) {
    if ($Object -isnot [System.Collections.IDictionary] -or -not $Object.Contains($Key) -or
        $Object[$Key] -isnot [string] -or $Object[$Key] -cnotmatch $Pattern) {
        # Field names are static call-site constants; never echo input values.
        throw "mcp_input_invalid:$Key"
    }
}
function Assert-McpPrivatePolicy($Policy) {
    if ($Policy -isnot [System.Collections.IDictionary] -or -not $Policy.Contains('bindings') -or
        $Policy.bindings -isnot [array]) { throw 'mcp_iam_policy_unreadable' }
    foreach ($binding in $Policy.bindings) {
        if ($binding -isnot [System.Collections.IDictionary] -or -not $binding.Contains('members') -or
            $binding.members -isnot [array]) { throw 'mcp_iam_policy_unreadable' }
        foreach ($member in $binding.members) {
            if ($member -isnot [string] -or $member -in @('allUsers', 'allAuthenticatedUsers')) {
                throw 'mcp_public_or_unknown_principal_rejected'
            }
        }
    }
}
function Assert-McpProductionReady {
    # Intentional hard gate, not an operator approval switch. Do not replace
    # with static trusted identity, fake stop proof or config.approved=true.
    throw 'mcp_production_adapters_unimplemented:authenticator,run_binding,stopped_evidence;cloud_execution_deferred'
}

$cfg = Read-McpJson $InputsPath
$app = Read-McpJson $AppConfigPath
$regional = '^[a-z]+(?:-[a-z]+)+[0-9]$'
Require-McpField $cfg 'project_id' '^[a-z][a-z0-9-]{4,28}[a-z0-9]$'
Require-McpField $cfg 'project_number' '^[1-9][0-9]+$'
Require-McpField $cfg 'deployer_account' '^[^\s@]+@[^\s@]+$'
foreach ($field in @('mcp_cloud_run_region', 'artifact_registry_location', 'build_region')) {
    Require-McpField $cfg $field $regional
}
foreach ($field in @('agent_region', 'web_cloud_run_region')) { Require-McpField $app $field $regional }
Require-McpField $app 'model_location' '^(global|[a-z]+(?:-[a-z]+)+[0-9])$'
Require-McpField $app 'firestore_location' '^(?:[a-z]+(?:-[a-z]+)+[0-9]|[a-z]{3}[0-9])$'
Require-McpField $app 'firestore_database' '^(\(default\)|[a-z][a-z0-9-]{2,61}[a-z0-9])$'
Require-McpField $app 'mode' '^cloud$'
Require-McpField $app 'workspace_access_mode' '^explicit_resource_sharing$'
$project = $cfg.project_id
$saPattern = '^[a-z][a-z0-9-]{4,28}[a-z0-9]@' + [regex]::Escape($project) + '\.iam\.gserviceaccount\.com$'
foreach ($field in @('backend_service_account', 'agent_service_account', 'mcp_service_account')) {
    Require-McpField $cfg $field $saPattern
    if (-not $app.Contains('identities') -or $app.identities[$field] -cne $cfg[$field]) {
        throw 'mcp_app_identity_mismatch'
    }
}
$builderPattern = '^([a-z][a-z0-9-]{4,28}[a-z0-9]@' + [regex]::Escape($project) +
    '\.iam\.gserviceaccount\.com|' + $cfg.project_number + '-compute@developer\.gserviceaccount\.com|' +
    $cfg.project_number + '@cloudbuild\.gserviceaccount\.com)$'
Require-McpField $cfg 'build_service_account' $builderPattern
if (@($cfg.backend_service_account, $cfg.agent_service_account, $cfg.mcp_service_account,
        $cfg.build_service_account | Select-Object -Unique).Count -ne 4) { throw 'mcp_distinct_identities_required' }
if (-not $app.Contains('project_id') -or $app.project_id -cne $project -or
    -not $app.Contains('mcp_cloud_run_region') -or $app.mcp_cloud_run_region -cne $cfg.mcp_cloud_run_region) {
    throw 'mcp_app_project_or_region_mismatch'
}
Require-McpField $cfg 'artifact_registry_repository' '^[a-z][a-z0-9-]{0,62}$'
Require-McpField $cfg 'staging_bucket' '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'
Require-McpField $cfg 'mcp_service_name' '^[a-z][a-z0-9-]{0,47}[a-z0-9]$'
Require-McpField $cfg 'image_tag' '^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$'
Require-McpField $cfg 'image_digest' '^sha256:[0-9a-f]{64}$'
Require-McpField $cfg 'mcp_app_factory' '^campuspulse_recovery(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+:[a-zA-Z_][a-zA-Z0-9_]*$'
if ($cfg.mcp_app_factory -match '^campuspulse_recovery\.mcp_service\.server:') {
    throw 'mcp_uncomposed_or_legacy_factory_rejected'
}
$secretPattern = '^projects/' + [regex]::Escape($project) + '/secrets/[A-Za-z0-9_-]+/versions/[1-9][0-9]*$'
Require-McpField $cfg 'app_config_secret_reference' $secretPattern
if (-not $cfg.Contains('secret_readers') -or $cfg.secret_readers -isnot [array]) { throw 'mcp_secret_readers_required' }
$secretName = $cfg.app_config_secret_reference.Split('/')[3]
if (@($cfg.secret_readers | Where-Object {
    $_ -is [System.Collections.IDictionary] -and $_['name'] -ceq $secretName -and
    $_['identity'] -ceq $cfg.mcp_service_account }).Count -ne 1) { throw 'mcp_explicit_secret_reader_required' }
if (-not $app.Contains('student')) { throw 'mcp_student_allowlist_required' }
Require-McpField $app.student 'trusted_user_id' '^[^\s\x00-\x1f]{1,256}$'
Require-McpField $app.student 'allowed_signin_principal' '^https://[^\s#]+#[^\s#]+$'
Require-McpField $app.student 'spreadsheet_id' '^[A-Za-z0-9_-]{1,256}$'
Require-McpField $app.student 'calendar_id' '^[^\s\x00-\x1f]{1,512}$'
if ($app.student.calendar_id -ieq 'primary') { throw 'mcp_explicit_calendar_required' }
Require-McpField $app.student 'timezone' '^[A-Za-z_]+(?:/[A-Za-z0-9_+.-]+)+$'
Require-McpField $app.student 'assignments_a1_range' "^(?:'(?:[^'\x00-\x1f]|'')+'|[A-Za-z_][A-Za-z0-9_]*)![A-Z]{1,3}[1-9][0-9]{0,6}:[A-Z]{1,3}[1-9][0-9]{0,6}$"
# These checks protect command preparation, not a substitute for T04's strict
# load_config(environment='cloud') inside the future trusted factory.

$root = Split-Path $PSScriptRoot -Parent
$imageBase = "$($cfg.artifact_registry_location)-docker.pkg.dev/$project/$($cfg.artifact_registry_repository)/campuspulse-mcp"
$taggedImage = "${imageBase}:$($cfg.image_tag)"
$pinnedImage = "$imageBase@$($cfg.image_digest)"
$common = @("--project=$project", "--account=$($cfg.deployer_account)", '--quiet', '--format=json')
$buildConfig = [ordered]@{
    steps = @(@{name = 'gcr.io/cloud-builders/docker'; args = @('build', '-f', 'deploy/Dockerfile.mcp', '-t', $taggedImage, '.')})
    images = @($taggedImage)
    serviceAccount = "projects/$project/serviceAccounts/$($cfg.build_service_account)"
    options = @{logging = 'CLOUD_LOGGING_ONLY'}
    timeout = '1200s'
}
$secretMount = '/var/run/campuspulse/app-config.json=' + $secretName + ':' + $cfg.app_config_secret_reference.Split('/')[-1]
$steps = @(
    @{phase = 'read_before_mutation'; args = @('projects', 'get-iam-policy', $project) + $common},
    @{phase = 'read_before_mutation'; args = @('run', 'services', 'get-iam-policy', $cfg.mcp_service_name, "--region=$($cfg.mcp_cloud_run_region)") + $common},
    @{phase = 'build'; args = @('builds', 'submit', $root, "--region=$($cfg.build_region)",
        "--gcs-source-staging-dir=gs://$($cfg.staging_bucket)/t12-source", '--config=<generated-build-config.json>',
        '--ignore-file=<generated-t12.gcloudignore>') + $common},
    @{phase = 'deploy_after_verified_build_digest'; args = @('run', 'deploy', $cfg.mcp_service_name,
        "--image=$pinnedImage", "--region=$($cfg.mcp_cloud_run_region)", "--service-account=$($cfg.mcp_service_account)",
        '--no-allow-unauthenticated', '--invoker-iam-check', '--ingress=all', '--port=8080',
        '--min-instances=0', '--max-instances=2', '--concurrency=4', '--cpu=1', '--memory=512Mi',
        '--timeout=300', '--cpu-throttling', '--startup-probe=httpGet.path=/healthz,httpGet.port=8080',
        "--set-env-vars=CAMPUSPULSE_MCP_ENVIRONMENT=cloud,CAMPUSPULSE_MCP_APP_FACTORY=$($cfg.mcp_app_factory),CAMPUSPULSE_APP_CONFIG_PATH=/var/run/campuspulse/app-config.json",
        "--set-secrets=$secretMount") + $common},
    @{phase = 'bind_only_after_private_readback'; args = @('run', 'services', 'add-iam-policy-binding', $cfg.mcp_service_name,
        "--region=$($cfg.mcp_cloud_run_region)", "--member=serviceAccount:$($cfg.agent_service_account)", '--role=roles/run.invoker') + $common},
    @{phase = 'read_after_mutation'; args = @('run', 'services', 'describe', $cfg.mcp_service_name, "--region=$($cfg.mcp_cloud_run_region)") + $common},
    @{phase = 'read_after_mutation'; args = @('run', 'services', 'get-iam-policy', $cfg.mcp_service_name, "--region=$($cfg.mcp_cloud_run_region)") + $common}
)
$plan = [ordered]@{
    schema_version = 1; status = 'OFFLINE_PREPARED'; executable = $false
    network_class = 'none'; project_id = $project; mcp_region = $cfg.mcp_cloud_run_region
    app_config_sha256 = (Get-FileHash -LiteralPath $AppConfigPath -Algorithm SHA256).Hash.ToLowerInvariant()
    production_surface = @('read_assignments', 'read_calendar', 'apply_plan', 'verify_plan')
    smoke_calls = @('read_assignments', 'read_calendar'); build_config = $buildConfig; command_vectors = $steps
    # Docker ignore does not protect the source upload to Cloud Build. Future
    # executor must materialize this separate allowlist and inspect the tar.
    build_source_ignore = @('*', '!pyproject.toml', '!src/', '!src/**', '!requirements/',
        '!requirements/LICENSE', '!requirements/dev.lock', '!requirements/mcp-linux.lock',
        '!deploy/', '!deploy/Dockerfile.mcp', '!deploy/mcp-entrypoint.py', '!.dockerignore',
        '**/__pycache__/', '**/*.pyc', '**/*.egg-info/')
    required_readbacks = @('billing_and_locations_and_permissions', 'builder_identity_and_successful_build_digest_equals_input',
        'no_public_principals_project_service_and_effective_inheritance', 'secret_reader_and_pinned_app_config_bytes',
        'source_upload_allowlist_and_no_private_config', 'cloud_run_iam_check_enabled', 'runtime_sa', 'ready_revision_image_digest_traffic_and_scaling',
        'healthz_and_private_mcp_http', 'agent_engine_network_reachability')
    blockers = @('production_authenticator', 'production_server_owned_run_binding', 'production_durable_stopped_evidence',
        'trusted_factory_config_and_host_allowlist_wiring', 'cloud_execution_deferred')
}
if ($Mode -eq 'Deploy') { Assert-McpProductionReady }
$plan
