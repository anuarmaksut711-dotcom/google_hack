"""Real backend/ADK/MCP flow through the managed runtime, with synthetic edges."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta

import pytest

from campuspulse_recovery.agent_engine import runtime as module
from campuspulse_recovery.cloud import agent, authority
from campuspulse_recovery.cloud.transport import CloudTransport
from campuspulse_recovery.domain.ports import PortError
from campuspulse_recovery.storage.plans import _decode, _encode
from campuspulse_recovery.web.auth import WebSession
from tests.lms.test_planning_backend import Backend, loopback_only


@asynccontextmanager
async def engine(h, monkeypatch):
    raw = h.settings.model_dump()
    raw['chat'] = {'provider': 'vertex', 'project_id': h.policy.config.project_id,
                   'location': 'global', 'model': 'gemini-synthetic-test'}
    runtime = module.AgentEngineRuntime(raw)
    @asynccontextmanager
    async def factory(parsed):
        # Substitute only Firestore/ADC startup; all worker dispatch stays real.
        yield agent.create_agent(parsed, h.policy, h.documents, close=lambda: None)
    monkeypatch.setattr(module, '_worker_app', factory)
    original = CloudTransport.request
    calls = []
    async def request(transport, reference, *, path='/internal/invoke', seconds=150):
        if transport.origin != h.settings.agent_origin:
            return await original(transport, reference, path=path, seconds=seconds)
        result = await runtime.async_query(reference, 'synthetic-service')
        calls.append((reference, result))
        if result['status_code'] != 200:
            raise PortError('managed_outcome_unconfirmed')
        return result['payload']
    monkeypatch.setattr(CloudTransport, 'request', request)
    yield runtime, calls


def test_moodle_propose_apply_verify_and_replay_through_engine(monkeypatch):
    async def scenario():
        h = Backend(monkeypatch)
        h.allow_mutations = True
        await h.seed()
        async with h.runtime(monkeypatch), engine(h, monkeypatch) as (runtime, calls), h.browser() as browser:
            headers = await h.login(browser)
            assert (await browser.post('/api/moodle/planning', headers=headers, json=h.update())).status_code == 200
            proposal = await browser.post('/api/runs', headers=headers, json={'request_key': 'engine-propose'})
            assert proposal.json()['status'] == 'proposed', proposal.text
            plan = proposal.json()['plan']
            approved = await browser.post('/api/approval', headers=headers,
                json={**plan, 'request_key': 'engine-consent'})
            assert approved.status_code == 200
            applied = await browser.post('/api/runs/continue', headers=headers,
                json={'plan': plan, 'request_key': 'engine-apply'})
            assert applied.json()['status'] == 'verified', applied.text
            assert applied.json()['verified_success'] is True
            assert h.tool_calls.count('apply_plan') == h.tool_calls.count('verify_plan') == 1
            assert len(h.api.mutations) == 2
            before, count = deepcopy(h.api.mutations), len(calls)
            replay = await browser.post('/api/runs/continue', headers=headers,
                json={'plan': plan, 'request_key': 'engine-apply'})
            assert replay.json() == applied.json()
            assert h.api.mutations == before and len(calls) == count
            assert len(calls) >= 3 and all(value['status_code'] == 200 for _, value in calls)
            # A second engine delivery cannot reclaim a consumed server grant.
            denied = await runtime.async_query(calls[0][0], 'synthetic-service')
            assert denied['status_code'] == 403
            assert h.api.mutations == before
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['expired_grant', 'revoked_session'])
def test_engine_rechecks_live_session_and_grant_before_tools(monkeypatch, change):
    async def scenario():
        h = Backend(monkeypatch)
        await h.seed()
        async with h.runtime(monkeypatch), engine(h, monkeypatch), h.browser() as browser:
            headers = await h.login(browser)
            assert (await browser.post('/api/moodle/planning', headers=headers, json=h.update())).status_code == 200
            original = module.AgentEngineRuntime.async_query
            rejected = []
            async def intercept(runtime, invocation, service_token):
                grant = await authority.Grants(h.documents, h.policy, clock=lambda: h.now).load(invocation)
                if change == 'expired_grant':
                    h.now = grant.expires_at
                else:
                    raw = await h.documents.get(grant.session_path)
                    session = _decode(raw, 'web_session', WebSession)
                    h.documents.data[grant.session_path] = _encode('web_session', session.model_copy(update={'revoked': True}))
                result = await original(runtime, invocation, service_token)
                rejected.append(result)
                return result
            monkeypatch.setattr(module.AgentEngineRuntime, 'async_query', intercept)
            await browser.post('/api/runs', headers=headers, json={'request_key': 'engine-expired'})
            assert rejected and rejected[0]['status_code'] == 403
            assert not h.tool_calls and not h.model_calls and not h.api.mutations
    asyncio.run(scenario())


@pytest.mark.parametrize('cause', ['signature', 'audience', 'issuer', 'principal', 'expiry'])
def test_engine_worker_verifies_backend_token_before_grant_lookup(monkeypatch, cause):
    async def scenario():
        h = Backend(monkeypatch)
        raw = h.settings.model_dump()
        raw['chat'] = {'provider': 'vertex', 'project_id': h.policy.config.project_id,
                       'location': 'global', 'model': 'gemini-synthetic-test'}
        @asynccontextmanager
        async def factory(parsed):
            yield agent.create_agent(parsed, h.policy, h.documents, close=lambda: None)
        monkeypatch.setattr(module, '_worker_app', factory)
        def verify(token, request, *, audience):
            if cause == 'signature':
                raise ValueError('synthetic bad signature')
            now = h.now
            claims = {'iss': 'https://accounts.google.com', 'aud': audience,
                'email': h.policy.config.identities.backend_service_account, 'email_verified': True,
                'sub': 'synthetic-service-subject', 'iat': int((now - timedelta(seconds=1)).timestamp()),
                'exp': int((now + timedelta(minutes=5)).timestamp())}
            if cause == 'audience': claims['aud'] = h.settings.mcp_origin
            elif cause == 'issuer': claims['iss'] = 'https://foreign.test'
            elif cause == 'principal': claims['email'] = h.policy.config.identities.agent_service_account
            elif cause == 'expiry': claims['exp'] = int(now.timestamp())
            return claims
        monkeypatch.setattr('google.oauth2.id_token.verify_oauth2_token', verify)
        original = h.documents.get
        reads = []
        async def observe(path):
            reads.append(path)
            return await original(path)
        monkeypatch.setattr(h.documents, 'get', observe)
        result = await module.AgentEngineRuntime(raw).async_query('a' * 43, 'signed.backend.token')
        assert result == {'status_code': 403, 'payload': {'error': 'service_or_invocation_rejected'}}
        assert reads == [] and not h.documents.data
    asyncio.run(scenario())
