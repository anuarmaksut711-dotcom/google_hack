"""Explicit, key-free recovery infrastructure deployment; never deploys the website.

Run with Python 3.12 and the repository's pinned cloud dependencies. Credentials
come from the active gcloud login and are held only in memory. Private artifacts
go to ignored .campuspulse-local/. No Calendar reads/writes or live grants in smoke.
"""
import argparse
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'gen-lang-client-0602946419'
NUMBER = '585736102295'
REGION = 'us-central1'
DATABASE = 'campuspulse-calendar'
AGENT = f'campuspulse-recovery-agent@{PROJECT}.iam.gserviceaccount.com'
MCP = f'campuspulse-recovery-mcp@{PROJECT}.iam.gserviceaccount.com'
BACKEND = f'campuspulse-calendar@{PROJECT}.iam.gserviceaccount.com'
BUCKET = f'{PROJECT}-campuspulse-agent-engine'
SECRET = 'campuspulse-recovery-settings'
MCP_SERVICE = 'campuspulse-recovery-mcp'
MCP_ORIGIN = f'https://{MCP_SERVICE}-{NUMBER}.{REGION}.run.app'
AGENT_AUDIENCE = f'https://campuspulse-recovery-agent-{NUMBER}.{REGION}.run.app'
MODEL = 'gemini-3.5-flash-lite'
PRIVATE = ROOT / '.campuspulse-local' / 'recovery-gcp'
CLI = shutil.which('gcloud.cmd') or shutil.which('gcloud')


def emit(**values):
    print(json.dumps(values, ensure_ascii=True), flush=True)


def gcloud(*args, raw=False, input_text=None):
    p = subprocess.run([CLI, *args, f'--project={PROJECT}', '--quiet', '--format=json'],
                       input=input_text, capture_output=True, text=True, encoding='utf-8')
    if p.returncode:
        # These commands never contain credentials as arguments. Do not print
        # payloads/SDK exceptions or configuration in a deployment failure.
        detail = p.stderr.strip()
        if any(s in args for s in ('access', 'add')) and 'secrets' in args:
            detail = 'secret operation failed; inspect IAM and version metadata'
        raise RuntimeError(f'gcloud {" ".join(args[:3])}: {detail}')
    return p.stdout if raw else json.loads(p.stdout or 'null')


def token(identity=None, audience=None):
    args = ['auth', 'print-identity-token' if audience else 'print-access-token']
    if identity:
        args += [f'--impersonate-service-account={identity}']
    if audience:
        args += [f'--audiences={audience}', '--include-email']
    value = gcloud(*args, raw=True).strip()
    if value.startswith('{'):
        value = json.loads(value)['token']
    if not re.fullmatch(r'[A-Za-z0-9._~-]+', value):
        raise RuntimeError('unexpected credential response shape')
    return value


def request(url, *, body=None, access=None):
    headers = {'Authorization': 'Bearer ' + (access or token())}
    data = None if body is None else json.dumps(body).encode()
    if data:
        headers['Content-Type'] = 'application/json'
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        value = json.loads(e.read()).get('error', {})
        raise RuntimeError(f'API HTTP {e.code}: {value.get("status")}: {value.get("message")}') from None


def engines():
    return request(f'https://{REGION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/reasoningEngines').get('reasoningEngines', [])


def inspect():
    project = gcloud('projects', 'describe', PROJECT)
    if str(project['projectNumber']) != NUMBER:
        raise RuntimeError('project number mismatch')
    emit(project=PROJECT, region=REGION,
         billing_enabled=gcloud('billing', 'projects', 'describe', PROJECT).get('billingEnabled'),
         engines=[{'name': e['name'], 'displayName': e.get('displayName')} for e in engines()],
         services=[s['metadata']['name'] for s in gcloud('run', 'services', 'list')],
         service_accounts=[s['email'] for s in gcloud('iam', 'service-accounts', 'list')],
         buckets=[b['name'] for b in gcloud('storage', 'buckets', 'list')])


