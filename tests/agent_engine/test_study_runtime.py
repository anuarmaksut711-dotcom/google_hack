"""Real ADK loop with a scripted model and fake authenticated MCP I/O only."""
import asyncio
from contextlib import asynccontextmanager
import json
import pickle
import socket
from types import SimpleNamespace

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr
import pytest

from campuspulse_recovery.agent_engine import study_runtime as module
from campuspulse_recovery.security.context import AccessDenied
from tests.lms.test_settings import valid_payload


def configuration():
    value = valid_payload()
    app = value['application']
    study = {'target': {'project_id': app['project_id'], 'location': app['agent_region'],
        'resource_name': 'projects/12345/locations/europe-west1/reasoningEngines/87654'},
        'agent_audience': value['agent_origin'], 'mcp_origin': value['mcp_origin'],
        'backend_service_account': app['identities']['backend_service_account'],
        'agent_service_account': app['identities']['agent_service_account']}
    chat = {'provider': 'vertex', 'project_id': app['project_id'],
            'location': 'global', 'model': 'gemini-synthetic'}
    return app, study, chat


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Study runtime unit tests forbid network and ADC')
    original_connect = socket.socket.connect
    def local_event_loop_only(sock, address):
        # Windows asyncio creates its self-pipe using a loopback socket pair.
        if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1'}:
            return original_connect(sock, address)
        return forbidden()
    monkeypatch.setattr(socket.socket, 'connect', local_event_loop_only)
    monkeypatch.setattr('google.auth.default', forbidden)


def invocation():
    return {'invocation_id': 'c' * 64, 'owner': 'owner-private-identifier',
        'session': 'session-private-identifier', 'message': 'Задания и план на завтра с 14 до 16',
        'history': [], 'timezone': 'Asia/Qyzylorda', 'expires': '2026-10-23T00:00:00+00:00'}


def assignments():
    return {'status': 'available', 'source_kind': 'moodle_calendar_feed',
        'fetched_at': '2026-10-22T12:00:00+00:00', 'now': '2026-10-22T12:00:00+00:00',
        'timezone': 'Asia/Qyzylorda', 'assignments': [{'id': 'internal-task-id',
            'title': 'Real assignment from fake MCP', 'course_title': 'Algorithms',
            'due_at': '2026-10-25T23:59:00+05:00', 'submission_status': 'unknown'}],
        'total': 1, 'truncated': False, 'period': 'Moodle-selected range',
        'oauth_token': 'secret-must-not-reach-model', 'url': 'https://private-feed/?token=secret'}


def schedule():
    return {'status': 'not_connected', 'class_schedule': {'status': 'not_connected'},
        'context': ['Расписание не заполнено.'], 'now': '2026-10-22T12:00:00+00:00',
        'timezone': 'Asia/Qyzylorda'}


def reply(plan=False):
    return {'kind': 'plan' if plan else 'message', 'message': '' if plan else 'Сначала Algorithms.',
        'blocks': [{'title': 'Algorithms, примерный блок', 'start': '2026-10-23T14:00:00+05:00',
                    'end': '2026-10-23T15:00:00+05:00'}] if plan else []}


