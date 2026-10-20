"""Custom-operation transport contract; external SDK I/O is the only fake edge."""
import asyncio
from types import SimpleNamespace

import pytest

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
