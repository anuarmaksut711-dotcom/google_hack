"""Custom-operation transport contract; external SDK I/O is the only fake edge."""
import asyncio
import builtins
import json
import threading
from types import SimpleNamespace

import pytest
import httpx

from campuspulse_recovery.domain.ports import PortError
from campuspulse_recovery.web.agent_client import AgentEngineTarget

TARGET = AgentEngineTarget(project_id='campus-test-42', location='us-central1',
    resource_name='projects/123/locations/us-central1/reasoningEngines/456')


def test_custom_query_unwraps_payload_and_closes_without_stream_query():
    from campuspulse_recovery.cloud.engine_transport import AgentEngineTransport
    async def scenario():
        calls, closed = [], []
        async def query(**kwargs):
            calls.append(kwargs)
            return {'status_code': 200, 'payload': {'read_assignments': {'safe': True}}}
        async def token(_): return {'Authorization': 'Bearer signed.backend.token'}
        class Client:
            def __init__(self, **kwargs):
                assert kwargs['project'] == 'campus-test-42'
                assert kwargs['http_options']['retry_options']['attempts'] == 1
                self.agent_engines = SimpleNamespace(get=self.get)
                self.aio = SimpleNamespace(aclose=self.close_async)
                self._api_client = SimpleNamespace(close=lambda: closed.append('sync'))
            def get(self, *, name, config):
                assert name == TARGET.resource_name
                return SimpleNamespace(async_query=query)
            async def close_async(self): closed.append('async')
        result = await AgentEngineTransport(TARGET, audience='https://agent.example.run.app',
            token_factory=token, client_factory=Client).request('a' * 43, seconds=1)
        assert result == {'read_assignments': {'safe': True}}
        assert calls == [{'invocation': 'a' * 43, 'service_token': 'signed.backend.token'}]
        assert closed == ['async', 'sync']
    asyncio.run(scenario())


async def _service_token(_):
    return {'Authorization': 'Bearer signed.backend.token'}


async def _access_token(seconds):
    return 'platform-access-token'


def rest_transport(handler, *, token_factory=_service_token, access_token_factory=_access_token,
                   factory_options=None):
    from campuspulse_recovery.cloud.engine_transport import AgentEngineTransport
    def http(**kwargs):
        if factory_options is not None:
            factory_options.append(kwargs)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    return AgentEngineTransport(TARGET, audience='https://agent.example.run.app',
        token_factory=token_factory, access_token_factory=access_token_factory, http_client_factory=http)


def test_production_rest_uses_fixed_operation_and_separate_platform_and_service_tokens_without_sdk(monkeypatch):
    imports = builtins.__import__
    def no_vertex(name, *args, **kwargs):
        if name == 'vertexai' or name.startswith('vertexai.'):
            raise AssertionError('Production requests must not import the Vertex SDK')
        return imports(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_vertex)
    async def scenario():
        calls, options = [], []
        async def handler(request):
            calls.append(request)
            assert str(request.url) == ('https://us-central1-aiplatform.googleapis.com/v1/'
                                       + TARGET.resource_name + ':query')
            assert request.method == 'POST'
            assert request.headers['authorization'] == 'Bearer platform-access-token'
            assert json.loads(request.content) == {'class_method': 'async_query', 'input': {
                'invocation': 'a' * 43, 'service_token': 'signed.backend.token'}}
            return httpx.Response(200, json={'output': {'status_code': 200,
                                                       'payload': {'reply': 'План готов'}}})
        result = await rest_transport(handler, factory_options=options).request('a' * 43, seconds=1)
        assert result == {'reply': 'План готов'} and len(calls) == 1
        assert options[0]['trust_env'] is False and options[0]['follow_redirects'] is False
    asyncio.run(scenario())


