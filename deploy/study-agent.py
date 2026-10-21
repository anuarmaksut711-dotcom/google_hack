"""Update the existing read-only study Agent Engine from verified local artifacts.

Run with Python 3.12 and requirements/cloud-linux.lock installed. This command
never creates an Engine, edits IAM, deploys Cloud Run, or reads/writes a calendar.
Authentication uses the selected gcloud login in memory; settings are never
copied into the upload directory or emitted in diagnostics.
"""
import argparse
from contextlib import contextmanager
import hashlib
from importlib.metadata import version
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'project-e25873c6-e889-40bb-86d'
PROJECT_NUMBER = '403025736340'
LOCATION = 'us-central1'
RESOURCE = (f'projects/{PROJECT_NUMBER}/locations/{LOCATION}/'
            'reasoningEngines/8841397574196789248')
DISPLAY_NAME = 'campuspulse-readonly-deadlines'
TELEMETRY = 'GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY'


class DeploymentError(ValueError):
    """A static diagnostic code that cannot contain settings or credentials."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise DeploymentError('invalid_cli_arguments')


def emit(**values):
    print(json.dumps(values, ensure_ascii=True), flush=True)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DeploymentError('duplicate_settings_key')
        result[key] = value
    return result


def _settings(path):
    with path.open('rb') as source:
        data = source.read(64_001)
    if len(data) > 64_000:
        raise DeploymentError('settings_too_large')
    return json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique_object)


def _access_token(account):
    command = shutil.which('gcloud.cmd') or shutil.which('gcloud')
    if command is None:
        raise DeploymentError('gcloud_not_found')
    args = [command, 'auth', 'print-access-token', '--quiet']
    if account:
        if re.fullmatch(r'[A-Za-z0-9._+@-]{3,256}', account) is None:
            raise DeploymentError('invalid_gcloud_account')
        args += ['--account=' + account]
    completed = subprocess.run(args, capture_output=True, text=True,
                               encoding='utf-8', timeout=90, check=False)
    token = completed.stdout.strip()
    if completed.returncode or re.fullmatch(r'[A-Za-z0-9._~-]{32,4096}', token) is None:
        raise DeploymentError('gcloud_access_token_unavailable')
    return token


def _get_json(url, token):
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(2_000_001)
    except urllib.error.HTTPError as error:
        # API error messages may echo request configuration; only use the code.
        raise DeploymentError('metadata_http_' + str(error.code)) from None
    if len(data) > 2_000_000:
        raise DeploymentError('metadata_response_too_large')
    return json.loads(data)


def _runtime_and_artifacts(options):
    if sys.version_info[:2] != (3, 12):
        raise DeploymentError('python_3_12_required')
    for package, expected in {'google-cloud-aiplatform': '1.165.1',
                              'google-adk': '2.6.3', 'cloudpickle': '3.1.2'}.items():
        if version(package) != expected:
            raise DeploymentError('verified_sdk_versions_required')
    sys.path.insert(0, str(ROOT / 'src'))
    from campuspulse_recovery.agent.conversation import ConversationTarget
    from campuspulse_recovery.agent_engine.artifacts import inspect_artifacts
    from campuspulse_recovery.agent_engine.study_runtime import StudyAgentRuntime
    from campuspulse_recovery.cloud.study_grants import StudyAgentSettings
    from campuspulse_recovery.config import AppConfig
    raw = _settings(options.settings)
    application = AppConfig.model_validate(raw['application'])
    study = StudyAgentSettings.model_validate(raw['study_agent'])
    chat = ConversationTarget.model_validate(raw['chat'])
    application.require_environment('cloud')
    if (application.mode != 'cloud' or application.project_id != options.project
            or application.agent_region != options.location
            or study.target.project_id != options.project
            or study.target.location != options.location
            or study.target.resource_name != options.resource
            or not study.agent_service_account.endswith('@' + options.project + '.iam.gserviceaccount.com')
            or study.backend_service_account != application.identities.backend_service_account
            or chat.provider != 'vertex' or chat.project_id != options.project
            or chat.location != application.model_location):
        raise DeploymentError('settings_target_or_identity_mismatch')
    artifacts = inspect_artifacts(options.wheel, options.requirements)
    digest = hashlib.sha256(artifacts.wheel).hexdigest()
    if options.wheel_sha256 and options.wheel_sha256 != digest:
        raise DeploymentError('wheel_sha256_mismatch')
    # The SDK pickles installed class definitions. Require that the wheel ships
    # those same source bytes, including the new runtime and grant boundary.
    package = ROOT / 'src' / 'campuspulse_recovery'
    with ZipFile(BytesIO(artifacts.wheel)) as archive:
        for source in package.rglob('*.py'):
            if '__pycache__' in source.parts:
                continue
            name = 'campuspulse_recovery/' + source.relative_to(package).as_posix()
            if archive.read(name).replace(b'\r\n', b'\n') != source.read_bytes().replace(b'\r\n', b'\n'):
                raise DeploymentError('stale_product_wheel')
    runtime = StudyAgentRuntime(application, study, chat)
    if runtime.register_operations() != {'async': ['async_query']}:
        raise DeploymentError('read_only_runtime_operation_required')
    from vertexai.agent_engines import _agent_engines as sdk
    schemas = sdk._generate_class_methods_spec_or_raise(
        agent_engine=runtime, operations=runtime.register_operations())
    if len(schemas) != 1 or schemas[0]['name'] != 'async_query':
        raise DeploymentError('unexpected_runtime_operation_schema')
    return runtime, application, study, artifacts, digest


def _environment(metadata):
    # Preserve existing environment and Secret Manager references, never reading
    # the referenced secrets. SDK update masks leave resource settings intact.
    deployment = metadata.get('spec', {}).get('deploymentSpec', {})
    result = {item['name']: item.get('value', '') for item in deployment.get('env', [])}
    for item in deployment.get('secretEnv', []):
        result[item['name']] = item['secretRef']
    result[TELEMETRY] = 'false'
    return result


@contextmanager
def _stage(artifacts):
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='campuspulse-study-agent-') as temporary:
        directory = Path(temporary)
        (directory / artifacts.wheel_name).write_bytes(artifacts.wheel)
        digest = hashlib.sha256(artifacts.wheel).hexdigest()
        requirements = (artifacts.requirements + b'\n' +
            (artifacts.wheel_name + ' --hash=sha256:' + digest + '\n').encode('utf-8'))
        (directory / 'deployment-requirements.txt').write_bytes(requirements)
        os.chdir(directory)
        try:
            yield
        finally:
            os.chdir(previous)


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--requirements', type=Path, default=ROOT / 'requirements/cloud-linux.lock')
    parser.add_argument('--wheel-sha256', help='Optional expected SHA-256 of the wheel.')
    parser.add_argument('--staging-bucket', required=True)
    parser.add_argument('--resource', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--location', required=True)
    parser.add_argument('--account', help='Existing gcloud login; no credential is persisted.')
    phase = 'validation'
    try:
        options = parser.parse_args(argv)
        if (options.project != PROJECT or options.location != LOCATION
                or options.resource != RESOURCE):
            raise DeploymentError('explicit_existing_study_engine_required')
        if re.fullmatch(r'gs://[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]', options.staging_bucket) is None:
            raise DeploymentError('staging_bucket_without_prefix_required')
        if options.wheel_sha256 and re.fullmatch(r'[a-f0-9]{64}', options.wheel_sha256) is None:
            raise DeploymentError('invalid_wheel_sha256')
        logging.disable(logging.CRITICAL)
        runtime, application, study, artifacts, digest = _runtime_and_artifacts(options)
        phase = 'authentication'
        token = _access_token(options.account)
        phase = 'target_inspection'
        project = _get_json('https://cloudresourcemanager.googleapis.com/v1/projects/' + PROJECT, token)
        if str(project.get('projectNumber')) != PROJECT_NUMBER or project.get('lifecycleState') != 'ACTIVE':
            raise DeploymentError('actual_project_identity_required')
        base = 'https://' + LOCATION + '-aiplatform.googleapis.com/v1/'
        before = _get_json(base + RESOURCE, token)
        if (before.get('name') != RESOURCE or before.get('displayName') != DISPLAY_NAME
                or before.get('spec', {}).get('serviceAccount') != study.agent_service_account):
            raise DeploymentError('existing_engine_name_or_identity_mismatch')
        from google.oauth2.credentials import Credentials
        import vertexai
        from vertexai import agent_engines
        vertexai.init(project=PROJECT, location=LOCATION,
                      staging_bucket=options.staging_bucket, credentials=Credentials(token))
        engine = agent_engines.get(RESOURCE)
        if engine.resource_name != RESOURCE:
            raise DeploymentError('sdk_engine_resource_mismatch')
        emit(status='verified_update_starting', resource_name=RESOURCE,
             wheel_sha256=digest, sdk_version='1.165.1', calendar_writes=False)
        phase = 'engine_update'
        with _stage(artifacts):
            remote = engine.update(agent_engine=runtime,
                requirements='deployment-requirements.txt',
                extra_packages=[artifacts.wheel_name],
                gcs_dir_name='study-agent-' + uuid.uuid4().hex,
                service_account=study.agent_service_account,
                env_vars=_environment(before))
        phase = 'deployment_verification'
        if remote.resource_name != RESOURCE:
            raise DeploymentError('updated_engine_resource_mismatch')
        methods = remote.operation_schemas()
        if len(methods) != 1 or methods[0].get('name') != 'async_query':
            raise DeploymentError('updated_operation_schema_mismatch')
        emit(status='existing_engine_updated', resource_name=remote.resource_name,
             operation_schema=methods, wheel_sha256=digest, calendar_writes=False)
        return 0
    except Exception as error:
        reason = str(error) if isinstance(error, DeploymentError) else 'study_agent_deployment_failed'
        emit(status='failed', phase=phase, reason=reason)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
