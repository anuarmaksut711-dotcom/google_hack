"""Managed runtime tests never acquire credentials or contact Google."""
import asyncio
import socket

import pytest

from tests.lms.test_settings import valid_payload


def settings():
    value = valid_payload()
    value['chat'] = {'provider': 'vertex', 'project_id': value['application']['project_id'],
                     'location': 'global', 'model': 'gemini-synthetic-test'}
    return value


@pytest.fixture(autouse=True)
def no_cloud(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Agent Engine unit tests forbid network and ADC')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr('google.auth.default', forbidden)


def test_constructor_validates_vertex_settings_without_creating_clients():
    from campuspulse_recovery.agent_engine.runtime import AgentEngineRuntime
    raw = settings()
    runtime = AgentEngineRuntime(raw)
    assert runtime.register_operations() == {'async': ['async_query']}
    # The caller cannot mutate the runtime's saved configuration through this DTO.
    raw['chat']['provider'] = 'gemini_api'
    assert runtime.register_operations() == {'async': ['async_query']}


@pytest.mark.parametrize('change', ['api_key_provider', 'unknown_field', 'foreign_project'])
def test_runtime_rejects_ambiguous_or_foreign_configuration(change):
    from campuspulse_recovery.agent_engine.runtime import AgentEngineRuntime
    raw = settings()
    if change == 'api_key_provider':
        raw['chat'] = {'provider': 'gemini_api', 'model': 'gemini-synthetic-test'}
    elif change == 'unknown_field':
        raw['backend_token'] = 'secret-that-must-not-be-accepted'
    else:
        raw['chat']['project_id'] = 'foreign-project'
    with pytest.raises(ValueError):
        AgentEngineRuntime(raw)


def test_query_uses_private_worker_route_and_closes_each_lifespan(monkeypatch):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, Request
    from campuspulse_recovery.agent_engine import runtime as module
    seen = []
    @asynccontextmanager
    async def lifespan(app):
        seen.append('open')
        try:
            yield
        finally:
            seen.append('close')
    app = FastAPI(lifespan=lifespan)
    @app.post('/internal/invoke')
    async def invoke(request: Request):
        assert request.headers['Authorization'] == 'Bearer synthetic-service-token'
        assert request.headers['X-CampusPulse-Invocation'] == 'a' * 43
        assert not request.query_params and await request.json() == {}
        return {'read_calendar': {'source': 'saved-snapshot'}}
    @asynccontextmanager
    async def create(parsed):
        assert parsed.chat.provider == 'vertex'
        yield app
    monkeypatch.setattr(module, '_worker_app', create, raising=False)
    async def scenario():
        raw = settings()
        agent = module.AgentEngineRuntime(raw)
        raw['chat']['provider'] = 'gemini_api'
        for _ in range(2):
            result = await agent.async_query(invocation='a' * 43, service_token='synthetic-service-token')
            assert result == {'status_code': 200, 'payload': {'read_calendar': {'source': 'saved-snapshot'}}}
    asyncio.run(scenario())
    assert seen == ['open', 'close', 'open', 'close']
