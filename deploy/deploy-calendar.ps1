[CmdletBinding()]
param([string]$DeploymentPath = '.campuspulse-local/calendar-deployment.json')
$ErrorActionPreference = 'Stop'
$cfg = Get-Content -Raw -LiteralPath $DeploymentPath | ConvertFrom-Json -AsHashtable
function Gcp([string[]]$Arguments) {
    $raw = & gcloud @Arguments "--project=$($cfg.project)" --quiet --format=json
    if ($LASTEXITCODE -ne 0) { throw "Cloud operation failed: $($Arguments[0..1] -join ' ')" }
    if ($raw) { return ($raw -join "`n" | ConvertFrom-Json -AsHashtable) }
}
$build = Gcp @('builds','describe',$cfg.build_id,"--region=$($cfg.region)")
if ($build.status -ne 'SUCCESS' -or $build.results.images[0].digest -notmatch '^sha256:[a-f0-9]{64}$') {
    throw 'Build has not succeeded; refusing deployment.'
}
$image = $cfg.image_tag.Split(':')[0] + '@' + $build.results.images[0].digest
$secrets = "/var/run/campuspulse/settings.json=campuspulse-calendar-settings:$($cfg.settings_version),GEMINI_API_KEY=campuspulse-calendar-gemini:$($cfg.gemini_version)"
$environment = 'CAMPUSPULSE_CALENDAR_SETTINGS=/var/run/campuspulse/settings.json'
$null = Gcp @('run','deploy','campuspulse',"--region=$($cfg.region)","--image=$image",
    '--command=python','--args=-m,campuspulse_recovery.cloud.calendar_app',
    "--service-account=$($cfg.runtime)",'--execution-environment=gen2','--port=8080',
    '--cpu=1','--memory=1Gi','--concurrency=8','--min-instances=0','--max-instances=2',
    '--timeout=300','--ingress=all','--allow-unauthenticated','--invoker-iam-check',
    "--add-custom-audiences=$($cfg.origin)","--update-secrets=$secrets","--update-env-vars=$environment")
$null = Gcp @('run','jobs','deploy','campuspulse-check',"--region=$($cfg.region)","--image=$image",
    "--service-account=$($cfg.runtime)",'--cpu=1','--memory=1Gi','--tasks=1','--max-retries=0',
    '--task-timeout=300','--command=python','--args=-m,campuspulse_recovery.cloud.calendar_check',
    "--update-secrets=$secrets","--update-env-vars=$environment")
$scheduler = "campuspulse-scheduler@$($cfg.project).iam.gserviceaccount.com"
$null = Gcp @('run','services','add-iam-policy-binding','campuspulse',"--region=$($cfg.region)",
    "--member=serviceAccount:$scheduler",'--role=roles/run.invoker')
$jobs = @(Gcp @('scheduler','jobs','list',"--location=$($cfg.region)"))
$operation = if ($jobs | Where-Object { $_.name.EndsWith('/campuspulse-calendar-sync') }) { 'update' } else { 'create' }
$null = Gcp @('scheduler','jobs',$operation,'http','campuspulse-calendar-sync',"--location=$($cfg.region)",
    '--schedule=*/15 * * * *','--time-zone=Asia/Qyzylorda',"--uri=$($cfg.origin)/internal/calendar/sync",
    '--http-method=POST',"--oidc-service-account-email=$scheduler","--oidc-token-audience=$($cfg.origin)",
    '--attempt-deadline=300s','--max-retry-attempts=2','--min-backoff=60s','--max-backoff=300s')
$cfg.image_digest=$image
$cfg.deployed_at=[DateTime]::UtcNow.ToString('o')
$cfg | ConvertTo-Json | Set-Content -LiteralPath $DeploymentPath -Encoding utf8NoBOM
Write-Output "Deployed: $($cfg.origin)/login"
