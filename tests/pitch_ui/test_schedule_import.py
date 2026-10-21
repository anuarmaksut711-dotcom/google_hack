"""Bounded timetable extraction: real auth, contracts and synthetic model IO only."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from campuspulse_recovery.agent.conversation import AdkConversationAgent, ConversationTarget
from campuspulse_recovery.lms.manual_schedule import ManualSchedule
from campuspulse_recovery.web.manual_schedule import install_class_routes
from tests.storage.test_plans import run
from tests.web.test_approval import Setup, login
from tests.web.test_chat import app as chat_app


PDF = b'%PDF-1.7\nsynthetic timetable, no provider calls'
DRAFT = {'valid_from': None, 'valid_until': None, 'classes': [
    {'subject': 'Алгоритмы', 'weekday': 1, 'start': '12:00', 'end': '14:00'}]}


class Importer:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = None
        self.calls = []
        self.value = deepcopy(DRAFT)
        self.error = False

    async def extract_classes(self, **values):
        self.calls.append(values)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error:
            raise RuntimeError('private credential and document details')
        return self.value


def application(h, importer):
    app = chat_app(h)
    install_class_routes(app, auth=app.state.web_auth,
        account=ManualSchedule(h.documents, h.policy, clock=h.clock), importer=importer)
    return app


def test_route_is_additive_and_requires_explicit_save():
    async def scenario():
        h, importer = Setup(), Importer()
        async with h.client(application(h, importer)) as client:
            path = '/api/account/classes/import'
            assert (await client.post(path, content=PDF)).status_code == 403
            headers = {**await login(client), 'Content-Type': 'application/pdf'}
            assert (await client.post(path, content=PDF, headers={'Content-Type':'application/pdf'})).status_code == 403
            assert (await client.post(path+'?owner=bob', content=PDF, headers=headers)).status_code == 400
            response = await client.post(path, content=PDF, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()['draft'] == DRAFT
            assert response.json()['warning']
            assert importer.calls == [{'document': PDF, 'mime_type': 'application/pdf'}]
            assert (await client.get('/api/account/classes')).json()['connected'] is False
            assert not any(key.startswith('cp_class_schedules/') for key in h.documents.data)
            draft = {**response.json()['draft'], 'valid_from':'2026-10-19', 'valid_until':'2026-11-01'}
            assert (await client.post('/api/account/classes', json=draft, headers={
                **headers, 'Content-Type':'application/json'})).status_code == 200
            assert (await client.get('/api/account/classes')).json()['connected'] is True
    run(scenario())


@pytest.mark.parametrize('mime,document,status', [
    ('text/plain', PDF, 415), ('application/pdf; charset=utf-8', PDF, 415),
    ('application/pdf', b'', 400), ('application/pdf', b'not PDF', 400),
    ('image/png', PDF, 400), ('image/jpeg', b'\xff\xd8\xfffixture', 200),
    ('image/png', b'\x89PNG\r\n\x1a\nfixture', 200),
    ('application/pdf', PDF + b'x' * (8*1024*1024), 413),
], ids=['unsupported', 'parameters', 'empty', 'bad-pdf', 'mismatch', 'jpeg', 'png', 'too-large'])
def test_file_type_magic_and_stream_limit(mime, document, status):
    async def scenario():
        h, importer = Setup(), Importer()
        async with h.client(application(h, importer)) as client:
            headers = {**await login(client), 'Content-Type': mime}
            async def chunks():
                for offset in range(0, len(document), 65536):
                    yield document[offset:offset+65536]
            response = await client.post('/api/account/classes/import', content=chunks(), headers=headers)
            assert response.status_code == status, response.text
            assert bool(importer.calls) == (status == 200)
    run(scenario())


@pytest.mark.parametrize('value', [
    {**DRAFT, 'owner_id': 'other'}, {**DRAFT, 'classes': []},
    {**DRAFT, 'classes': DRAFT['classes']*31},
    {**DRAFT, 'classes': DRAFT['classes']*2},
    {**DRAFT, 'classes': [{**DRAFT['classes'][0], 'weekday':7}]},
    {**DRAFT, 'classes': [{**DRAFT['classes'][0], 'end':'11:00'}]},
    {**DRAFT, 'valid_from':'2026-10-20', 'valid_until':'2026-10-19'},
    {**DRAFT, 'valid_from':'2026-10-20', 'valid_until':'2027-10-20'},
])
def test_invalid_provider_drafts_fail_closed(value):
    async def scenario():
        h, importer = Setup(), Importer()
        importer.value = value
        async with h.client(application(h, importer)) as client:
            headers = {**await login(client), 'Content-Type':'application/pdf'}
            result = await client.post('/api/account/classes/import', content=PDF, headers=headers)
            assert result.status_code == 422
            assert result.json() == {'error':'schedule_import_unreadable'}
            assert (await client.get('/api/account/classes')).json()['connected'] is False
    run(scenario())


def test_busy_is_account_bound_and_revoked_session_is_rechecked_after_extract():
    async def scenario():
        h, importer = Setup(), Importer()
        importer.release = asyncio.Event()
        app = application(h, importer)
        async with h.client(app) as alice, h.client(app) as second:
            headers = {**await login(alice), 'Content-Type':'application/pdf'}
            other = {**await login(second), 'Content-Type':'application/pdf'}
            first = asyncio.create_task(alice.post('/api/account/classes/import', content=PDF, headers=headers))
            await importer.entered.wait()
            concurrent = await second.post('/api/account/classes/import', content=PDF, headers=other)
            assert concurrent.status_code == 429
            assert (await alice.post('/auth/signout', json={}, headers=headers)).status_code == 200
            importer.release.set()
            assert (await first).status_code == 403
            assert (await second.post('/api/account/classes/import', content=PDF, headers=other)).status_code == 200
    run(scenario())


def test_safe_failures_and_unconfigured_import_keep_manual_route_available():
    async def scenario():
        for importer in (None, Importer()):
            h = Setup()
            if importer is not None:
                importer.error = True
            async with h.client(application(h, importer)) as client:
                headers = {**await login(client), 'Content-Type':'application/pdf'}
                failed = await client.post('/api/account/classes/import', content=PDF, headers=headers)
                assert failed.status_code == 503
                assert 'private' not in failed.text and 'credential' not in failed.text
                assert (await client.get('/api/account/classes')).status_code == 200
    run(scenario())


class SyntheticModel(BaseLlm):
    _requests: list = PrivateAttr(default_factory=list)
    _parts: list = PrivateAttr(default_factory=list)
    _reason: object = PrivateAttr(default=None)

    async def generate_content_async(self, request, stream=False):
        self._requests.append(request)
        yield LlmResponse(content=types.Content(role='model', parts=self._parts), finish_reason=self._reason)


def test_configured_multimodal_model_receives_only_untrusted_file_and_returns_validated_draft():
    async def scenario():
        model = SyntheticModel(model='gemini-synthetic')
        model._parts = [types.Part(text='private reasoning', thought=True), types.Part(text=json.dumps(DRAFT))]
        closed = []
        async def close_async():
            closed.append('async')
        model.__dict__['api_client'] = SimpleNamespace(aio=SimpleNamespace(aclose=close_async), close=lambda:closed.append('sync'))
        agent = AdkConversationAgent(ConversationTarget(project_id='campus-test-42', location='us-central1',
            model='gemini-synthetic'), model_factory=lambda:model)
        result = await agent.extract_classes(document=PDF, mime_type='application/pdf')
        assert result.model_dump(mode='json', exclude={'schema_version'}, exclude_none=False)['classes'][0]['subject'] == 'Алгоритмы'
        request, = model._requests
        assert request.model == 'gemini-synthetic' and not request.tools_dict and not request.config.tools
        assert len(request.contents) == 1 and len(request.contents[0].parts) == 1
        assert request.contents[0].parts[0].inline_data.data == PDF
        assert request.contents[0].parts[0].inline_data.mime_type == 'application/pdf'
        assert request.config.response_mime_type == 'application/json'
        assert request.config.response_schema and request.config.max_output_tokens <= 4096
        assert 'untrusted' in request.config.system_instruction and 'Never invent' in request.config.system_instruction
        assert closed == ['async', 'sync']
    run(scenario())


@pytest.mark.parametrize('parts,reason', [
    ([types.Part(text=json.dumps(DRAFT))], types.FinishReason.MAX_TOKENS),
    ([types.Part(text=json.dumps({**DRAFT, 'classes':[]}))], None),
    ([types.Part(text='secret thought', thought=True)], None),
    ([types.Part(text='x'*25000)], None),
    ([types.Part(function_call=types.FunctionCall(name='save_classes', args={}))], None),
])
def test_model_rejects_incomplete_empty_unbounded_or_tool_output(parts, reason):
    async def scenario():
        model = SyntheticModel(model='gemini-synthetic')
        model._parts, model._reason = parts, reason
        agent = AdkConversationAgent(ConversationTarget(project_id='campus-test-42', location='us-central1',
            model='gemini-synthetic'), model_factory=lambda:model)
        with pytest.raises(ValueError):
            await agent.extract_classes(document=PDF, mime_type='application/pdf')
    run(scenario())