class ScriptedModel(BaseLlm):
    _script: list = PrivateAttr(default_factory=list)
    _requests: list = PrivateAttr(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        self._requests.append(llm_request.model_copy(deep=True))
        item = self._script.pop(0)
        if isinstance(item, str):
            parts = [types.Part(function_call=types.FunctionCall(name=item, args={}))]
        else:
            parts = [types.Part(text='private reasoning', thought=True),
                     types.Part(text=json.dumps(item, ensure_ascii=False))]
        yield LlmResponse(content=types.Content(role='model', parts=parts))


def install_tools(monkeypatch, *, unavailable=False, revoked=False, fail_on_repeat=None,
                  schedule_unavailable=False):
    calls = []
    class Grants:
        def __init__(self, documents):
            pass
        async def issue_tool(self, value, tool):
            assert value['owner'] == invocation()['owner']
            calls.append(tool)
            return 'private-capability-' + tool
        async def session(self, documents, owner, session):
            assert (owner, session) == (invocation()['owner'], invocation()['session'])
            if revoked:
                raise AccessDenied('session_revoked')
    async def call(settings, reference, tool):
        assert reference == 'private-capability-' + tool
        if tool == fail_on_repeat and calls.count(tool) > 1:
            raise ValueError('source_refresh_failed')
        value = assignments() if tool == 'read_assignments' else schedule()
        if unavailable and tool == 'read_assignments':
            value.update(status='unavailable', assignments=[], total=0)
        if schedule_unavailable and tool == 'read_schedule':
            value.update(status='unavailable', class_schedule={'status': 'unavailable'})
        return value
    monkeypatch.setattr(module, 'StudyGrants', Grants)
    monkeypatch.setattr(module, '_mcp_call', call)
    return calls


def run_model(monkeypatch, script, **options):
    calls = install_tools(monkeypatch, **options)
    model = ScriptedModel(model='gemini-synthetic')
    model._script = script
    app, study, chat = configuration()
    async def scenario():
        return await module._run_study(app, module.StudyAgentSettings.model_validate(study),
            module.ConversationTarget.model_validate(chat), object(), invocation(), model_factory=lambda: model)
    return asyncio.run(scenario()), calls, model


def test_model_selects_real_tools_then_returns_plan_with_separate_source_and_trace(monkeypatch):
    result, calls, model = run_model(monkeypatch, ['read_assignments', 'read_schedule', reply(plan=True)])
    assert calls == ['read_assignments', 'read_schedule']
    assert json.loads(result['reply']) == reply(plan=True)
    assert [(row['tool'], row['status'], row['result_count']) for row in result['trace']] == [
        ('read_assignments', 'completed', 1), ('read_schedule', 'not_connected', 0)]
    assert result['class_schedule']['status'] == 'not_connected'
    assert result['run_id'].startswith('study_')
    assert len(model._requests) == 3
    for request in model._requests:
        assert {'read_assignments', 'read_schedule'} <= request.tools_dict.keys()
        assert 'apply_plan' not in request.tools_dict and 'verify_plan' not in request.tools_dict
        # Inspect only content sent to Gemini, not private runner session state.
        visible = json.dumps([content.model_dump(mode='json') for content in request.contents]) + str(request.config.system_instruction)
        assert 'private-capability-' not in visible
        assert 'owner-private-identifier' not in visible and 'session-private-identifier' not in visible
        assert 'secret-must-not-reach-model' not in visible and 'https://private-feed' not in visible
    assert 'Real assignment from fake MCP' in json.dumps([
        content.model_dump(mode='json') for content in model._requests[-1].contents])
    assert 'private reasoning' not in result['reply']


def test_model_decides_to_read_only_assignments_for_deadline_question(monkeypatch):
    result, calls, _ = run_model(monkeypatch, ['read_assignments', reply()])
    assert calls == ['read_assignments']
    assert result['class_schedule'] is None
    assert len(result['trace']) == 1


def test_greeting_requires_no_source_reads(monkeypatch):
    result, calls, _ = run_model(monkeypatch, [reply()])
    assert calls == [] and result['trace'] == []


def test_assignment_without_moodle_category_stays_readable():
    source = assignments()
    source['assignments'][0]['course_title'] = ''
    result = module._safe_source('read_assignments', source, 'Asia/Qyzylorda')
    assert result['assignments'][0]['course_title'] == ''
    assert result['assignments'][0]['title'] == source['assignments'][0]['title']


def test_mcp_accepts_bounded_utf8_russian_source_without_ascii_expansion(monkeypatch):
    source = assignments()
    source['assignments'] = [{**source['assignments'][0], 'title': 'Я' * 360,
                              'course_title': 'Я' * 160} for _ in range(99)]
    source['total'] = 99
    assert len(json.dumps(source, ensure_ascii=False).encode('utf-8')) < 180000
    assert len(json.dumps(source, ensure_ascii=True).encode('utf-8')) > 180000
    async def token(audience, seconds):
        return SimpleNamespace(value='synthetic-platform-id-token')
    @asynccontextmanager
    async def transport(url, http_client):
        assert url.endswith('/study/mcp')
        yield object(), object(), None
    class Session:
        def __init__(self, read, write):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def initialize(self):
            pass
        async def call_tool(self, tool, arguments):
            assert tool == 'read_assignments' and arguments == {'reference': 'private-reference'}
            return SimpleNamespace(isError=False, structuredContent=source, content=[])
    monkeypatch.setattr(module, 'PlatformIdTokenIssuer', lambda: token)
    monkeypatch.setattr('mcp.ClientSession', Session)
    monkeypatch.setattr('mcp.client.streamable_http.streamable_http_client', transport)
    settings = module.StudyAgentSettings.model_validate(configuration()[1])
    result = asyncio.run(module._mcp_call(settings, 'private-reference', 'read_assignments'))
    assert len(result['assignments']) == 99
    assert result['assignments'][0]['title'] == 'Я' * 360


def test_plan_without_real_tools_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match='study_plan_sources_missing'):
        run_model(monkeypatch, [reply(plan=True)])


def test_failed_assignment_read_cannot_be_published_as_real_plan(monkeypatch):
    with pytest.raises(ValueError, match='study_assignments_unavailable'):
        run_model(monkeypatch, ['read_assignments', 'read_schedule', reply(plan=True)], unavailable=True)


@pytest.mark.parametrize('tool', ['read_assignments', 'read_schedule'])
def test_failed_latest_read_invalidates_earlier_success_before_plan(monkeypatch, tool):
    with pytest.raises(ValueError, match='study_plan_sources_missing'):
        run_model(monkeypatch, ['read_assignments', 'read_schedule', tool, reply(plan=True)],
                  fail_on_repeat=tool)


