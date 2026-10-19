#requires -Version 7.0
<#
.SYNOPSIS
Create local, non-secret Gemini and Google connection settings without network calls.
.DESCRIPTION
Does not authenticate, enable APIs, deploy services, run the application or run tests.
Missing values remain null. Existing files are preserved; use another output directory
when preparing a different team's configuration.
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [ValidatePattern('^$|^[a-z][a-z0-9-]{4,28}[a-z0-9]$')][string]$ProjectId,
    [ValidatePattern('^$|^[1-9][0-9]+$')][string]$ProjectNumber,
    [string]$DeployerAccount,
    [ValidatePattern('^[a-z]+(?:-[a-z]+)+[0-9]$')][string]$Region = 'us-central1',
    [ValidatePattern('^gemini-[a-z0-9.-]+$')][string]$GeminiModel = 'gemini-flash-latest',
    [ValidatePattern('^$|^gemini-[a-z0-9.-]+$')][string]$VertexModel,
    [ValidatePattern('^$|^[A-Za-z0-9_-]+$')][string]$SpreadsheetId,
    [string]$CalendarId,
    [string]$SigninPrincipal,
    [string]$GoogleSigninClientId,
    [string]$WorkingHoursPath
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $projectRoot '.campuspulse-local' }
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$null = New-Item -ItemType Directory -Path $outputRoot -Force