@pytest.mark.parametrize('status,content,headers', [
    (302, b'private-redirect', {'location': 'https://other.example/secret'}),
    (403, b'private-provider-error', {}), (500, b'private-provider-error', {}),
    (200, b'not JSON private data', {}), (200, b'\xff', {}),
    (200, b'{"output":null}', {}), (200, b'{"output":{"status_code":503,"payload":{}}}', {}),
    (200, b'{"output":{"status_code":true,"payload":{}}}', {}),
    (200, b'{"output":{"status_code":200,"payload":{}} ,"extra":"private"}', {}),
    (200, b'{"output":{"status_code":200,"payload":{}}}', {'content-length': '800001'}),
])
def test_rest_rejections_are_redacted_and_never_retry_or_follow_redirect(status, content, headers):
    async def scenario():
        calls = []
        async def handler(request):
            calls.append(request)
            return httpx.Response(status, content=content, headers=headers)
        with pytest.raises(PortError, match='agent_engine_outcome_unconfirmed') as error:
            await rest_transport(handler).request('a' * 43, seconds=1)
        assert 'private' not in str(error.value) and len(calls) == 1
    asyncio.run(scenario())


def test_rest_stream_is_size_bounded_without_content_length_and_always_closed():
    async def scenario():
        class Stream(httpx.AsyncByteStream):
            def __init__(self):
                self.closed, self.yielded = False, 0
            async def __aiter__(self):
                for _ in range(100):
                    self.yielded += 65536
                    yield b'x' * 65536
            async def aclose(self):
                self.closed = True
        stream = Stream()
        async def handler(request):
            return httpx.Response(200, stream=stream)
        with pytest.raises(PortError):
            await rest_transport(handler).request('a' * 43, seconds=1)
        assert stream.closed and 800000 < stream.yielded <= 800000 + 65536
    asyncio.run(scenario())


@pytest.mark.parametrize('value', ['', 'bad\r\nheader', 'has spaces', None, True])
@pytest.mark.parametrize('kind', ['service', 'platform'])
def test_rest_invalid_tokens_never_dispatch_a_query(value, kind):
    async def scenario():
        async def service(_): return {'Authorization': value if value is None else 'Bearer ' + str(value)}
        async def access(_): return value
        async def forbidden(request):
            pytest.fail('Invalid credential reached the Vertex endpoint')
        # A literal True string is not an actual service-token type error; use
        # the unmodified Authorization value so it cannot acquire a Bearer prefix.
        if kind == 'service' and value is True:
            async def service(_): return {'Authorization': True}
        with pytest.raises(PortError):
            await rest_transport(forbidden,
                token_factory=service if kind == 'service' else _service_token,
                access_token_factory=access if kind == 'platform' else _access_token).request('a' * 43, seconds=1)
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', [False, True])
def test_rest_total_timeout_and_caller_cancellation_stop_pending_request_without_retry(cancel):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls, aborted = [], []
        async def handler(request):
            calls.append(request)
            entered.set()
            try:
                await release.wait()
            finally:
                aborted.append(True)
            return httpx.Response(200, json={'output': {'status_code': 200, 'payload': {}}})
        task = asyncio.create_task(rest_transport(handler).request('a' * 43, seconds=.05 if not cancel else 1))
        await entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert task.cancelled()
        else:
            with pytest.raises(PortError, match='outcome_unconfirmed'): await task
        assert len(calls) == 1 and aborted == [True]
    asyncio.run(scenario())