def provision():
    inspect()
    if not gcloud('billing', 'projects', 'describe', PROJECT).get('billingEnabled'):
        raise RuntimeError('billing not enabled; this script never links billing')
    required = {'aiplatform.googleapis.com', 'run.googleapis.com', 'cloudbuild.googleapis.com',
                'artifactregistry.googleapis.com', 'firestore.googleapis.com', 'iam.googleapis.com',
                'iamcredentials.googleapis.com', 'secretmanager.googleapis.com', 'storage.googleapis.com'}
    enabled = {s['config']['name'] for s in gcloud('services', 'list', '--enabled')}
    if required - enabled:
        gcloud('services', 'enable', *sorted(required - enabled))
    request(f'https://serviceusage.googleapis.com/v1beta1/projects/{NUMBER}/services/aiplatform.googleapis.com:generateServiceIdentity', body={})
    existing = {s['email']: s for s in gcloud('iam', 'service-accounts', 'list')}
    for sa in (AGENT, MCP):
        if sa not in existing:
            gcloud('iam', 'service-accounts', 'create', sa.split('@')[0], '--display-name=CampusPulse recovery')
        elif existing[sa].get('disabled'):
            raise RuntimeError('existing recovery service account is disabled')
    dbs = gcloud('firestore', 'databases', 'list')
    if not any(d['name'].endswith('/' + DATABASE) and d['locationId'] == REGION for d in dbs):
        raise RuntimeError('existing session database missing/wrong region; do not create a parallel session store')
    for sa in (AGENT, MCP):
        gcloud('projects', 'add-iam-policy-binding', PROJECT,
               f'--member=serviceAccount:{sa}', '--role=roles/datastore.user',
               f'--condition=expression=resource.name==\'projects/{PROJECT}/databases/{DATABASE}\',title=recovery-database')
    gcloud('projects', 'add-iam-policy-binding', PROJECT,
           f'--member=serviceAccount:{AGENT}', '--role=roles/aiplatform.user', '--condition=None')
    # Agent Engine's service agent needs to obtain the custom runtime identity.
    gcloud('iam', 'service-accounts', 'add-iam-policy-binding', AGENT,
           f'--member=serviceAccount:service-{NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com',
           '--role=roles/iam.serviceAccountTokenCreator')
    buckets = {b['name'].removeprefix('gs://').rstrip('/') for b in gcloud('storage', 'buckets', 'list')}
    if BUCKET not in buckets:
        gcloud('storage', 'buckets', 'create', 'gs://' + BUCKET, f'--location={REGION}',
               '--uniform-bucket-level-access', '--public-access-prevention')
    gcloud('storage', 'buckets', 'add-iam-policy-binding', 'gs://' + BUCKET,
           f'--member=serviceAccount:service-{NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com',
           '--role=roles/storage.objectViewer')
    secrets = {s['name'].split('/')[-1] for s in gcloud('secrets', 'list')}
    if SECRET not in secrets:
        gcloud('secrets', 'create', SECRET, '--replication-policy=user-managed', f'--locations={REGION}')
    gcloud('secrets', 'add-iam-policy-binding', SECRET,
           f'--member=serviceAccount:{MCP}', '--role=roles/secretmanager.secretAccessor')
    emit(status='provisioned', public_site_changed=False)


def bootstrap_settings():
    sys.path.insert(0, str(ROOT / 'src'))
    from campuspulse_recovery.cloud.settings import CloudSettings
    response = gcloud('secrets', 'versions', 'access', '1', '--secret=campuspulse-calendar-settings')
    live = json.loads(base64.b64decode(response['payload']['data']))
    app = live['application']
    app.update(agent_region=REGION, model_location='global', mcp_cloud_run_region=REGION)
    app['identities'] = dict(backend_service_account=BACKEND, agent_service_account=AGENT, mcp_service_account=MCP)
    # No real user or shared Calendar: this deployment cannot accept live owners
    # until the separate per-user integration is completed and redeployed.
    app['student'] = dict(trusted_user_id='recovery-infrastructure-bootstrap',
        allowed_signin_principal='https://accounts.google.com#recovery-infrastructure-bootstrap',
        spreadsheet_id='recovery_infrastructure_bootstrap_no_resource',
        assignments_a1_range='Assignments!A1:H100',
        calendar_id='recovery-infrastructure-bootstrap.invalid', timezone='Asia/Qyzylorda')
    app['secrets'] = dict(oauth_client_secret_reference=None, oauth_refresh_token_secret_reference=None)
    settings = CloudSettings.model_validate(dict(application=app,
        backend_origin=live['origin'], agent_origin=AGENT_AUDIENCE, mcp_origin=MCP_ORIGIN,
        google_signin_client_id=live['google_client_id'],
        chat=dict(provider='vertex', project_id=PROJECT, location='global', model=MODEL),
        working_hours=[dict(weekdays=[0,1,2,3,4], start='09:00', end='18:00')]))
    PRIVATE.mkdir(parents=True, exist_ok=True)
    path = PRIVATE / 'engine-settings.json'
    if path.exists():
        raise RuntimeError('private settings already exist; inspect instead of overwriting')
    path.write_text(settings.model_dump_json(), encoding='utf-8')
    versions = gcloud('secrets', 'versions', 'list', SECRET)
    if any(v.get('state') == 'ENABLED' for v in versions):
        raise RuntimeError('existing settings secret version; do not silently replace')
    v = gcloud('secrets', 'versions', 'add', SECRET, f'--data-file={path}')
    emit(status='inert_bootstrap_settings', secret_version=v['name'], user_calendar_configured=False)


