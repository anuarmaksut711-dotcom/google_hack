"""Capacity changes must preserve serving code, identities and private config."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.error

import pytest


@pytest.fixture
def cli():
    path = Path(__file__).resolve().parents[2] / 'deploy' / 'pitch-performance.py'
    spec = importlib.util.spec_from_file_location('pitch_performance_test_target', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def engine(cli, minimum=0):
    return {'name': cli.RESOURCE, 'displayName': cli.DISPLAY_NAME,
        'spec': {'serviceAccount': cli.ENGINE_ACCOUNT,
            'classMethods': [{'name': 'async_query', 'apiMode': 'async'}],
            'agentFramework': 'custom',
            'packageSpec': {'pickleObjectGcsUri': 'gs://private/unchanged.pkl',
                            'requirementsGcsUri': 'gs://private/requirements.txt'},
            'deploymentSpec': {'minInstances': minimum, 'maxInstances': 1,
                'containerConcurrency': 2, 'resourceLimits': {'cpu': '1', 'memory': '2Gi'},
                'env': [{'name': 'PRIVATE', 'value': 'private-settings-canary'}],
                'secretEnv': [{'name': 'SECRET', 'secretRef': {'secret': 'unchanged', 'version': '3'}}]}}}


def service(cli, name, minimum=0):
    revision = name + '-existing-serving-revision'
    return {'metadata': {'name': name, 'namespace': cli.PROJECT_NUMBER,
        'labels': {'cloud.googleapis.com/location': cli.LOCATION},
        'annotations': {'run.googleapis.com/maxScale': '3', cli.MIN_ANNOTATION: str(minimum)}},
        'spec': {'traffic': [{'revisionName': revision, 'percent': 100}],
            'template': {'metadata': {'name': revision,
                'annotations': {'autoscaling.knative.dev/maxScale': '2'}},
                'spec': {'serviceAccountName': cli.SERVICE_ACCOUNTS[name],
                    'containerConcurrency': 8,
                    'containers': [{'image': 'private-image@sha256:unchanged',
                        'env': [{'name': 'PRIVATE', 'value': 'private-settings-canary'}],
                        'resources': {'limits': {'cpu': '1', 'memory': '1Gi'}}}]}}},
        'status': {'latestReadyRevisionName': revision, 'latestCreatedRevisionName': revision,
                   'traffic': [{'revisionName': revision, 'percent': 100}]}}


@pytest.fixture
def cloud(cli, monkeypatch):
    state = SimpleNamespace(engine=engine(cli), services={
        name: service(cli, name) for name in cli.SERVICE_ACCOUNTS},
        http=[], commands=[], after_patch=None, after_service=None)

    def request(method, url, token, body=None):
        state.http.append((method, url, token, deepcopy(body)))
        if method == 'GET':
            assert url == cli.ENGINE_URL
            return deepcopy(state.engine)
        assert method == 'PATCH' and url == cli.PATCH_URL
        assert body == {'name': cli.RESOURCE,
                        'spec': {'deploymentSpec': {'minInstances': body['spec']['deploymentSpec']['minInstances']}}}
        state.engine['spec']['deploymentSpec']['minInstances'] = body['spec']['deploymentSpec']['minInstances']
        if state.after_patch:
            state.after_patch(state.engine)
        return {'name': cli.RESOURCE + '/operations/test-update', 'done': True}

    def gcloud(args):
        state.commands.append(args)
        if args[:2] == ['auth', 'print-access-token']:
            return 'token-canary-' + 'a' * 40
        assert args[:2] == ['run', 'services']
        name = args[3]
        assert name in cli.SERVICE_ACCOUNTS
        assert '--project=' + cli.PROJECT in args
        assert '--region=' + cli.LOCATION in args
        if args[2] == 'update':
            assert args[4:] == ['--project=' + cli.PROJECT, '--region=' + cli.LOCATION,
                                next(a for a in args if a.startswith('--min=')),
                                '--quiet', '--format=json']
            minimum = next(a.split('=', 1)[1] for a in args if a.startswith('--min='))
            state.services[name]['metadata']['annotations'][cli.MIN_ANNOTATION] = minimum
            if state.after_service:
                state.after_service(state.services[name])
        else:
            assert args[2] == 'describe'
        return json.dumps(state.services[name])

    monkeypatch.setattr(cli, '_request', request)
    monkeypatch.setattr(cli, '_gcloud', gcloud)
    return state


def args(cli, profile='pitch', apply=False):
    return ['--project', cli.PROJECT, '--location', cli.LOCATION, '--profile', profile] + (
        ['--apply'] if apply else [])


def test_dry_run_inspects_but_never_patches_or_updates(cli, cloud, capsys):
    assert cli.main(args(cli)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'dry_run'
    assert output['engine_minimum'] == {'before': 0, 'target': 1}
    assert set(output['service_minima']) == set(cli.SERVICE_ACCOUNTS)
    assert all(method == 'GET' for method, *_ in cloud.http)
    assert not any(command[:3] == ['run', 'services', 'update'] for command in cloud.commands)


def test_pitch_only_changes_exact_minima_and_preserves_all_other_configuration(cli, cloud, capsys):
    before_engine = deepcopy(cloud.engine)
    before_services = deepcopy(cloud.services)
    assert cli.main(args(cli, apply=True)) == 0
    patches = [call for call in cloud.http if call[0] == 'PATCH']
    assert len(patches) == 1
    assert patches[0][1] == (cli.API_BASE + cli.RESOURCE +
                             '?updateMask=spec.deploymentSpec.minInstances')
    assert patches[0][3] == {'name': cli.RESOURCE, 'spec': {'deploymentSpec': {'minInstances': 1}}}
    before_engine['spec']['deploymentSpec']['minInstances'] = 1
    assert cloud.engine == before_engine
    for name, before in before_services.items():
        before['metadata']['annotations'][cli.MIN_ANNOTATION] = '1'
        assert cloud.services[name] == before
    output = capsys.readouterr().out
    assert 'minimum_capacity_verified' in output
    assert 'private-settings-canary' not in output and 'token-canary' not in output
    assert 'unchanged.pkl' not in output


def test_idle_restores_explicit_zero_without_touching_code_or_traffic(cli, cloud, capsys):
    original = deepcopy((cloud.engine, cloud.services))
    assert cli.main(args(cli, apply=True)) == 0
    assert cli.main(args(cli, profile='idle', apply=True)) == 0
    assert (cloud.engine, cloud.services) == original
    updates = [c for c in cloud.commands if c[:3] == ['run', 'services', 'update']]
    assert sum('--min=0' in c for c in updates) == 2
    assert not any(any(arg.startswith('--min-instances') for arg in c) for c in updates)
    assert '"profile": "idle"' in capsys.readouterr().out


def test_already_warm_apply_is_read_only_and_still_verifies(cli, cloud):
    cloud.engine['spec']['deploymentSpec']['minInstances'] = 1
    for value in cloud.services.values():
        value['metadata']['annotations'][cli.MIN_ANNOTATION] = '1'
    assert cli.main(args(cli, apply=True)) == 0
    assert len(cloud.http) == 2  # Initial inspection and final verification.
    assert all(call[0] == 'GET' for call in cloud.http)
    assert not any(c[:3] == ['run', 'services', 'update'] for c in cloud.commands)


@pytest.mark.parametrize('mutation', [
    lambda value: value['spec'].update(serviceAccount='wrong-account'),
    lambda value: value['spec'].update(classMethods=[{'name': 'query'}]),
    lambda value: value.update(name='projects/another/locations/us-central1/reasoningEngines/other'),
])
def test_wrong_engine_identity_or_operation_prevents_all_mutations(cli, cloud, mutation, capsys):
    mutation(cloud.engine)
    assert cli.main(args(cli, apply=True)) == 2
    assert all(call[0] == 'GET' for call in cloud.http)
    assert not any(c[:3] == ['run', 'services', 'update'] for c in cloud.commands)
    assert 'existing_engine_identity_or_operation_mismatch' in capsys.readouterr().out


def test_missing_explicit_engine_minimum_fails_closed(cli, cloud, capsys):
    cloud.engine['spec']['deploymentSpec'].pop('minInstances')
    assert cli.main(args(cli, apply=True)) == 2
    assert 'engine_minimum_missing' in capsys.readouterr().out
    assert all(call[0] == 'GET' for call in cloud.http)


def test_wrong_service_identity_prevents_engine_and_service_changes(cli, cloud, capsys):
    cloud.services['campuspulse-mcp']['spec']['template']['spec']['serviceAccountName'] = 'wrong'
    assert cli.main(args(cli, apply=True)) == 2
    assert 'existing_service_identity_mismatch' in capsys.readouterr().out
    assert all(call[0] == 'GET' for call in cloud.http)


def test_unrelated_engine_change_is_detected_before_service_update(cli, cloud, capsys):
    cloud.after_patch = lambda value: value['spec']['deploymentSpec'].update(maxInstances=9)
    assert cli.main(args(cli, apply=True)) == 2
    assert 'engine_unrelated_configuration_changed' in capsys.readouterr().out
    assert not any(c[:3] == ['run', 'services', 'update'] for c in cloud.commands)


@pytest.mark.parametrize('mutation', [
    lambda value: value['spec']['template']['spec']['containers'][0].update(image='unexpected-image'),
    lambda value: value['status']['traffic'][0].update(percent=50),
    lambda value: value['metadata']['annotations'].update({'run.googleapis.com/maxScale': '9'}),
])
def test_unrelated_service_image_traffic_or_limits_change_fails_verification(cli, cloud, mutation, capsys):
    cloud.after_service = mutation
    assert cli.main(args(cli, apply=True)) == 2
    out = capsys.readouterr().out
    assert 'service_unrelated_configuration_changed' in out
    assert 'private-settings-canary' not in out


@pytest.mark.parametrize('operation', [
    'https://evil.example/steal-token',
    'projects/other/locations/us-central1/operations/x',
    'projects/403025736340/locations/europe-west1/operations/x',
    'projects/403025736340/locations/us-central1/reasoningEngines/other/operations/x',
])
def test_operation_poll_never_follows_a_foreign_target(cli, operation):
    with pytest.raises(cli.PerformanceError, match='unexpected_operation_target'):
        cli._operation_url(operation)


def test_actual_http_layer_rejects_foreign_urls_before_network(cli, monkeypatch):
    def unexpected(*_):
        pytest.fail('Network opener must not be constructed for a foreign URL')
    monkeypatch.setattr(cli.urllib.request, 'build_opener', unexpected)
    with pytest.raises(cli.PerformanceError, match='unexpected_api_target'):
        cli._request('GET', 'https://evil.example/private', 'token-canary')


def test_http_error_body_and_url_are_not_exposed(cli, monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError('https://private/token-canary', 403,
                                         'private-settings-canary', {}, None)
    monkeypatch.setattr(cli.urllib.request, 'build_opener', lambda *_: Opener())
    with pytest.raises(cli.PerformanceError) as caught:
        cli._request('GET', cli.ENGINE_URL, 'token-canary')
    assert str(caught.value) == 'cloud_http_403'


def test_operation_failure_is_static_and_redacted(cli, cloud, monkeypatch, capsys):
    original = cli._request
    def failed(method, url, token, body=None):
        if method == 'PATCH':
            return {'name': cli.RESOURCE + '/operations/failed', 'done': True,
                    'error': {'message': 'token-canary private-settings-canary'}}
        return original(method, url, token, body)
    monkeypatch.setattr(cli, '_request', failed)
    assert cli.main(args(cli, apply=True)) == 2
    out = capsys.readouterr().out
    assert 'engine_operation_failed' in out
    assert 'canary' not in out


def test_operation_poll_is_bounded(cli, monkeypatch):
    ticks = iter([0, 0.1, 10])
    monkeypatch.setattr(cli.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(cli.time, 'sleep', lambda _: None)
    op = {'name': cli.RESOURCE + '/operations/waiting'}
    monkeypatch.setattr(cli, '_request', lambda *_: op)
    with pytest.raises(cli.PerformanceError, match='engine_operation_timeout'):
        cli._wait_operation(op, 'token-canary', 10)


def test_gcloud_failure_cannot_print_captured_credentials(cli, monkeypatch):
    monkeypatch.setattr(cli.shutil, 'which', lambda _: '/fixed/gcloud')
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=1, stdout='token-canary', stderr='private-settings-canary'))
    with pytest.raises(cli.PerformanceError) as caught:
        cli._gcloud(['auth', 'print-access-token', '--quiet'])
    assert str(caught.value) == 'gcloud_command_failed'


def test_unexpected_exception_and_invalid_arguments_are_redacted(cli, monkeypatch, capsys):
    def secret_error():
        raise RuntimeError('token-canary private-settings-canary')
    monkeypatch.setattr(cli, '_token', secret_error)
    assert cli.main(args(cli)) == 2
    assert cli.main(args(cli) + ['--private-settings-canary']) == 2
    out = capsys.readouterr()
    assert 'canary' not in out.out and out.err == ''
    assert 'performance_configuration_failed' in out.out
    assert 'invalid_cli_arguments' in out.out


def test_wrong_project_or_location_is_rejected_before_authentication(cli, monkeypatch, capsys):
    monkeypatch.setattr(cli, '_token', lambda: pytest.fail('No authentication for an invalid target'))
    assert cli.main(['--project', 'wrong', '--location', cli.LOCATION, '--profile', 'pitch']) == 2
    assert 'explicit_existing_pitch_targets_required' in capsys.readouterr().out