def test_platform_adc_discovery_and_refresh_run_off_event_loop_with_bounded_request(monkeypatch):
    from campuspulse_recovery.cloud.engine_transport import _platform_access_token
    observations, main_thread = [], threading.get_ident()
    class Request:
        def __init__(self, *, session): pass
        def __call__(self, *args, **kwargs):
            observations.append(('http_timeout', kwargs['timeout']))
    class Credentials:
        valid, token = False, None
        def refresh(self, request):
            observations.append(('refresh_thread', threading.get_ident()))
            request('http://metadata.test/', timeout=999)
            self.token = 'platform-access-token'
    def adc(*, scopes, request):
        assert scopes == ['https://www.googleapis.com/auth/cloud-platform']
        observations.append(('adc_thread', threading.get_ident()))
        return Credentials(), 'platform-project'
    monkeypatch.setattr('google.auth.default', adc)
    monkeypatch.setattr('google.auth.transport.requests.Request', Request)
    assert asyncio.run(_platform_access_token(2)) == 'platform-access-token'
    assert ('http_timeout', 2) in observations
    assert all(value != main_thread for kind, value in observations if kind.endswith('_thread'))


def test_pending_production_transport_keeps_independent_web_get_responsive():
    from fastapi import FastAPI
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={'output': {'status_code': 200, 'payload': {'ok': True}}})
        transport = rest_transport(handler)
        app = FastAPI()
        @app.post('/query')
        async def query(): return await transport.request('a' * 43, seconds=2)
        @app.get('/progress')
        async def progress(): return {'phase': 'queued'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://app.test') as browser:
            posting = asyncio.create_task(browser.post('/query'))
            await entered.wait()
            response = await asyncio.wait_for(browser.get('/progress'), timeout=.25)
            assert response.json() == {'phase': 'queued'}
            release.set()
            assert (await posting).json() == {'ok': True}
    asyncio.run(scenario())


@pytest.mark.parametrize('response', [None, {'status_code': 503, 'payload': {}},
    {'status_code': 403, 'payload': {}}, {'status_code': 200, 'payload': 'verified'},
    {'status_code': True, 'payload': {}}, {'status_code': 200, 'payload': {}, 'extra': 'private'},
    {'status_code': 200, 'payload': {'body': 'a' * 800_001}}])
def test_malformed_or_rejected_operation_never_retries(response):
    from campuspulse_recovery.cloud.engine_transport import AgentEngineTransport
    async def scenario():
        calls = []
        async def query(**kwargs):
            calls.append(kwargs)
            return response
        async def token(_): return {'Authorization': 'Bearer signed.backend.token'}
        class Client:
            def __init__(self, **kwargs):
                self.agent_engines = SimpleNamespace(get=lambda **kw: SimpleNamespace(async_query=query))
                self.aio = SimpleNamespace(aclose=self.close)
                self._api_client = SimpleNamespace(close=lambda: None)
            async def close(self): pass
        with pytest.raises(PortError, match='outcome_unconfirmed'):
            await AgentEngineTransport(TARGET, audience='https://agent.example.run.app',
                token_factory=token, client_factory=Client).request('a' * 43, seconds=1)
        assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', [False, True])
def test_cleanup_failure_is_redacted_and_cannot_replace_cancellation(cancel):
    from campuspulse_recovery.cloud.engine_transport import AgentEngineTransport
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        closed = []
        async def query(**kwargs):
            entered.set()
            if cancel:
                await finish.wait()
            return {'status_code': 200, 'payload': {}}
        async def token(_): return {'Authorization': 'Bearer signed.backend.token'}
        class Client:
            def __init__(self, **kwargs):
                self.agent_engines = SimpleNamespace(get=lambda **kw: SimpleNamespace(async_query=query))
                self.aio = SimpleNamespace(aclose=self.close_async)
                self._api_client = SimpleNamespace(close=lambda: closed.append('sync'))
            async def close_async(self):
                closed.append('async')
                raise RuntimeError('private-cleanup-details')
        task = asyncio.create_task(AgentEngineTransport(TARGET, audience='https://agent.example.run.app',
            token_factory=token, client_factory=Client).request('a' * 43, seconds=2))
        await entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert task.cancelled()
        else:
            with pytest.raises(PortError, match='outcome_unconfirmed') as error:
                await task
            assert 'private-cleanup' not in str(error.value)
        assert closed == ['async', 'sync']
    asyncio.run(scenario())
