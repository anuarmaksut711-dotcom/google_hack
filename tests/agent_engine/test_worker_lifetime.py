"""Exercise production worker startup/cleanup with only the Google client edge fake."""
import asyncio

import pytest

from campuspulse_recovery.agent_engine.runtime import AgentEngineRuntime
from tests.agent_engine.test_runtime import settings, no_cloud


@pytest.mark.parametrize('startup', ['valid', 'target_mismatch'])
def test_default_factory_closes_google_client_on_rejection_or_startup_failure(monkeypatch, startup):
    monkeypatch.delenv('FIRESTORE_EMULATOR_HOST', raising=False)
    monkeypatch.delenv('GOOGLE_APPLICATION_CREDENTIALS', raising=False)
    closed, targets = [], []
    class Client:
        def __init__(self, *, project, database):
            targets.append((project, database))
            self.project = project if startup == 'valid' else 'foreign-project'
            self._database = database
        async def close(self):
            closed.append(True)
    monkeypatch.setattr('google.cloud.firestore.AsyncClient', Client)
    def invalid_signature(*args, **kwargs):
        raise ValueError('synthetic signature failure')
    monkeypatch.setattr('google.oauth2.id_token.verify_oauth2_token', invalid_signature)
    result = asyncio.run(AgentEngineRuntime(settings()).async_query('a' * 43, 'synthetic.token'))
    assert result['status_code'] == (403 if startup == 'valid' else 503)
    assert targets == [('campus-test-42', '(default)')]
    assert closed == [True]


@pytest.mark.parametrize('override', ['FIRESTORE_EMULATOR_HOST', 'GOOGLE_APPLICATION_CREDENTIALS'])
def test_default_factory_rejects_credential_overrides_before_client_creation(monkeypatch, override):
    monkeypatch.setenv(override, 'synthetic-forbidden-override')
    def forbidden(*args, **kwargs):
        raise AssertionError('Forbidden credential override reached Google client creation')
    monkeypatch.setattr('google.cloud.firestore.AsyncClient', forbidden)
    result = asyncio.run(AgentEngineRuntime(settings()).async_query('a' * 43, 'synthetic.token'))
    assert result['status_code'] == 503
