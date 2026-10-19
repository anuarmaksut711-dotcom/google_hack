"""A prepared directory is a local handoff, never a deployment receipt."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.agent_engine.test_artifacts import LOCK, wheel
from tests.agent_engine.test_runtime import settings, no_cloud


def test_bundle_contains_exact_hashed_inputs_and_sdk_usable_paths(tmp_path):
    from campuspulse_recovery.agent_engine.prepare import prepare_bundle
    output = tmp_path / 'prepared'
    product = wheel(tmp_path)
    result = prepare_bundle(settings(), wheel=product, requirements=LOCK, output=output)
    manifest = json.loads((output / 'manifest.json').read_text())
    assert result == manifest
    assert manifest['status'] == 'prepared_not_deployed' and manifest['cloud_calls'] == 0
    assert manifest['entrypoint'] == 'campuspulse_recovery.agent_engine.runtime.AgentEngineRuntime'
    assert set(path.name for path in output.iterdir()) == set(manifest['files']) | {'manifest.json'}
    for name, facts in manifest['files'].items():
        data = (output / name).read_bytes()
        assert facts == {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    assert (output / product.name).read_bytes() == product.read_bytes()
    assert (output / 'requirements.lock').read_bytes() == LOCK.read_bytes()
    saved = json.loads((output / 'settings.json').read_text())
    assert saved['chat']['provider'] == 'vertex' and saved['moodle']['expected_user_id'] == 42
    assert saved['application']['project_id'] == 'campus-test-42'
    config = json.loads((output / 'create-config.json').read_text())
    assert config['service_account'] == 'agentxx@campus-test-42.iam.gserviceaccount.com'
    assert config['requirements'] == 'requirements.lock'
    assert config['extra_packages'] == [product.name]
    assert config['env_vars']['GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY'] == 'false'
    import inspect
    from vertexai.agent_engines import AgentEngine
    inspect.signature(AgentEngine.create).bind(object(), **config)
    schema = json.loads((output / 'operations.json').read_text())
    assert len(schema) == 1 and schema[0]['name'] == 'async_query'
    if os.name != 'nt':
        assert output.stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())


def test_existing_bundle_is_never_overwritten(tmp_path):
    from campuspulse_recovery.agent_engine.prepare import prepare_bundle
    output = tmp_path / 'existing'
    output.mkdir()
    sentinel = output / 'friend-settings.json'
    sentinel.write_text('preserve this')
    with pytest.raises(FileExistsError):
        prepare_bundle(settings(), wheel=wheel(tmp_path), requirements=LOCK, output=output)
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_text() == 'preserve this'


def test_invalid_configuration_or_artifact_does_not_create_ready_directory(tmp_path):
    from campuspulse_recovery.agent_engine.prepare import prepare_bundle
    output = tmp_path / 'invalid'
    raw = settings()
    raw['chat'] = {'provider': 'gemini_api', 'model': 'gemini-synthetic-test'}
    with pytest.raises(ValueError):
        prepare_bundle(raw, wheel=wheel(tmp_path), requirements=LOCK, output=output)
    assert not output.exists()


def test_write_failure_removes_only_new_incomplete_bundle(tmp_path, monkeypatch):
    from campuspulse_recovery.agent_engine import prepare
    output = tmp_path / 'incomplete'
    original = prepare._write_private
    calls = []
    def fail(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError('synthetic disk full')
        return original(path, data)
    monkeypatch.setattr(prepare, '_write_private', fail)
    with pytest.raises(OSError):
        prepare.prepare_bundle(settings(), wheel=wheel(tmp_path), requirements=LOCK, output=output)
    assert not output.exists()
