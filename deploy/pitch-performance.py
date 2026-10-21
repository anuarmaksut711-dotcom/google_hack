"""Inspect or change only minimum warm capacity for the existing pitch services.

The default is a read-only dry run. --apply explicitly changes Engine and
Cloud Run minima; it never uploads code, reads user sources, edits IAM, or
changes Calendar data. Use --profile idle after the pitch to restore min=0.
Only the standard library and the existing gcloud login are required.
"""
import argparse
from copy import deepcopy
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request


PROJECT = 'project-e25873c6-e889-40bb-86d'
PROJECT_NUMBER = '403025736340'
LOCATION = 'us-central1'
RESOURCE = (f'projects/{PROJECT_NUMBER}/locations/{LOCATION}/'
            'reasoningEngines/8841397574196789248')
API_BASE = f'https://{LOCATION}-aiplatform.googleapis.com/v1/'
ENGINE_URL = API_BASE + RESOURCE
UPDATE_MASK = 'spec.deploymentSpec.minInstances'
PATCH_URL = ENGINE_URL + '?updateMask=' + UPDATE_MASK
DISPLAY_NAME = 'campuspulse-readonly-deadlines'
ENGINE_ACCOUNT = f'campuspulse-deadline-agent@{PROJECT}.iam.gserviceaccount.com'
SERVICE_ACCOUNTS = {
    'campuspulse': f'campuspulse-calendar@{PROJECT}.iam.gserviceaccount.com',
    'campuspulse-mcp': f'campuspulse-mcp@{PROJECT}.iam.gserviceaccount.com',
}
MIN_ANNOTATION = 'run.googleapis.com/minScale'
VOLATILE_ANNOTATIONS = {
    'run.googleapis.com/client-name', 'run.googleapis.com/client-version',
    'run.googleapis.com/operation-id', 'run.googleapis.com/lastModifier',
}


