"""Backend -> real ADK -> real loopback MCP -> solver -> persisted proposal.

Google identity, Calendar, document transactions and model responses are synthetic.
No real credentials, university requests or Calendar mutations are used.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import socket
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from campuspulse_recovery.cloud import agent, authority, backend, mcp, writers
from campuspulse_recovery.cloud.planning import assignments_resource, planning_inputs
from campuspulse_recovery.domain.models import Interval
from campuspulse_recovery.lms.sync import MoodleSync
from campuspulse_recovery.security.resources import ResourcePolicy
from campuspulse_recovery.web.auth import VerifiedSignIn
from tests.integrations.test_calendar_read import event
from tests.integrations.test_calendar_write import FakeAPI
from tests.lms.test_planning import lms_snapshot
from tests.lms.test_settings import valid_payload, validate
from tests.storage.test_plans import FakeDocuments, NOW, interval


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', 'localhost', '::1'):
            raise AssertionError('Moodle planning tests forbid external network')
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    def credentials(*args, **kwargs):
        raise AssertionError('Moodle planning tests forbid real credentials')
    monkeypatch.setattr('google.auth.default', credentials)


class Backend:
    def __init__(self, monkeypatch):
        self.settings = validate(valid_payload())
        self.policy = ResourcePolicy(self.settings.application, environment='cloud',
            assignments_resource_id=assignments_resource(self.settings))
        self.documents, self.now = FakeDocuments(), NOW
        self.snapshot = lms_snapshot()
        self.origin = self.settings.backend_origin
        self.inputs = planning_inputs(self.settings, self.policy, self.documents, clock=lambda: self.now)
        self.model_calls, self.tool_calls, self.calendar_reads = [], [], []
        self.allow_mutations = False
        for module in (backend, agent, mcp, authority, writers):
            monkeypatch.setattr(module, 'utcnow', lambda: self.now)
        monkeypatch.delenv('GEMINI_API_KEY', raising=False)
        async def verify(verifier, token, *, nonce, now):
            assert token == 'synthetic-id-token' and nonce and now == self.now
            return VerifiedSignIn(principal_id=self.settings.application.student.allowed_signin_principal,
                authenticated_at=now, expires_at=now + timedelta(hours=1))
        monkeypatch.setattr('campuspulse_recovery.web.auth.GoogleSignInVerifier.verify', verify)
        self.app = backend.create_backend(self.settings, self.policy, self.documents, close=lambda: None)

    async def seed(self):
        async def fetch():
            return self.snapshot
        await MoodleSync(self.documents, owner_id=self.inputs.connection.owner_id,
            site_url=self.settings.moodle.site_url, expected_user_id=42,
            fetch_snapshot=fetch, clock=lambda: self.now).refresh()

    def browser(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=self.origin)

    async def login(self, browser):
        challenge = await browser.get('/auth/challenge')
        result = await browser.post('/auth/signin', json={'credential': 'synthetic-id-token'},
            headers={'Origin': self.origin, 'X-CSRF-Token': challenge.json()['csrf_token']})
        assert result.status_code == 200, result.text
        return {'Origin': self.origin, 'X-CSRF-Token': result.json()['csrf_token']}

    def update(self, **changes):
        return {'expected_revision': 0,
            'working_windows': [interval(NOW + timedelta(hours=1), hours=4).model_dump(mode='json')],
            'estimates': [{'task_id': 'assign:9', 'remaining_minutes': 90}], **changes}

    @asynccontextmanager
    async def runtime(self, monkeypatch):
        # Fake only external identity/Calendar/model edges. Production grants,
        # authenticated run binding, MCP dispatch, journal and services stay real.
        original = authority.BoundAuthority.__init__
        def bound(value, *args, **kwargs):
            original(value, *args, **kwargs)
            value.contexts._clock = lambda: self.now
        monkeypatch.setattr(authority.BoundAuthority, '__init__', bound)
        async def service_identity(value, request):
            assert request.headers.get('authorization') == 'Bearer synthetic-service'
            return self.now - timedelta(seconds=1), self.now + timedelta(hours=1)
        monkeypatch.setattr(authority.ServiceIdentity, 'verify', service_identity)

        owner = self
        from campuspulse_recovery.integrations import google_clients
        self.api = FakeAPI()
        busy = event(start=NOW + timedelta(hours=2), end=NOW + timedelta(hours=3))
        self.api.events[busy['id']] = busy
        self.api.native_zone = self.policy.config.student.timezone
        monkeypatch.setattr(google_clients, 'create_workspace_credentials', lambda *args, **kwargs: None)
        monkeypatch.setattr(google_clients, '_RequestsHttp', lambda *args, **kwargs: self.api)
        class Workspace(mcp.CloudWorkspaceClients):
            def sheet_read(value, *args, **kwargs):
                raise AssertionError('Moodle planning must not read Sheets')
            def calendar_request(value, context, *, calendar_id, request_factory, mutation=False):
                value.policy.require_calendar(context, calendar_id)
                assert not mutation or owner.allow_mutations, 'This case must not mutate even the fake Calendar'
                owner.calendar_reads.append(calendar_id)
                return super().calendar_request(context, calendar_id=calendar_id,
                    request_factory=request_factory, mutation=mutation)
        monkeypatch.setattr(mcp, 'CloudWorkspaceClients', Workspace)

        class Scripted(BaseLlm):
            def __init__(value):
                super().__init__(model='synthetic-not-Gemini')
                value._actions = None
            async def generate_content_async(value, llm_request, stream=False):
                owner.model_calls.append(True)
                if value._actions is None:
                    from campuspulse_recovery.agent.policy import current
                    state = current()
                    horizon = interval(hours=72).model_dump(mode='json')
                    value._actions = ([('apply_plan', state.allowed_apply.model_dump(mode='json')),
                        ('verify_plan', {'plan_id': state.allowed_plan.plan_id, 'version': state.allowed_plan.version})]
                        if state.mode == 'apply' else [('read_assignments', {'horizon': horizon}),
                            ('read_calendar', {'horizon': horizon}), ('solve_repair', {'request': {}})])
                if value._actions:
                    name, arguments = value._actions.pop(0)
                    response = types.Part(function_call=types.FunctionCall(name=name, args=deepcopy(arguments)))
                else:
                    response = types.Part(text='Synthetic candidate complete')
                yield LlmResponse(content=types.Content(role='model', parts=[response]))
        monkeypatch.setattr('campuspulse_recovery.agent.conversation.AdkConversationAgent._create_model',
                            lambda value: Scripted())

        original_mcp = mcp.create_complete_app
        def capture(**kwargs):
            self.mcp_services = kwargs
            return original_mcp(**kwargs)
        monkeypatch.setattr(mcp, 'create_complete_app', capture)
        mcp_app = mcp.create_mcp(self.settings, self.policy, self.documents, port=8080, close=lambda: None)
        agent_app = agent.create_agent(self.settings, self.policy, self.documents, close=lambda: None)
        apps = {self.settings.agent_origin: agent_app, self.settings.mcp_origin: mcp_app}
        async def invoke(transport, reference, *, path='/internal/invoke', seconds=150):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[transport.origin]),
                                         base_url=transport.origin) as client:
                result = await client.post(path, json={}, headers={
                    'Authorization': 'Bearer synthetic-service', 'X-CampusPulse-Invocation': reference})
                assert result.status_code == 200, result.text
                return result.json()
        monkeypatch.setattr('campuspulse_recovery.cloud.transport.CloudTransport.request', invoke)

        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(mcp_app, log_level='error', access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(200):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(.01)
            assert server.started
            port = listener.getsockname()[1]
            async def call(transport, reference, name, arguments):
                owner.tool_calls.append(name)
                async with httpx.AsyncClient(headers={'Authorization': 'Bearer synthetic-service',
                    'Host': urlsplit(owner.settings.mcp_origin).netloc,
                    'X-CampusPulse-Invocation': reference}, trust_env=False) as http:
                    async with streamable_http_client(f'http://127.0.0.1:{port}/mcp', http_client=http) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool(name, arguments)
                return result.model_dump(mode='json')
            monkeypatch.setattr('campuspulse_recovery.cloud.transport.McpTransport.call', call)
            yield
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)
            listener.close()


def test_planning_api_persists_partial_updates_and_checks_revision(monkeypatch):
    async def scenario():
        h = Backend(monkeypatch)
        await h.seed()
        async with h.browser() as browser:
            assert (await browser.get('/api/moodle/planning')).status_code == 403
            headers = await h.login(browser)
            assert (await browser.get('/api/moodle/planning')).json()['revision'] == 0
            first = await browser.post('/api/moodle/planning', headers=headers, json=h.update())
            assert first.status_code == 200, first.text
            assert first.json()['revision'] == 1
            assert not {'owner_id', 'site_url', 'expected_user_id'} & first.json().keys()
            assert (await browser.post('/api/moodle/planning', headers=headers, json=h.update())).status_code == 409
            second = await browser.post('/api/moodle/planning', headers=headers, json={
                'expected_revision': 1, 'estimates': [{'task_id': 'assign:9', 'remaining_minutes': None}]})
            assert second.status_code == 200, second.text
            assert second.json()['revision'] == 2
            assert second.json()['working_windows'] == first.json()['working_windows']
            assert (await browser.get('/api/moodle/planning')).json() == second.json()
    asyncio.run(scenario())


def test_full_moodle_rework_apply_verify_and_replay_with_synthetic_calendar(monkeypatch):
    async def scenario():
        h = Backend(monkeypatch)
        h.snapshot = lms_snapshot(status='graded', grade=0)
        h.allow_mutations = True
        await h.seed()
        async with h.runtime(monkeypatch), h.browser() as browser:
            headers = await h.login(browser)
            assert (await browser.post('/api/moodle/planning', headers=headers, json=h.update())).status_code == 200
            started = await browser.post('/api/runs', headers=headers, json={'request_key': 'complete-plan'})
            assert started.json()['status'] == 'proposed', started.text
            reference = started.json()['plan']
            assert (await browser.post('/api/approval', headers=headers,
                json={**reference, 'request_key': 'complete-approval'})).status_code == 200
            applied = await browser.post('/api/runs/continue', headers=headers,
                json={'request_key': 'complete-apply', 'plan': reference})
            assert applied.status_code == 200, applied.text
            assert applied.json()['status'] == 'verified', applied.text
            assert applied.json()['verified_success'] is True
            assert h.tool_calls[:2] == ['read_assignments', 'read_calendar']
            assert h.tool_calls[-2:] == ['apply_plan', 'verify_plan']
            assert h.tool_calls.count('apply_plan') == h.tool_calls.count('verify_plan') == 1
            assert len(h.api.mutations) == 2
            operations = deepcopy(h.api.mutations)
            replay = await browser.post('/api/runs/continue', headers=headers,
                json={'request_key': 'complete-apply', 'plan': reference})
            assert replay.json() == applied.json()
            assert h.api.mutations == operations
    asyncio.run(scenario())


@pytest.mark.parametrize('attack', ['csrf', 'origin', 'owner', 'resource', 'query', 'task'])
def test_planning_api_rejects_forged_input_without_writes(monkeypatch, attack):
    async def scenario():
        h = Backend(monkeypatch)
        await h.seed()
        async with h.browser() as browser:
            headers = await h.login(browser)
            data, params = h.update(), {}
            if attack == 'csrf': headers.pop('X-CSRF-Token')
            if attack == 'origin': headers['Origin'] = 'https://foreign.test'
            if attack == 'owner': data['owner_id'] = 'foreign'
            if attack == 'resource': data['resource_id'] = 'sheet-foreign'
            if attack == 'query': params['user_id'] = '123'
            if attack == 'task': data['estimates'][0]['task_id'] = 'assign:123'
            response = await browser.post('/api/moodle/planning', headers=headers, json=data, params=params)
            assert response.status_code == (403 if attack in ('csrf', 'origin') else 409 if attack == 'task' else 400)
            assert (await browser.get('/api/moodle/planning')).json()['revision'] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('changed', ['hours', 'effort', 'deadline'])
def test_moodle_plan_is_persisted_and_stale_input_prevents_apply(monkeypatch, changed):
    async def scenario():
        h = Backend(monkeypatch)
        await h.seed()
        async with h.runtime(monkeypatch), h.browser() as browser:
            headers = await h.login(browser)
            saved = await browser.post('/api/moodle/planning', headers=headers, json=h.update())
            assert saved.status_code == 200
            started = await browser.post('/api/runs', headers=headers, json={'request_key': 'moodle-proposal'})
            assert started.status_code == 200, started.text
            run = started.json()
            assert run['status'] == 'proposed', run
            assert h.tool_calls == ['read_assignments', 'read_calendar']
            assert h.calendar_reads
            reference = run['plan']
            read = await browser.get('/api/plan', params={'plan_id': reference['plan_id'], 'version': reference['version']})
            assert read.status_code == 200, read.text
            plan = read.json()['plan']['payload']
            assert plan['source_references'][0]['resource_id'].startswith('moodle:')
            assert len(plan['operations']) == 2
            busy = interval(NOW + timedelta(hours=2))
            for block in plan['blocks']:
                span = Interval.model_validate(block['interval'])
                assert not (span.start < busy.end and span.end > busy.start)
            calls = len(h.model_calls)
            replay = await browser.post('/api/runs', headers=headers, json={'request_key': 'moodle-proposal'})
            assert replay.json() == run and len(h.model_calls) == calls
            approval = await browser.post('/api/approval', headers=headers, json={
                **reference, 'request_key': 'moodle-approval'})
            assert approval.status_code == 200, approval.text
            if changed in ('hours', 'effort'):
                data = {'expected_revision': 1}
                if changed == 'hours':
                    data['working_windows'] = [interval(NOW + timedelta(hours=4)).model_dump(mode='json')]
                else:
                    data['estimates'] = [{'task_id': 'assign:9', 'remaining_minutes': 30}]
                assert (await browser.post('/api/moodle/planning', headers=headers, json=data)).status_code == 200
            else:
                # Simulate the sync worker publishing a new deadline, without HTTP.
                from campuspulse_recovery.lms.sync import _Record
                from campuspulse_recovery.storage.plans import _decode, _encode
                path = h.inputs.connection.path
                record = _decode(h.documents.data[path], 'moodle_connection', _Record)
                h.documents.data[path] = _encode('moodle_connection', record.model_copy(update={
                    'snapshot': lms_snapshot(deadline=NOW + timedelta(hours=4))}))
            result = await browser.post('/api/runs/continue', headers=headers,
                json={'request_key': 'moodle-apply', 'plan': reference})
            assert result.status_code == 409, result.text
            assert 'apply_plan' not in h.tool_calls
            assert len(h.model_calls) == calls
    asyncio.run(scenario())


@pytest.mark.parametrize('missing,status', [('hours', 'needs_input'), ('estimate', 'needs_input'),
    ('deadline', 'needs_input'), ('capacity', 'infeasible')])
def test_unresolved_or_insufficient_input_cannot_publish_or_approve_a_plan(monkeypatch, missing, status):
    async def scenario():
        h = Backend(monkeypatch)
        if missing == 'deadline':
            h.snapshot = lms_snapshot(deadline=None)
        await h.seed()
        async with h.runtime(monkeypatch), h.browser() as browser:
            headers = await h.login(browser)
            data = h.update()
            if missing == 'hours': data['working_windows'] = None
            if missing == 'estimate': data['estimates'] = []
            if missing == 'capacity':
                data['working_windows'] = [Interval(start=NOW + timedelta(hours=1),
                    end=NOW + timedelta(hours=1, minutes=15)).model_dump(mode='json')]
            assert (await browser.post('/api/moodle/planning', headers=headers, json=data)).status_code == 200
            result = await browser.post('/api/runs', headers=headers, json={'request_key': 'incomplete'})
            assert result.status_code == 200, result.text
            assert result.json()['status'] == status, result.text
            assert result.json()['plan'] is None
            assert not any(path.startswith(('cp_plans/', 'cp_approvals/')) for path in h.documents.data)
            assert 'apply_plan' not in h.tool_calls
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['dispatch_cas', 'final_io'])
@pytest.mark.parametrize('changed', ['hours', 'effort', 'deadline', 'refresh'])
def test_guard_rechecks_input_and_cache_at_dispatch_and_final_io(monkeypatch, boundary, changed):
    async def scenario():
        from types import SimpleNamespace
        from campuspulse_recovery.cloud.writers import WRITER, WriterLifetime
        from campuspulse_recovery.domain.models import OperationState, PlanReference
        from campuspulse_recovery.domain.ports import BindingConflict
        from campuspulse_recovery.security.context import AuthenticatedCaller, RequestContextFactory, ServerRunBinding
        h = Backend(monkeypatch)
        await h.seed()
        async with h.runtime(monkeypatch), h.browser() as browser:
            headers = await h.login(browser)
            assert (await browser.post('/api/moodle/planning', headers=headers, json=h.update())).status_code == 200
            response = await browser.post('/api/runs', headers=headers, json={'request_key': 'guard-proposal'})
            assert response.json()['status'] == 'proposed', response.text
            reference = PlanReference.model_validate(response.json()['plan'])
            assert (await browser.post('/api/approval', headers=headers, json={
                **reference.model_dump(mode='json'), 'request_key': 'guard-approval'})).status_code == 200

            cfg = h.policy.config
            binding = ServerRunBinding(owner_id=cfg.student.trusted_user_id,
                signin_principal_id=cfg.student.allowed_signin_principal, session_id='guard-session',
                run_id='guard-run', config_revision=cfg.config_revision, expires_at=NOW + timedelta(hours=1))
            caller = AuthenticatedCaller(principal_id=cfg.identities.agent_service_account,
                session_id=binding.session_id, run_id=binding.run_id, authenticated_at=NOW,
                expires_at=binding.expires_at)
            contexts = RequestContextFactory(h.policy, boundary='mcp', clock=lambda: NOW,
                authenticator=SimpleNamespace(authenticate=lambda *args, **kwargs: caller),
                bindings=SimpleNamespace(resolve=lambda **kwargs: binding))
            service = h.mcp_services['apply']
            token = WRITER.set(WriterLifetime())
            try:
                with contexts.request(object()) as context:
                    plan = await service.plans.get(context, reference.plan_id, reference.version)
                    lease = await service.coordination.acquire(context, holder_id='guard-writer',
                        now=NOW, expires_at=NOW + timedelta(minutes=1))
                    state = await service.executions.bind_request(context, request_key='guard-request', plan=reference, at=NOW)
                    state = await service.executions.compare_and_set(context, expected_revision=0, lease=lease,
                        execution=state.model_copy(update={'status': 'approved', 'revision': 1}))
                    intent = OperationState(operation_id=state.operations[0].operation_id,
                        status='in_flight', attempts=1, updated_at=NOW, fencing_generation=lease.generation)
                    if boundary == 'final_io':
                        await service.executions.record_operation(context, request_key=state.request_key,
                            expected_revision=1, operation=intent, lease=lease)
                    # Initial planning/approval checks have already succeeded.
                    if changed in ('hours', 'effort'):
                        data = {'expected_revision': 1}
                        if changed == 'hours': data['working_windows'] = []
                        else: data['estimates'] = [{'task_id': 'assign:9', 'remaining_minutes': 30}]
                        response = await browser.post('/api/moodle/planning', headers=headers, json=data)
                        assert response.status_code == 200, response.text
                    else:
                        from campuspulse_recovery.lms.sync import _Record
                        from campuspulse_recovery.storage.plans import _decode, _encode
                        path = h.inputs.connection.path
                        record = _decode(h.documents.data[path], 'moodle_connection', _Record)
                        if changed == 'refresh':
                            h.now += timedelta(seconds=15)
                            snapshot = record.snapshot.model_copy(update={'fetched_at': h.now})
                        else:
                            snapshot = lms_snapshot(deadline=NOW + timedelta(hours=4))
                        h.documents.data[path] = _encode('moodle_connection', record.model_copy(update={'snapshot': snapshot}))
                    intent = intent.model_copy(update={'updated_at': h.now})
                    async def dispatch():
                        if boundary == 'dispatch_cas':
                            await service.executions.record_operation(context, request_key=state.request_key,
                                expected_revision=1, operation=intent, lease=lease)
                        else:
                            await service._send_guard(context, plan, lease, state.request_key)
                    if changed == 'refresh':
                        await dispatch()  # Same work after sync remains approved.
                        with monkeypatch.context() as guards:
                            async def outside_transaction(*args, **kwargs):
                                raise AssertionError('Planning guard must use caller transaction')
                            guards.setattr(h.documents, 'get', outside_transaction)
                            await service.approvals.require_active(context, reference, h.now)
                    else:
                        with pytest.raises(BindingConflict, match='moodle_planning_changed'):
                            await dispatch()
            finally:
                WRITER.reset(token)
    asyncio.run(scenario())