def build_mcp():
    stage = PRIVATE / ('source-' + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    files = ['pyproject.toml', 'requirements/LICENSE', 'requirements/dev.lock',
             'requirements/cloud-linux.lock', 'deploy/Dockerfile.recovery', 'deploy/prepare-agent-engine.py']
    files += [str(p.relative_to(ROOT)) for p in (ROOT / 'src').rglob('*')
              if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py','.js','.png','.txt'}]
    files += [str(p.relative_to(ROOT)) for p in (ROOT / 'tests').rglob('*.py') if '__pycache__' not in p.parts]
    for name in files:
        dst = stage / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dst)
    tag = f'{REGION}-docker.pkg.dev/{PROJECT}/campuspulse/recovery-mcp:{uuid.uuid4().hex}'
    config = dict(steps=[dict(name='gcr.io/cloud-builders/docker', args=['build','-f','deploy/Dockerfile.recovery','-t',tag,'.'])],
        images=[tag], timeout='1800s', options=dict(logging='CLOUD_LOGGING_ONLY'),
        serviceAccount=f'projects/{PROJECT}/serviceAccounts/campuspulse-build@{PROJECT}.iam.gserviceaccount.com')
    cfg = stage / 'cloudbuild.json'
    cfg.write_text(json.dumps(config), encoding='utf-8')
    result = gcloud('builds', 'submit', str(stage), f'--config={cfg}', f'--region={REGION}',
        f'--gcs-source-staging-dir=gs://{PROJECT}-campuspulse-build/recovery-source', '--async')
    emit(build_id=result['id'], image_tag=tag, source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())


def deploy_mcp(image):
    if not re.fullmatch(rf'{REGION}-docker.pkg.dev/{PROJECT}/campuspulse/recovery-mcp@sha256:[a-f0-9]{{64}}', image):
        raise RuntimeError('immutable recovery MCP image required')
    # IAM remains private on creation and update. The application also validates
    # signed service identity AND a server-issued session-bound tool grant.
    result = gcloud('run', 'deploy', MCP_SERVICE, f'--region={REGION}', f'--image={image}',
        f'--service-account={MCP}', '--no-allow-unauthenticated', '--ingress=all',
        '--min-instances=0', '--max-instances=2', '--memory=1Gi', '--cpu=1', '--timeout=180',
        '--set-env-vars=CAMPUSPULSE_ROLE=mcp,CAMPUSPULSE_CLOUD_SETTINGS=/var/run/campuspulse/settings.json',
        f'--set-secrets=/var/run/campuspulse/settings.json={SECRET}:1')
    gcloud('run', 'services', 'add-iam-policy-binding', MCP_SERVICE, f'--region={REGION}',
        f'--member=serviceAccount:{AGENT}', '--role=roles/run.invoker')
    policy = gcloud('run', 'services', 'get-iam-policy', MCP_SERVICE, f'--region={REGION}')
    if any(m in {'allUsers','allAuthenticatedUsers'} for b in policy.get('bindings',[]) for m in b['members']):
        raise RuntimeError('MCP public policy detected')
    emit(mcp_origin=MCP_ORIGIN, ready_revision=result['status']['latestReadyRevisionName'], iam=policy['bindings'])


def deploy_engine(bundle):
    logging.disable(logging.CRITICAL)
    from importlib.metadata import version
    if version('google-cloud-aiplatform') != '1.165.1':
        raise RuntimeError('requires verified SDK 1.165.1')
    sys.path.insert(0, str(ROOT / 'src'))
    from campuspulse_recovery.agent_engine.runtime import AgentEngineRuntime
    from google.oauth2.credentials import Credentials
    import vertexai
    from vertexai import agent_engines
    found = [e for e in engines() if e.get('displayName') == 'campuspulse-recovery']
    if found:
        emit(status='existing_resource_reused_no_create', resource_names=[e['name'] for e in found])
        return
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / 'manifest.json').read_text())
    for name, entry in manifest['files'].items():
        if Path(name).name != name or hashlib.sha256((bundle/name).read_bytes()).hexdigest() != entry['sha256']:
            raise RuntimeError('bundle hash/path mismatch')
    settings = json.loads((bundle / 'settings.json').read_text())
    if settings['application']['project_id'] != PROJECT or settings['application']['identities']['agent_service_account'] != AGENT:
        raise RuntimeError('bundle target identity mismatch')
    vertexai.init(project=PROJECT, location=REGION, staging_bucket='gs://' + BUCKET, credentials=Credentials(token()))
    cfg = json.loads((bundle / 'create-config.json').read_text())
    os.chdir(bundle)
    remote = agent_engines.create(AgentEngineRuntime(settings), **cfg,
        min_instances=0, max_instances=2, resource_limits={'cpu':'2','memory':'4Gi'}, container_concurrency=5)
    resource = remote.resource_name
    PRIVATE.mkdir(parents=True, exist_ok=True)
    (PRIVATE/'engine-resource.txt').write_text(resource, encoding='utf-8')
    emit(status='remote_deployment_completed', resource_name=resource, operation_schema=remote.operation_schemas())