class PerformanceError(ValueError):
    """Static codes only: never include cloud, CLI or credential text."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise PerformanceError('invalid_cli_arguments')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def emit(**values):
    print(json.dumps(values, ensure_ascii=True), flush=True)


def _gcloud(args):
    executable = shutil.which('gcloud.cmd') or shutil.which('gcloud')
    if executable is None:
        raise PerformanceError('gcloud_not_found')
    result = subprocess.run([executable, *args], capture_output=True,
                            text=True, encoding='utf-8', timeout=180,
                            stdin=subprocess.DEVNULL, check=False)
    if result.returncode:
        raise PerformanceError('gcloud_command_failed')
    return result.stdout


def _token():
    value = _gcloud(['auth', 'print-access-token', '--quiet']).strip()
    if re.fullmatch(r'[A-Za-z0-9._~-]{32,4096}', value) is None:
        raise PerformanceError('gcloud_access_token_unavailable')
    return value


def _json(text):
    try:
        result = json.loads(text)
    except (ValueError, UnicodeError):
        raise PerformanceError('invalid_cloud_json') from None
    if not isinstance(result, dict):
        raise PerformanceError('invalid_cloud_object')
    return result


def _operation_url(name):
    prefix = rf'projects/{PROJECT_NUMBER}/locations/{LOCATION}/'
    child = r'(?:reasoningEngines/8841397574196789248/)?operations/[A-Za-z0-9_-]{1,128}'
    if not isinstance(name, str) or re.fullmatch(prefix + child, name) is None:
        raise PerformanceError('unexpected_operation_target')
    return API_BASE + name


def _request(method, url, token, body=None):
    # No arbitrary URL or redirects can receive the in-memory bearer token.
    if not (method == 'PATCH' and url == PATCH_URL or
            method == 'GET' and (url == ENGINE_URL or
                url.startswith(API_BASE) and _operation_url(url[len(API_BASE):]) == url)):
        raise PerformanceError('unexpected_api_target')
    payload = None if body is None else json.dumps(body).encode('utf-8')
    request = urllib.request.Request(url, data=payload, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=45) as response:
            data = response.read(2_000_001)
    except urllib.error.HTTPError as error:
        raise PerformanceError('cloud_http_' + str(error.code)) from None
    except (OSError, ValueError):
        raise PerformanceError('cloud_request_failed') from None
    if len(data) > 2_000_000:
        raise PerformanceError('cloud_response_too_large')
    return _json(data)


def _read_engine(token):
    result = _request('GET', ENGINE_URL, token)
    spec = result.get('spec', {})
    methods = spec.get('classMethods', [])
    if (result.get('name') != RESOURCE or result.get('displayName') != DISPLAY_NAME
            or spec.get('serviceAccount') != ENGINE_ACCOUNT or len(methods) != 1
            or methods[0].get('name') != 'async_query'
            or methods[0].get('apiMode', 'async') != 'async'):
        raise PerformanceError('existing_engine_identity_or_operation_mismatch')
    if type(_engine_min(result)) is not int or _engine_min(result) < 0:
        raise PerformanceError('invalid_engine_minimum')
    return result


def _engine_min(engine):
    # The actual existing API reports explicit zero too. Do not guess a default
    # if an unexpected response omits the field during a capacity change.
    deployment = engine['spec'].get('deploymentSpec', {})
    if 'minInstances' not in deployment:
        raise PerformanceError('engine_minimum_missing')
    return deployment['minInstances']


def _read_service(name):
    if name not in SERVICE_ACCOUNTS:
        raise PerformanceError('unexpected_service_target')
    raw = _gcloud(['run', 'services', 'describe', name, '--project=' + PROJECT,
                   '--region=' + LOCATION, '--quiet', '--format=json'])
    result = _json(raw)
    metadata = result.get('metadata', {})
    account = result.get('spec', {}).get('template', {}).get('spec', {}).get('serviceAccountName')
    if (metadata.get('name') != name or str(metadata.get('namespace')) != PROJECT_NUMBER
            or account != SERVICE_ACCOUNTS[name]):
        raise PerformanceError('existing_service_identity_mismatch')
    _service_min(result)
    return result


def _service_min(service):
    value = service.get('metadata', {}).get('annotations', {}).get(MIN_ANNOTATION, '0')
    if not isinstance(value, str) or re.fullmatch(r'[0-9]{1,3}', value) is None:
        raise PerformanceError('invalid_service_minimum')
    return int(value)


def _engine_invariants(engine):
    spec = deepcopy(engine['spec'])
    spec.setdefault('deploymentSpec', {}).pop('minInstances', None)
    return {'name': engine['name'], 'displayName': engine['displayName'], 'spec': spec}


def _ordered_traffic(values):
    return sorted(json.dumps(value, sort_keys=True) for value in values)


def _service_invariants(service):
    spec = deepcopy(service['spec'])
    spec['traffic'] = _ordered_traffic(spec.get('traffic', []))
    metadata, status = service['metadata'], service.get('status', {})
    annotations = {key: value for key, value in metadata.get('annotations', {}).items()
                   if key not in VOLATILE_ANNOTATIONS and key != MIN_ANNOTATION}
    return {'name': metadata['name'], 'namespace': metadata['namespace'],
            'labels': metadata.get('labels', {}), 'annotations': annotations, 'spec': spec,
            'active_traffic': _ordered_traffic(status.get('traffic', [])),
            'latest_ready': status.get('latestReadyRevisionName'),
            'latest_created': status.get('latestCreatedRevisionName')}


def _wait_operation(operation, token, seconds):
    deadline = time.monotonic() + seconds
    url = _operation_url(operation.get('name'))
    while True:
        if operation.get('done') is True:
            if 'error' in operation:
                raise PerformanceError('engine_operation_failed')
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PerformanceError('engine_operation_timeout')
        time.sleep(min(2, remaining))
        operation = _request('GET', url, token)
        if _operation_url(operation.get('name')) != url:
            raise PerformanceError('unexpected_operation_target')


def _apply_engine(before, target, token, poll_seconds):
    if _engine_min(before) != target:
        operation = _request('PATCH', PATCH_URL, token,
            {'name': RESOURCE, 'spec': {'deploymentSpec': {'minInstances': target}}})
        _wait_operation(operation, token, poll_seconds)
    after = _read_engine(token)
    if (_engine_min(after) != target or
            _engine_invariants(before) != _engine_invariants(after)):
        raise PerformanceError('engine_unrelated_configuration_changed')


def _apply_service(name, before, target):
    if _service_min(before) != target:
        _gcloud(['run', 'services', 'update', name, '--project=' + PROJECT,
                 '--region=' + LOCATION, '--min=' + str(target), '--quiet', '--format=json'])
    after = _read_service(name)
    if (_service_min(after) != target or
            _service_invariants(before) != _service_invariants(after)):
        raise PerformanceError('service_unrelated_configuration_changed')


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--location', required=True)
    parser.add_argument('--profile', required=True, choices=['pitch', 'idle'])
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--poll-seconds', type=int, default=600)
    phase = 'validation'
    try:
        options = parser.parse_args(argv)
        if options.project != PROJECT or options.location != LOCATION:
            raise PerformanceError('explicit_existing_pitch_targets_required')
        if not 10 <= options.poll_seconds <= 1800:
            raise PerformanceError('bounded_poll_seconds_required')
        phase = 'authentication'
        token = _token()
        phase = 'target_inspection'
        before = _read_engine(token)
        services = {name: _read_service(name) for name in SERVICE_ACCOUNTS}
        target = 1 if options.profile == 'pitch' else 0
        if before['spec'].get('deploymentSpec', {}).get('maxInstances', 100) < target:
            raise PerformanceError('engine_maximum_below_minimum')
        if not options.apply:
            emit(status='dry_run', profile=options.profile,
                 engine_minimum={'before': _engine_min(before), 'target': target},
                 service_minima={name: {'before': _service_min(value), 'target': target}
                                 for name, value in services.items()}, calendar_writes=False)
            return 0
        emit(status='verified_apply_starting', profile=options.profile, minimum_instances=target,
             calendar_writes=False)
        phase = 'engine_minimum_update'
        _apply_engine(before, target, token, options.poll_seconds)
        for name in SERVICE_ACCOUNTS:
            phase = 'website_minimum_update' if name == 'campuspulse' else 'mcp_minimum_update'
            _apply_service(name, services[name], target)
        emit(status='minimum_capacity_verified', profile=options.profile,
             minimum_instances=target, calendar_writes=False)
        return 0
    except Exception as error:
        reason = str(error) if isinstance(error, PerformanceError) else 'performance_configuration_failed'
        emit(status='failed', phase=phase, reason=reason)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