function Optional-Value([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    return $Value
}
function Save-Settings([string]$Name, $Value) {
    $destination = Join-Path $outputRoot $Name
    if (Test-Path -LiteralPath $destination) {
        Write-Host "Preserved existing settings: $destination"
        return
    }
    $json = $Value | ConvertTo-Json -Depth 12
    # CreateNew also prevents overwriting a file created concurrently.
    $stream = [System.IO.File]::Open($destination, [System.IO.FileMode]::CreateNew)
    try {
        $writer = [System.IO.StreamWriter]::new($stream, [System.Text.UTF8Encoding]::new($false))
        try { $writer.WriteLine($json) } finally { $writer.Dispose() }
    }
    finally { $stream.Dispose() }
    Write-Host "Prepared: $destination"
}
function Service-Identity([string]$Role) {
    if ($ProjectId) { return "campuspulse-$Role@$ProjectId.iam.gserviceaccount.com" }
    return $null
}

Save-Settings 'chat-gemini.json' ([ordered]@{
    provider = 'gemini_api'
    model = $GeminiModel
})
Save-Settings 'chat-vertex.json' ([ordered]@{
    provider = 'vertex'
    project_id = Optional-Value $ProjectId
    location = $Region
    model = Optional-Value $VertexModel
})

$application = Get-Content -LiteralPath (Join-Path $projectRoot 'config.example.json') -Raw | ConvertFrom-Json -AsHashtable
$application.project_id = Optional-Value $ProjectId
foreach ($field in @('agent_region', 'model_location', 'web_cloud_run_region', 'mcp_cloud_run_region', 'firestore_location')) {
    $application[$field] = $Region
}
$application.student.trusted_user_id = 'campuspulse-student'
$application.student.allowed_signin_principal = Optional-Value $SigninPrincipal
$application.student.spreadsheet_id = Optional-Value $SpreadsheetId
$application.student.assignments_a1_range = "'Assignments'!A1:F100"
$application.student.calendar_id = Optional-Value $CalendarId
foreach ($role in @('backend', 'agent', 'mcp')) {
    $application.identities["${role}_service_account"] = Service-Identity $role
}
$application.mode = 'local_development'
Save-Settings 'app-config.local.json' $application
$application.mode = 'cloud'
Save-Settings 'app-config.cloud.json' $application

$cloud = [ordered]@{
    project_id = Optional-Value $ProjectId
    project_number = Optional-Value $ProjectNumber
    deployer_account = Optional-Value $DeployerAccount
    agent_region = $Region
    model_location = $Region
    web_cloud_run_region = $Region
    mcp_cloud_run_region = $Region
    artifact_registry_location = $Region
    build_region = $Region
    firestore_location = $Region
    firestore_database = '(default)'
    artifact_registry_repository = 'campuspulse'
    staging_bucket = $(if ($ProjectId -and $ProjectNumber) { "$ProjectId-$ProjectNumber-campus" } else { $null })
    staging_bucket_location = $Region
    backend_service_account = Service-Identity 'backend'
    agent_service_account = Service-Identity 'agent'
    mcp_service_account = Service-Identity 'mcp'
    build_service_account = Service-Identity 'build'
    secret_manager_location = $Region
    secrets = @('campuspulse-app-config')
    secret_readers = @(@{ name = 'campuspulse-app-config'; identity = (Service-Identity 'mcp') })
    mcp_service_name = 'campuspulse-mcp'
    image_tag = 'hackathon'
    image_digest = $null
    mcp_app_factory = $null
    app_config_secret_reference = $null
    reasoning_engine_resource = $null
}
Save-Settings 'cloud-inputs.json' $cloud

# Separate files keep earlier Agent Engine preparation and user edits intact.
$runCloud = [ordered]@{}
foreach ($entry in $cloud.GetEnumerator()) { $runCloud[$entry.Key] = $entry.Value }
$runCloud.secrets = @('campuspulse-cloud-settings', 'campuspulse-gemini-key')
$runCloud.secret_readers = @(
    @{ name='campuspulse-cloud-settings'; identity=(Service-Identity 'backend') },
    @{ name='campuspulse-cloud-settings'; identity=(Service-Identity 'agent') },
    @{ name='campuspulse-cloud-settings'; identity=(Service-Identity 'mcp') },
    @{ name='campuspulse-gemini-key'; identity=(Service-Identity 'backend') },
    @{ name='campuspulse-gemini-key'; identity=(Service-Identity 'agent') }
)
Save-Settings 'cloud-run-inputs.json' $runCloud
$hours = $null
if ($WorkingHoursPath) { $hours = @(Get-Content -LiteralPath $WorkingHoursPath -Raw | ConvertFrom-Json -AsHashtable) }
Save-Settings 'cloud-settings.json' ([ordered]@{
    application=$application
    backend_origin=$(if ($ProjectNumber) { "https://campuspulse-web-$ProjectNumber.$Region.run.app" } else { $null })
    agent_origin=$(if ($ProjectNumber) { "https://campuspulse-agent-$ProjectNumber.$Region.run.app" } else { $null })
    mcp_origin=$(if ($ProjectNumber) { "https://campuspulse-mcp-$ProjectNumber.$Region.run.app" } else { $null })
    google_signin_client_id=(Optional-Value $GoogleSigninClientId)
    chat=@{ provider='gemini_api'; model=$GeminiModel }
    working_hours=$hours
})
Save-Settings 'cloud-runtime-status.json' ([ordered]@{
    status='implemented_not_deployed_or_tested'
    settings='cloud-settings.json'
    provisioning='cloud-run-inputs.json'
    entrypoint='python -m campuspulse_recovery.cloud'
    deployment='deploy/deploy-cloud.ps1'
    runtime='three_cloud_run_services'
    tests='not_run_at_user_request'
})

Save-Settings 'connections.json' ([ordered]@{
    status = 'prepared_not_connected'
    verification = 'not_run_at_user_request'
    chat = @{
        gemini_settings = 'chat-gemini.json'
        vertex_settings = 'chat-vertex.json'
        api_key_environment_variable = 'GEMINI_API_KEY'
        source_mode_in_run_demo = 'synthetic'
    }
    workspace = @{
        settings = 'app-config.local.json'
        authentication = 'user_adc_impersonates_mcp_service_account'
        sheets_access = 'reader_on_selected_spreadsheet'
        calendar_access = 'writer_on_selected_calendar'
        required_api_services = @('sheets.googleapis.com', 'calendar-json.googleapis.com', 'iamcredentials.googleapis.com')
    }
    cloud = @{
        settings = 'cloud-inputs.json'
        application_settings = 'app-config.cloud.json'
        region = $Region
        region_status = 'proposed_not_provisioned'
        setup_script = 'deploy/prepare-gcp.ps1'
        runtime_settings = 'cloud-settings.json'
        runtime_deployment = 'deploy/deploy-cloud.ps1'
        status = 'implemented_not_deployed_or_tested'
    }
})
Write-Host 'Preparation only: no API calls, tests, application startup or deployment performed.'
Write-Host 'See setup/CONNECTING.md for the handoff and later launch commands.'