def check(resource):
    if not re.fullmatch(rf'projects/{NUMBER}/locations/{REGION}/reasoningEngines/[0-9]+', resource):
        raise RuntimeError('explicit actual resource name required')
    url = f'https://{REGION}-aiplatform.googleapis.com/v1/{resource}'
    engine = request(url)
    emit(resource_name=engine['name'], service_account=engine['spec'].get('serviceAccount'),
         operation_schema=engine['spec'].get('classMethods', []))
    # Deliberately invalid input: runtime must reject before any source/calendar
    # IO, grant creation, session impersonation, or write. Not an end-to-end pass.
    result = request(url + ':query', body={'class_method':'async_query',
        'input': {'invocation':'invalid-smoke','service_token':'invalid-smoke'}})
    output = result.get('output', {})
    if output != {'status_code':403,'payload':{'error':'service_or_invocation_rejected'}}:
        raise RuntimeError('remote rejection smoke returned unexpected envelope')
    emit(status='remote_custom_operation_rejection_verified', output=output, recovery_success=False)


def authorize_backend(resource):
    if not re.fullmatch(rf'projects/{NUMBER}/locations/{REGION}/reasoningEngines/[0-9]+', resource):
        raise RuntimeError('explicit actual resource name required')
    permissions = ['aiplatform.reasoningEngines.get', 'aiplatform.reasoningEngines.query']
    role_id = 'campuspulseRecoveryCaller'
    roles = gcloud('iam', 'roles', 'list')
    existing = next((r for r in roles if r['name'].endswith('/' + role_id)), None)
    if existing:
        detail = gcloud('iam', 'roles', 'describe', role_id)
        if set(detail['includedPermissions']) != set(permissions):
            raise RuntimeError('existing caller role differs; review instead of overwriting')
    else:
        gcloud('iam', 'roles', 'create', role_id, '--title=CampusPulse recovery caller',
               '--permissions=' + ','.join(permissions), '--stage=GA')
    gcloud('projects', 'add-iam-policy-binding', PROJECT,
           f'--member=serviceAccount:{BACKEND}', f'--role=projects/{PROJECT}/roles/{role_id}',
           f'--condition=expression=resource.name==\'{resource}\',title=campuspulse-recovery-engine')
    emit(status='backend_query_authorized', backend=BACKEND, resource=resource, permissions=permissions)


def model_smoke():
    result = request(f'https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/global/publishers/google/models/{MODEL}:generateContent',
        access=token(AGENT), body={'contents':[{'role':'user','parts':[{'text':'Reply with OK.'}]}],
        'generationConfig':{'maxOutputTokens':32,'temperature':0}})
    emit(status='vertex_model_response', identity=AGENT, location='global', model=result.get('modelVersion'),
         candidates=len(result.get('candidates',[])), personal_data=False)


def check_mcp():
    def probe(path, identity=None):
        headers = {}
        if identity:
            value = token(identity, MCP_ORIGIN)
            headers = {'Authorization':'Bearer ' + value, 'X-Serverless-Authorization':'Bearer ' + value}
        try:
            with urllib.request.urlopen(urllib.request.Request(MCP_ORIGIN + path, headers=headers),timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    anonymous = probe('/mcp')
    signed_health = probe('/healthz', AGENT)
    missing_grant = probe('/mcp', AGENT)
    if (anonymous,signed_health,missing_grant) != (403,200,403):
        raise RuntimeError('MCP ingress/identity/grant checks failed')
    emit(status='private_mcp_checks_passed', anonymous=anonymous,
         agent_signed_health=signed_health, signed_missing_grant=missing_grant, tool_execution_verified=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['inspect','provision','bootstrap-settings','build-mcp','deploy-mcp','deploy-engine','check',
                                     'authorize-backend','model-smoke','check-mcp'])
    p.add_argument('--image'); p.add_argument('--bundle'); p.add_argument('--resource')
    a = p.parse_args()
    actions = {'inspect':inspect, 'provision':provision, 'bootstrap-settings':bootstrap_settings, 'build-mcp':build_mcp,
               'deploy-mcp':lambda:deploy_mcp(a.image or ''), 'deploy-engine':lambda:deploy_engine(a.bundle or ''),
               'check':lambda:check(a.resource or ''), 'authorize-backend':lambda:authorize_backend(a.resource or ''),
               'model-smoke':model_smoke, 'check-mcp':check_mcp}
    actions[a.action]()


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        emit(status='failed', error=str(error))
        sys.exit(1)
    except Exception as error:
        emit(status='failed', error_type=type(error).__name__)
        sys.exit(1)
