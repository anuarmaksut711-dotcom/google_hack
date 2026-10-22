$ErrorActionPreference = 'Stop'
$projectId = 'project-e25873c6-e889-40bb-86d'
$regionId = 'us-central1'
$origin = 'https://campuspulse-403025736340.us-central1.run.app'
$scheduler = "campuspulse-calendar-sync@$projectId.iam.gserviceaccount.com"
$job = 'campuspulse-telegram-deadlines'
$policyRaw = & gcloud --configuration=campuspulse-t11 run services get-iam-policy campuspulse "--project=$projectId" "--region=$regionId" --quiet --format=json
if ($LASTEXITCODE -ne 0) { throw 'Cloud Run IAM read failed' }
$policy = ($policyRaw -join "`n") | ConvertFrom-Json
$invoker = @($policy.bindings | Where-Object {$_.role -eq 'roles/run.invoker' -and $_.members -contains "serviceAccount:$scheduler"})
if (-not $invoker) {
    $null = & gcloud --configuration=campuspulse-t11 run services add-iam-policy-binding campuspulse "--project=$projectId" "--region=$regionId" "--member=serviceAccount:$scheduler" --role=roles/run.invoker --quiet --format=json
    if ($LASTEXITCODE -ne 0) { throw 'Scheduler invoker grant failed' }
}
$null = & gcloud --configuration=campuspulse-t11 scheduler jobs describe $job "--project=$projectId" "--location=$regionId" --quiet --format=json 2>$null
$operation = if ($LASTEXITCODE -eq 0) { 'update' } else { 'create' }
& gcloud --configuration=campuspulse-t11 scheduler jobs $operation http $job "--project=$projectId" "--location=$regionId" `
    '--schedule=*/5 * * * *' --time-zone=Asia/Qyzylorda "--uri=$origin/internal/telegram/remind" --http-method=POST `
    "--oidc-service-account-email=$scheduler" "--oidc-token-audience=$origin" `
    --attempt-deadline=240s --max-retry-attempts=1 --min-backoff=60s --max-backoff=300s --quiet --format='json(name,schedule,state,httpTarget.uri,httpTarget.oidcToken.serviceAccountEmail)'
if ($LASTEXITCODE -ne 0) { throw 'Scheduler configuration failed' }
