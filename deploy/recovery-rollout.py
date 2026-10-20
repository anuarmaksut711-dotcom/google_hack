"""Prepare a SHA-bound recovery rollout without inventing private user bindings.

snapshot/build are independent of user data. prepare-settings requires two real
explicit bindings and writes only ignored local files. No activation occurs here.
"""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('recovery_gcp',ROOT/'deploy/recovery-gcp.py')
gcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gcp)
ENGINE = f'projects/{gcp.NUMBER}/locations/{gcp.REGION}/reasoningEngines/3019439524657758208'
PRIVATE = ROOT/'.campuspulse-local/rollout-20261020'


def snapshot():
    PRIVATE.mkdir(parents=True,exist_ok=True)
    path = PRIVATE/'rollback.json'
    if path.exists():
        raise RuntimeError('rollback_snapshot_exists_do_not_overwrite')
    services = {}
    safe = {}
    for name in ('campuspulse',gcp.MCP_SERVICE):
        service = gcp.gcloud('run','services','describe',name,f'--region={gcp.REGION}')
        revision = service['status']['latestReadyRevisionName']
        rev = gcp.gcloud('run','revisions','describe',revision,f'--region={gcp.REGION}')
        services[name] = dict(service=service,revision=rev)
        safe[name] = dict(revision=revision,image_digest=rev['status']['imageDigest'],
                          traffic=service['status']['traffic'])
    url = f'https://{gcp.REGION}-aiplatform.googleapis.com/v1/{ENGINE}'
    value = dict(captured_at=datetime.now(timezone.utc).isoformat(), services=services,
        engine=gcp.request(url), engine_iam=gcp.request(url+':getIamPolicy',body={}),
        mcp_iam=gcp.gcloud('run','services','get-iam-policy',gcp.MCP_SERVICE,f'--region={gcp.REGION}'))
    path.write_text(json.dumps(value,indent=2),encoding='utf-8')
    gcp.emit(status='rollback_snapshot_saved',services=safe,engine=ENGINE)


def build():
    sha = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,text=True).strip():
        raise RuntimeError('commit_tracked_changes_before_source_snapshot')
    stage = PRIVATE/('source-'+sha)
    stage.mkdir(parents=True,exist_ok=False)
    archive = subprocess.check_output(['git','archive','--format=tar',sha],cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(stage,filter='data')
    # Archive contains only tracked files. Do not apply .gitignore again: some
    # tracked test fixtures deliberately have otherwise ignored filenames.
    (stage/'.gcloudignore').write_text('.git\n.gcloudignore\n',encoding='utf-8')
    tag = f'{gcp.REGION}-docker.pkg.dev/{gcp.PROJECT}/campuspulse/recovery-rollout:{sha}'
    qa_tag = tag+'-qa'
    config = dict(steps=[
        dict(name='gcr.io/cloud-builders/docker',args=['build','--target','verified','--build-arg','SOURCE_SHA='+sha,
             '-f','deploy/Dockerfile.rollout','-t',qa_tag,'.']),
        dict(name='gcr.io/cloud-builders/docker',args=['build','--target','runtime','--build-arg','SOURCE_SHA='+sha,
             '-f','deploy/Dockerfile.rollout','-t',tag,'.']),
        dict(name='gcr.io/cloud-builders/docker',entrypoint='bash',args=['-c',
             'set -eu\ncontainer=$(docker create '+qa_tag+')\nmkdir -p release-out\ndocker cp "$container:/out/." release-out/\n'])],
        images=[tag],timeout='3600s',options=dict(logging='CLOUD_LOGGING_ONLY'),
        serviceAccount=f'projects/{gcp.PROJECT}/serviceAccounts/campuspulse-build@{gcp.PROJECT}.iam.gserviceaccount.com',
        artifacts=dict(objects=dict(location=f'gs://{gcp.PROJECT}-campuspulse-build/recovery-rollout/{sha}/',
            paths=['release-out/qa-summary.json','release-out/dist/*.whl','release-out/dist/*.tar.gz',
                   'release-out/*.log','release-out/pytest.xml','release-out/voice-report.json'])))
    cfg = stage/'rollout-build.json'
    cfg.write_text(json.dumps(config),encoding='utf-8')
    result = gcp.gcloud('builds','submit',str(stage),f'--config={cfg}',f'--region={gcp.REGION}',
        f'--gcs-source-staging-dir=gs://{gcp.PROJECT}-campuspulse-build/recovery-rollout-source','--async')
    receipt = dict(source_sha=sha,build_id=result['id'],image_tag=tag,
                   artifacts=f'gs://{gcp.PROJECT}-campuspulse-build/recovery-rollout/{sha}/')
    (PRIVATE/'build-receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    gcp.emit(status='release_build_submitted',**receipt)


def prepare_settings(bindings,engine_file,calendar_file):
    import sys
    sys.path.insert(0,str(ROOT/'src'))
    from campuspulse_recovery.cloud.settings import CloudSettings
    from campuspulse_recovery.cloud.calendar_app import CalendarSettings
    raw = json.loads(Path(bindings).read_text(encoding='utf-8-sig'))
    if not isinstance(raw,list) or len(raw)<2:
        raise RuntimeError('two_verified_private_account_bindings_required')
    for account in raw:
        if (not re.fullmatch(r'https://accounts\.google\.com#[0-9]{10,128}',account.get('principal_id',''))
            or account.get('calendar_id','').casefold()=='primary'
            or not account.get('calendar_id','').endswith('@group.calendar.google.com')
            or 'bootstrap' in json.dumps(account).lower()):
            raise RuntimeError('verified_subject_and_dedicated_test_calendar_required')
    engine = json.loads(Path(engine_file).read_text(encoding='utf-8-sig'))
    engine['calendar_feed_accounts'] = raw
    shared = CloudSettings.model_validate(engine)
    if shared.chat.provider!='vertex' or shared.application.project_id!=gcp.PROJECT or shared.application.firestore_database!=gcp.DATABASE:
        raise RuntimeError('matching_real_vertex_project_database_required')
    calendar = json.loads(Path(calendar_file).read_text(encoding='utf-8-sig'))
    calendar['recovery'] = dict(settings=shared.model_dump(mode='json'),target=dict(
        project_id=gcp.PROJECT,location=gcp.REGION,resource_name=ENGINE))
    website = CalendarSettings.model_validate(calendar)
    PRIVATE.mkdir(parents=True,exist_ok=True)
    for filename,value in [('engine-personal.json',shared),('calendar-personal.json',website)]:
        path=PRIVATE/filename
        if path.exists():
            raise RuntimeError('private_prepared_settings_exist_do_not_overwrite')
        path.write_text(value.model_dump_json(),encoding='utf-8')
    gcp.emit(status='private_settings_validated_not_deployed',accounts=len(raw),calendar_acl_verified=False,
             settings_sha256=hashlib.sha256(shared.model_dump_json().encode()).hexdigest())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['snapshot','build','prepare-settings'])
    p.add_argument('--bindings');p.add_argument('--engine-settings');p.add_argument('--calendar-settings')
    a=p.parse_args()
    if a.action=='snapshot':snapshot()
    elif a.action=='build':build()
    else:prepare_settings(a.bindings,a.engine_settings,a.calendar_settings)


if __name__=='__main__':
    try:main()
    except Exception as e:
        # Validation exceptions can include private input values. Never print them.
        if isinstance(e,RuntimeError):gcp.emit(status='blocked',error=str(e))
        else:gcp.emit(status='blocked',error_type=type(e).__name__)
        raise SystemExit(1)
