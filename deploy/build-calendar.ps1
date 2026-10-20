[CmdletBinding()]
param([string]$DeploymentPath = '.campuspulse-local/calendar-deployment.json')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$cfg = Get-Content -Raw -LiteralPath $DeploymentPath | ConvertFrom-Json -AsHashtable
$stage = Join-Path $root ('.validation/calendar-source-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $stage
$files = @('pyproject.toml','requirements/LICENSE','requirements/dev.lock',
           'requirements/calendar-linux.lock','deploy/Dockerfile.calendar')
$files += @(Get-ChildItem -LiteralPath (Join-Path $root 'src') -File -Recurse |
    Where-Object { $_.Extension -in @('.py','.js','.png','.txt') -and $_.FullName -notmatch '[\\/]__pycache__[\\/]' } |
    ForEach-Object { [System.IO.Path]::GetRelativePath($root, $_.FullName) })
foreach ($relative in $files) {
    $destination = Join-Path $stage $relative
    $null = New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force
    Copy-Item -LiteralPath (Join-Path $root $relative) -Destination $destination
}
$tag = "$($cfg.region)-docker.pkg.dev/$($cfg.project)/campuspulse/calendar:$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))"
$buildFile = Join-Path $stage 'cloudbuild.json'
@{steps=@(@{name='gcr.io/cloud-builders/docker';args=@('build','-f','deploy/Dockerfile.calendar','-t',$tag,'.')});
    images=@($tag);timeout='1200s';options=@{logging='CLOUD_LOGGING_ONLY'};
    serviceAccount="projects/$($cfg.project)/serviceAccounts/$($cfg.builder)"} |
    ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $buildFile -Encoding utf8NoBOM
$raw = & gcloud builds submit $stage "--config=$buildFile" "--project=$($cfg.project)" "--region=$($cfg.region)" `
    "--gcs-source-staging-dir=gs://$($cfg.bucket)/calendar-source" --async --quiet --format=json
if ($LASTEXITCODE -ne 0) { throw 'Cloud build submission failed.' }
$build = $raw -join "`n" | ConvertFrom-Json -AsHashtable
$cfg.build_id=$build.id
$cfg.image_tag=$tag
$cfg | ConvertTo-Json | Set-Content -LiteralPath $DeploymentPath -Encoding utf8NoBOM
Write-Output "Build submitted: $($build.id)"
