"""Deployment uses the selected in-memory login and an existing bucket only."""
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


def module():
    return runpy.run_path(str(Path(__file__).resolve().parents[2] / 'deploy/study-agent.py'))


def test_storage_staging_receives_selected_credentials_and_restores_sdk_hook(monkeypatch):
    values = module()
    credentials, bucket = object(), object()
    calls = []
    class Client:
        def __init__(self, *, project, credentials):
            calls.append(('client', project, credentials))
        def get_bucket(self, name):
            calls.append(('get', name)); return bucket
        def create_bucket(self, *args, **kwargs):
            pytest.fail('Deployment must never create a bucket')
    monkeypatch.setattr('google.cloud.storage.Client', Client)
    original = object(); sdk = SimpleNamespace(_get_gcs_bucket=original)
    with values['_existing_staging_access'](sdk, credentials, project='project',
            location='region', staging_bucket='gs://existing'):
        assert sdk._get_gcs_bucket(project='project', location='region',
            staging_bucket='gs://existing', logger=None) is bucket
        with pytest.raises(values['DeploymentError'], match='staging_target_mismatch'):
            sdk._get_gcs_bucket(project='foreign', location='region', staging_bucket='gs://existing')
    assert sdk._get_gcs_bucket is original
    assert calls == [('client', 'project', credentials), ('get', 'existing')]


def test_missing_bucket_is_not_created_and_hook_restores_after_failure(monkeypatch):
    values = module()
    class Client:
        def __init__(self, **kwargs): pass
        def get_bucket(self, name): raise RuntimeError('private provider diagnostic')
        def create_bucket(self, *args, **kwargs): pytest.fail('No new bucket')
    monkeypatch.setattr('google.cloud.storage.Client', Client)
    original = object(); sdk = SimpleNamespace(_get_gcs_bucket=original)
    with pytest.raises(values['DeploymentError'], match='existing_staging_bucket_unavailable'):
        with values['_existing_staging_access'](sdk, object(), project='project',
                location='region', staging_bucket='gs://existing'):
            sdk._get_gcs_bucket(project='project', location='region', staging_bucket='gs://existing')
    assert sdk._get_gcs_bucket is original