def test_unavailable_class_schedule_cannot_be_published_as_plan(monkeypatch):
    with pytest.raises(ValueError, match='study_schedule_unavailable'):
        run_model(monkeypatch, ['read_assignments', 'read_schedule', reply(plan=True)],
                  schedule_unavailable=True)


def test_revoked_session_prevents_publication_after_model_finishes(monkeypatch):
    with pytest.raises(AccessDenied, match='session_revoked'):
        run_model(monkeypatch, ['read_assignments', reply()], revoked=True)


@pytest.mark.parametrize('reference, token', [('short', 'token'), ('a' * 43, ''),
    ('a' * 43, 'bad\r\nheader'), ('a' * 43, True), ('a' * 43, 't' * 11994)],
    ids=['short-reference', 'empty-token', 'header-injection', 'boolean-token', 'oversized-token'])
def test_invalid_credentials_rejected_before_identity_or_firestore(monkeypatch, reference, token):
    def forbidden(*args, **kwargs):
        raise AssertionError('invalid credential reached external boundary')
    monkeypatch.setattr(module, 'ServiceIdentity', forbidden)
    monkeypatch.setattr(module, '_documents', forbidden)
    result = asyncio.run(module.StudyAgentRuntime(*configuration()).async_query(reference, token))
    assert result == module._failure(403)


def test_backend_identity_verified_before_firestore_grant_is_consumed(monkeypatch):
    seen = []
    class Identity:
        def __init__(self, audience, service_account):
            assert audience == configuration()[1]['agent_audience']
            assert service_account == configuration()[1]['backend_service_account']
        async def verify(self, request):
            assert request.headers['authorization'] == 'Bearer synthetic-token'
            seen.append('identity')
            raise AccessDenied('foreign_service')
    def forbidden(*args, **kwargs):
        raise AssertionError('unauthorized caller reached documents')
    monkeypatch.setattr(module, 'ServiceIdentity', Identity)
    monkeypatch.setattr(module, '_documents', forbidden)
    result = asyncio.run(module.StudyAgentRuntime(*configuration()).async_query('a' * 43, 'synthetic-token'))
    assert result == module._failure(403) and seen == ['identity']


def test_authenticated_invocation_is_consumed_once_and_result_uses_private_envelope(monkeypatch):
    seen = []
    class Identity:
        def __init__(self, *args):
            pass
        async def verify(self, request):
            seen.append('identity')
    @asynccontextmanager
    async def documents(application):
        seen.append('documents-open')
        try:
            yield 'fake-documents'
        finally:
            seen.append('documents-close')
    class Grants:
        def __init__(self, documents):
            assert documents == 'fake-documents'
        async def consume_invocation(self, reference):
            assert reference == 'a' * 43
            seen.append('claim')
            return invocation()
    async def execute(app, settings, target, documents, saved):
        assert saved == invocation()
        seen.append('run')
        return {'reply': json.dumps(reply()), 'trace': [], 'run_id': 'study_synthetic', 'class_schedule': None}
    monkeypatch.setattr(module, 'ServiceIdentity', Identity)
    monkeypatch.setattr(module, '_documents', documents)
    monkeypatch.setattr(module, 'StudyGrants', Grants)
    monkeypatch.setattr(module, '_run_study', execute)
    result = asyncio.run(module.StudyAgentRuntime(*configuration()).async_query('a' * 43, 'synthetic-token'))
    assert result['status_code'] == 200
    assert seen == ['identity', 'documents-open', 'claim', 'run', 'documents-close']


def test_serialization_rebuilds_only_validated_configuration():
    runtime = module.StudyAgentRuntime(*configuration())
    runtime.private_runtime_token = 'must-not-be-serialized'
    restored = pickle.loads(pickle.dumps(runtime))
    assert not hasattr(restored, 'private_runtime_token')
    assert restored.register_operations() == {'async': ['async_query']}


def test_foreign_model_project_rejected():
    app, study, chat = configuration()
    chat['project_id'] = 'foreign-project'
    with pytest.raises(ValueError, match='study_deployment_mismatch'):
        module.StudyAgentRuntime(app, study, chat)


def test_explicit_study_identity_can_differ_from_existing_recovery_identity():
    app, study, chat = configuration()
    study['agent_service_account'] = 'deadline-agent@' + app['project_id'] + '.iam.gserviceaccount.com'
    runtime = module.StudyAgentRuntime(app, study, chat)
    assert runtime.register_operations() == {'async': ['async_query']}


def test_foreign_study_service_identity_rejected():
    app, study, chat = configuration()
    study['agent_service_account'] = 'deadline-agent@foreign-project.iam.gserviceaccount.com'
    with pytest.raises(ValueError, match='study_deployment_mismatch'):
        module.StudyAgentRuntime(app, study, chat)
