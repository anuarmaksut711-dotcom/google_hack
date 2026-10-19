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


@pytest.mark.parametrize('invocation,token', [
    ('short', 'signed-token'), ('a' * 44, 'signed-token'), (None, 'signed-token'),
    ('a' * 43, ''), ('a' * 43, None), ('a' * 43, 'injected\r\nHeader: value'),
    ('a' * 43, 'x' * 11994), ('a' * 43, True),
], ids=['short-grant', 'long-grant', 'missing-grant', 'empty-token', 'missing-token',
        'header-injection', 'long-token', 'boolean-token'])
def test_invalid_credentials_are_rejected_before_worker_setup(monkeypatch, invocation, token):
    from campuspulse_recovery.agent_engine import runtime as module
    def forbidden(*args):
        raise AssertionError('Invalid request reached worker setup')
    monkeypatch.setattr(module, '_worker_app', forbidden)
    result = asyncio.run(module.AgentEngineRuntime(settings()).async_query(invocation, token))
    assert result == {'status_code': 403, 'payload': {'error': 'service_or_invocation_rejected'}}


def install_app(monkeypatch, handler, closed):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from campuspulse_recovery.agent_engine import runtime as module
    @asynccontextmanager
    async def factory(parsed):
        app = FastAPI()
        app.add_api_route('/internal/invoke', handler, methods=['POST'])
        try:
            yield app
        finally:
            closed.append(True)
    monkeypatch.setattr(module, '_worker_app', factory)
    return module.AgentEngineRuntime(settings())


@pytest.mark.parametrize('failure', ['exception', 'large', 'invalid_json', 'unexpected_status', 'worker_error'])
def test_failed_or_unbounded_worker_result_is_safe_and_never_retried(monkeypatch, failure, capsys, caplog):
    from fastapi.responses import Response, JSONResponse
    calls, closed = [], []
    async def handler():
        calls.append(True)
        if failure == 'exception':
            raise RuntimeError('private-token-and-prompt')
        if failure == 'large':
            return JSONResponse({'data': 'x' * 800001})
        if failure == 'invalid_json':
            return Response('private-token-and-prompt', media_type='text/plain')
        return JSONResponse({'error': 'private-token-and-prompt'},
                            status_code=500 if failure == 'unexpected_status' else 503)
    runtime = install_app(monkeypatch, handler, closed)
    result = asyncio.run(runtime.async_query('a' * 43, 'synthetic-token'))
    assert result == {'status_code': 503, 'payload': {'error': 'invocation_outcome_unconfirmed'}}
    assert calls == [True] and closed == [True]
    captured = capsys.readouterr()
    assert 'private-token-and-prompt' not in str(result) + captured.out + captured.err + caplog.text


def test_query_deadline_closes_resources_without_retry(monkeypatch):
    from campuspulse_recovery.agent_engine import runtime as module
    calls, closed = [], []
    async def handler():
        calls.append(True)
        await asyncio.sleep(10)
    runtime = install_app(monkeypatch, handler, closed)
    monkeypatch.setattr(module, '_QUERY_SECONDS', .02, raising=False)
    result = asyncio.run(runtime.async_query('a' * 43, 'synthetic-token'))
    assert result['status_code'] == 503
    assert calls == [True] and closed == [True]


def test_caller_cancellation_propagates_and_closes_resources(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        closed = []
        async def handler():
            entered.set()
            await asyncio.sleep(10)
        runtime = install_app(monkeypatch, handler, closed)
        task = asyncio.create_task(runtime.async_query('a' * 43, 'synthetic-token'))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]
    asyncio.run(scenario())
