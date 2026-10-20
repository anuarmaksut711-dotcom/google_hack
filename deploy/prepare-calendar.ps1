# Prepare the dedicated calendar runtime. No local secrets enter the build source.
[CmdletBinding()]
param([Parameter(Mandatory)][string]$ProjectId,
      [Parameter(Mandatory)][string]$ProjectNumber,
      [string]$Region = 'us-central1')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
function Gcp([string[]]$Arguments) {
    $raw = & gcloud @Arguments "--project=$ProjectId" --quiet --format=json
    if ($LASTEXITCODE -ne 0) { throw "Cloud operation failed: $($Arguments[0..1] -join ' ')" }
    if ($raw) { return ($raw -join "`n" | ConvertFrom-Json -AsHashtable) }
}
$runtime = "campuspulse-calendar@$ProjectId.iam.gserviceaccount.com"
$builder = "campuspulse-build@$ProjectId.iam.gserviceaccount.com"
$origin = "https://campuspulse-$ProjectNumber.$Region.run.app"
$null = Gcp @('services','enable','run.googleapis.com','firestore.googleapis.com','secretmanager.googleapis.com',
    'cloudbuild.googleapis.com','artifactregistry.googleapis.com','cloudscheduler.googleapis.com')
$databases = @(Gcp @('firestore','databases','list'))
if (-not ($databases | Where-Object { $_.name.EndsWith('/campuspulse-calendar') })) {
    $null = Gcp @('firestore','databases','create','--database=campuspulse-calendar',"--location=$Region",'--type=firestore-native','--delete-protection')
}
$accounts = @(Gcp @('iam','service-accounts','list'))
foreach ($name in @('campuspulse-calendar','campuspulse-scheduler','campuspulse-build')) {
    if ("$name@$ProjectId.iam.gserviceaccount.com" -notin $accounts.email) {
        $null = Gcp @('iam','service-accounts','create',$name,"--display-name=$name")
    }
}
$null = Gcp @('projects','add-iam-policy-binding',$ProjectId,"--member=serviceAccount:$runtime",'--role=roles/datastore.user',
    "--condition=expression=resource.name=='projects/$ProjectId/databases/campuspulse-calendar',title=calendar-database")
$repos = @(Gcp @('artifacts','repositories','list',"--location=$Region"))
if (-not ($repos | Where-Object { $_.name.EndsWith('/campuspulse') })) {
    $null = Gcp @('artifacts','repositories','create','campuspulse',"--location=$Region",'--repository-format=docker')
}
$null = Gcp @('artifacts','repositories','add-iam-policy-binding','campuspulse',"--location=$Region",
    "--member=serviceAccount:$builder",'--role=roles/artifactregistry.writer')
$null = Gcp @('projects','add-iam-policy-binding',$ProjectId,"--member=serviceAccount:$builder",'--role=roles/logging.logWriter','--condition=None')
$bucket = "$ProjectId-campuspulse-build"
$buckets = @(Gcp @('storage','buckets','list'))
if (-not ($buckets | Where-Object { $_.name -eq $bucket })) {
    $null = Gcp @('storage','buckets','create',"gs://$bucket","--location=$Region",'--uniform-bucket-level-access','--public-access-prevention')
}
$null = Gcp @('storage','buckets','add-iam-policy-binding',"gs://$bucket","--member=serviceAccount:$builder",'--role=roles/storage.objectViewer')
$secretNames = @('campuspulse-calendar-settings','campuspulse-calendar-gemini')
$existing = @(Gcp @('secrets','list'))
foreach ($name in $secretNames) {
    if (-not ($existing | Where-Object { $_.name.EndsWith('/' + $name) })) {
        $null = Gcp @('secrets','create',$name,'--replication-policy=automatic')
    }
    $null = Gcp @('secrets','add-iam-policy-binding',$name,"--member=serviceAccount:$runtime",'--role=roles/secretmanager.secretAccessor')
}
# Copy the existing CampusPulse API key directly to Secret Manager, without a file or stdout.
$keyVersions = @(Gcp @('secrets','versions','list','campuspulse-calendar-gemini','--filter=state:ENABLED'))
if ($keyVersions.Count -eq 0) {
    $keys = @(Gcp @('services','api-keys','list'))
    $key = @($keys | Where-Object { $_.displayName -eq 'CampusPulse' })
    if ($key.Count -ne 1) { throw 'Expected one existing CampusPulse API key.' }
    $material = Gcp @('services','api-keys','get-key-string',$key[0].name)
    try {
        $material.keyString | & gcloud secrets versions add campuspulse-calendar-gemini --data-file=- "--project=$ProjectId" --quiet --format='value(name)'
        if ($LASTEXITCODE -ne 0) { throw 'Secret upload failed.' }
    } finally { $material = $null }
}
$settingsPath = Join-Path $root '.campuspulse-local/calendar-cloud-settings.json'
$settings = @{
    application = @{
        mode='cloud'; project_id=$ProjectId; agent_region=$Region; model_location=$Region;
        web_cloud_run_region=$Region; mcp_cloud_run_region=$Region; firestore_location=$Region;
        firestore_database='campuspulse-calendar'; workspace_access_mode='explicit_resource_sharing';
        student=@{trusted_user_id='bootstrap'; allowed_signin_principal='https://accounts.google.com#bootstrap';
            spreadsheet_id='calendar-feed'; assignments_a1_range="'Assignments'!A1:F100";
            calendar_id='calendar-feed'; timezone='Asia/Qyzylorda'};
        identities=@{backend_service_account=$runtime; agent_service_account=$builder;
            mcp_service_account="campuspulse-scheduler@$ProjectId.iam.gserviceaccount.com"}; secrets=@{}
    };
    origin=$origin;
    google_client_id=(Get-Content -Raw -LiteralPath (Join-Path $root '.campuspulse-local/google-signin-client-id.txt')).Trim();
    chat=(Get-Content -Raw -LiteralPath (Join-Path $root '.campuspulse-local/chat-moodle-gemini.json') | ConvertFrom-Json -AsHashtable);
    scheduler_service_account="campuspulse-scheduler@$ProjectId.iam.gserviceaccount.com"
}
$settings | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $settingsPath -Encoding utf8NoBOM
$version = Gcp @('secrets','versions','add','campuspulse-calendar-settings',"--data-file=$settingsPath")
$geminiVersion = if ($keyVersions.Count) {
    [string](($keyVersions | ForEach-Object { [int]$_.name.Split('/')[-1] } | Measure-Object -Maximum).Maximum)
} else { '1' }
@{project=$ProjectId; region=$Region; origin=$origin; builder=$builder; bucket=$bucket;
  settings_version=$version.name.Split('/')[-1]; gemini_version=$geminiVersion; runtime=$runtime} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root '.campuspulse-local/calendar-deployment.json') -Encoding utf8NoBOM
Write-Output "Calendar infrastructure prepared: $origin"
