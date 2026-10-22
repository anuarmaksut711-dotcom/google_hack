"""Real ADK loop with a scripted model and fake authenticated MCP I/O only."""
import asyncio
from contextlib import asynccontextmanager
import json
import pickle
import socket
import threading
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


GITHUB_URL = 'https://github.com/student/lab'


def github_source():
    return {'status': 'available', 'repository_url': GITHUB_URL, 'owner': 'student',
        'repository': 'lab', 'now': '2026-10-22T12:00:00+00:00', 'timezone': 'Asia/Qyzylorda',
        'readme': {'status': 'available', 'text': 'Implement a binary search tree.',
            'source_url': GITHUB_URL + '#readme', 'truncated': False},
        'issues': {'status': 'available', 'items': [{'number': 2, 'title': 'Add insertion tests',
            'body': 'Cover an empty tree and duplicate values.', 'url': GITHUB_URL + '/issues/2'}],
            'truncated': False}, 'oauth_token': 'secret-must-not-reach-model'}


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
        if isinstance(item, tuple):
            parts = [types.Part(function_call=types.FunctionCall(name=name, args={})) for name in item]
        elif isinstance(item, str):
            parts = [types.Part(function_call=types.FunctionCall(name=item, args={}))]
        elif 'tool' in item:
            parts = [types.Part(function_call=types.FunctionCall(name=item['tool'], args=item['args']))]
        else:
            parts = [types.Part(text='private reasoning', thought=True),
                     types.Part(text=json.dumps(item, ensure_ascii=False))]
        yield LlmResponse(content=types.Content(role='model', parts=parts))


def install_tools(monkeypatch, *, unavailable=False, revoked=False, fail_on_repeat=None,
                  schedule_unavailable=False, progress_events=None, progress_failure=None,
                  reverse_parallel=False, github_value=None, github_arguments=None):
    calls = []
    second_committed = asyncio.Event()
    class Grants:
        def __init__(self, documents):
            pass
        async def issue_tool(self, value, tool, *, with_call_id=False, repository_url=None):
            assert value['owner'] == invocation()['owner']
            if tool == 'read_github_assignment':
                assert repository_url == GITHUB_URL
                assert repository_url in value['message']
                if github_arguments is not None:
                    github_arguments.append(repository_url)
            else:
                assert repository_url is None
            if reverse_parallel and tool == 'read_assignments':
                await second_committed.wait()
            calls.append(tool)
            if reverse_parallel and tool == 'read_schedule':
                second_committed.set()
            reference = 'private-capability-' + tool
            return (reference, len(calls)) if with_call_id else reference
        async def session(self, documents, owner, session):
            assert (owner, session) == (invocation()['owner'], invocation()['session'])
            if revoked:
                raise AccessDenied('session_revoked')
        async def progress(self, value, *, phase=None, call=None):
            assert value['progress_id'] == 'd' * 64
            if progress_failure is not None:
                raise progress_failure
            progress_events.append({'phase': phase, 'call': call})
    async def call(settings, reference, tool):
        assert reference == 'private-capability-' + tool
        if tool == fail_on_repeat and calls.count(tool) > 1:
            raise ValueError('source_refresh_failed')
        value = (assignments() if tool == 'read_assignments' else
                 (github_source() if github_value is None else github_value)
                 if tool == 'read_github_assignment' else schedule())
        if unavailable and tool == 'read_assignments':
            value.update(status='unavailable', assignments=[], total=0)
        if schedule_unavailable and tool == 'read_schedule':
            value.update(status='unavailable', class_schedule={'status': 'unavailable'})
        return value
    monkeypatch.setattr(module, 'StudyGrants', Grants)
    monkeypatch.setattr(module, '_mcp_call', call)
    return calls


def run_model(monkeypatch, script, *, user_message=None, **options):
    calls = install_tools(monkeypatch, **options)
    model = ScriptedModel(model='gemini-synthetic')
    model._script = script
    app, study, chat = configuration()
    async def scenario():
        saved = invocation()
        if user_message is not None:
            saved['message'] = user_message
        if options.get('progress_events') is not None:
            saved['progress_id'] = 'd' * 64
        return await module._run_study(app, module.StudyAgentSettings.model_validate(study),
            module.ConversationTarget.model_validate(chat), object(), saved, model_factory=lambda: model)
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


def test_user_timetable_is_returned_as_review_draft_without_write_tools(monkeypatch):
    from tests.domain.test_chat_schedule import DRAFT
    draft_reply = {'kind': 'classes', 'message': '', 'blocks': [], 'timetable': DRAFT}
    result, calls, model = run_model(monkeypatch, [draft_reply])
    assert json.loads(result['reply']) == draft_reply
    assert calls == [] and result['trace'] == [] and result['class_schedule'] is None
    assert 'kind="classes"' in model._requests[0].config.system_instruction
    assert {'read_assignments', 'read_schedule'} <= model._requests[0].tools_dict.keys()
    assert not {'save_classes', 'apply_plan', 'verify_plan'} & model._requests[0].tools_dict.keys()


def test_live_progress_follows_actual_adk_model_and_tool_dispatch_without_reasoning_text(monkeypatch):
    events = []
    result, calls, _ = run_model(monkeypatch, ['read_assignments', 'read_schedule', reply(plan=True)],
                                progress_events=events)
    assert calls == ['read_assignments', 'read_schedule']
    assert [item['phase'] for item in events if item['phase']] == [
        'thinking', 'thinking', 'composing', 'composing', 'completed']
    observed = [item['call'] for item in events if item['call']]
    assert [(item['call_id'], item['tool'], item['status']) for item in observed] == [
        (1, 'read_assignments', 'running'), (1, 'read_assignments', 'completed'),
        (2, 'read_schedule', 'running'), (2, 'read_schedule', 'not_connected')]
    assert observed[1]['result_count'] == 1 and observed[3]['result_count'] == 0
    assert events[-1] == {'phase': 'completed', 'call': None}
    visible = json.dumps(events)
    assert 'private reasoning' not in visible and 'Real assignment' not in visible
    assert 'capability' not in visible and 'session-private' not in visible
    assert len(result['trace']) == 2


def test_greeting_progress_never_invents_tool_or_composing_stages(monkeypatch):
    events = []
    _, calls, _ = run_model(monkeypatch, [reply()], progress_events=events)
    assert calls == []
    assert [item['phase'] for item in events] == ['thinking', 'thinking', 'completed']
    assert all(item['call'] is None for item in events)


def test_progress_outage_never_prevents_actual_mcp_reads_or_valid_answer(monkeypatch):
    result, calls, _ = run_model(monkeypatch, ['read_assignments', 'read_schedule', reply(plan=True)],
        progress_events=[], progress_failure=RuntimeError('Firestore progress is temporarily unavailable'))
    assert calls == ['read_assignments', 'read_schedule']
    assert json.loads(result['reply']) == reply(plan=True)
    assert len(result['trace']) == 2


def test_progress_session_revocation_still_blocks_source_dispatch(monkeypatch):
    with pytest.raises(AccessDenied, match='session_revoked'):
        run_model(monkeypatch, ['read_assignments', reply()],
                  progress_events=[], progress_failure=AccessDenied('session_revoked'))


def test_parallel_tools_use_grant_commit_indices_when_commit_order_differs_from_model_order(monkeypatch):
    events = []
    result, calls, _ = run_model(monkeypatch,
        [('read_assignments', 'read_schedule'), reply(plan=True)],
        progress_events=events, reverse_parallel=True)
    assert calls == ['read_schedule', 'read_assignments']
    started = [item['call'] for item in events if item['call'] and item['call']['status'] == 'running']
    assert [(item['call_id'], item['tool']) for item in started] == [
        (1, 'read_schedule'), (2, 'read_assignments')]
    assert len(result['trace']) == 2


def test_model_decides_to_read_only_assignments_for_deadline_question(monkeypatch):
    result, calls, _ = run_model(monkeypatch, ['read_assignments', reply()])
    assert calls == ['read_assignments']
    assert result['class_schedule'] is None
    assert len(result['trace']) == 1


def test_greeting_requires_no_source_reads(monkeypatch):
    result, calls, _ = run_model(monkeypatch, [reply()])
    assert calls == [] and result['trace'] == []


def github_call():
    return {'tool': 'read_github_assignment', 'args': {'repository_url': GITHUB_URL}}


def github_reply():
    return {'kind': 'message', 'message': 'По [README](' + GITHUB_URL + '#readme):\n'
        '- [ ] Реализовать двоичное дерево поиска.\n'
        'Открытые задачи:\n- [ ] Добавить тесты вставки — [Issue #2](' + GITHUB_URL + '/issues/2).',
        'blocks': []}


def test_model_reads_real_github_mcp_tool_and_publishes_checklist_with_live_trace(monkeypatch):
    events, arguments = [], []
    result, calls, model = run_model(monkeypatch, [github_call(), github_reply()],
        user_message='Разбери требования и открытые задачи ' + GITHUB_URL,
        progress_events=events, github_arguments=arguments)
    assert calls == ['read_github_assignment'] and arguments == [GITHUB_URL]
    assert json.loads(result['reply']) == github_reply()
    assert result['class_schedule'] is None
    assert [(row['tool'], row['status'], row['result_count']) for row in result['trace']] == [
        ('read_github_assignment', 'completed', 2)]
    observed = [event['call'] for event in events if event['call']]
    assert [(item['tool'], item['status']) for item in observed] == [
        ('read_github_assignment', 'running'), ('read_github_assignment', 'completed')]
    visible = json.dumps([content.model_dump(mode='json')
        for content in model._requests[-1].contents], ensure_ascii=False)
    assert 'binary search tree' in visible and 'Add insertion tests' in visible
    assert 'secret-must-not-reach-model' not in visible and 'private-capability-' not in visible
    assert 'read_github_assignment' in model._requests[0].tools_dict
    assert 'Markdown-чеклист' in model._requests[0].config.system_instruction
    assert 'не выполняй' in model._requests[0].config.system_instruction


def test_explicit_github_repository_cannot_be_answered_without_actual_tool_read(monkeypatch):
    with pytest.raises(ValueError, match='study_github_source_missing'):
        run_model(monkeypatch, [github_reply()], user_message='Разбери ' + GITHUB_URL)


def test_moodle_read_cannot_substitute_for_explicit_github_repository(monkeypatch):
    with pytest.raises(ValueError, match='study_github_source_missing'):
        run_model(monkeypatch, ['read_assignments', github_reply()],
            user_message='Разбери ' + GITHUB_URL)


def test_partial_github_read_is_marked_partial_in_final_trace(monkeypatch):
    value = github_source()
    value.update(status='partial')
    value['readme'].update(status='rate_limited', text='')
    result, calls, _ = run_model(monkeypatch, [github_call(), reply()],
        user_message='Разбери ' + GITHUB_URL, github_value=value)
    assert calls == ['read_github_assignment']
    assert result['trace'][0]['status'] == 'partial' and result['trace'][0]['result_count'] == 1


def test_unavailable_github_is_not_reported_as_empty_success(monkeypatch):
    value = github_source()
    value.update(status='unavailable')
    value['readme'].update(status='unavailable', text='')
    value['issues'].update(status='rate_limited', items=[])
    result, _, model = run_model(monkeypatch, [github_call(), reply()],
        user_message='Разбери ' + GITHUB_URL, github_value=value)
    assert result['trace'][0]['status'] == 'unavailable' and result['trace'][0]['result_count'] == 0
    assert 'rate_limited' in json.dumps([content.model_dump(mode='json')
        for content in model._requests[-1].contents])


def test_github_checklist_cannot_become_calendar_blocks(monkeypatch):
    with pytest.raises(ValueError, match='study_github_checklist_requires_message'):
        run_model(monkeypatch, ['read_assignments', 'read_schedule', github_call(), reply(plan=True)],
            user_message='Разбери ' + GITHUB_URL)


def test_github_checklist_cannot_become_imported_classes(monkeypatch):
    from tests.domain.test_chat_schedule import DRAFT
    timetable = {'kind': 'classes', 'message': '', 'blocks': [], 'timetable': DRAFT}
    with pytest.raises(ValueError, match='study_github_checklist_requires_message'):
        run_model(monkeypatch, [github_call(), timetable], user_message='Разбери ' + GITHUB_URL)


def test_github_source_must_match_repository_bound_in_tool_call(monkeypatch):
    value = github_source()
    value['repository_url'] = 'https://github.com/foreign/lab'
    value['owner'] = 'foreign'
    value['readme']['source_url'] = value['repository_url'] + '#readme'
    value['issues']['items'] = []
    result, _, model = run_model(monkeypatch, [github_call(), reply()],
        user_message='Разбери ' + GITHUB_URL, github_value=value)
    assert result['trace'][0]['status'] == 'failed' and result['trace'][0]['result_count'] == 0
    visible = json.dumps([content.model_dump(mode='json')
        for content in model._requests[-1].contents])
    assert 'source_read_failed' in visible and 'https://github.com/foreign/lab' not in visible


def test_github_source_allowlist_drops_private_fields_at_each_level():
    value = github_source()
    value['readme']['oauth_token'] = 'private-readme-token'
    value['issues']['owner_id'] = 'private-owner'
    value['issues']['items'][0]['credential'] = 'private-issue-token'
    source = module._safe_source('read_github_assignment', value, 'Asia/Qyzylorda')
    visible = json.dumps(source)
    assert source['source_kind'] == 'github_repository'
    assert not any(private in visible for private in [
        'secret-must-not-reach-model', 'private-readme-token', 'private-owner', 'private-issue-token'])


@pytest.mark.parametrize('field, url', [('readme', 'https://evil.example/readme'),
    ('issue', 'https://github.com/foreign/lab/issues/2')])
def test_github_source_rejects_foreign_citation_urls(field, url):
    value = github_source()
    if field == 'readme':
        value['readme']['source_url'] = url
    else:
        value['issues']['items'][0]['url'] = url
    with pytest.raises(ValueError):
        module._safe_source('read_github_assignment', value, 'Asia/Qyzylorda')


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
    monkeypatch.setattr(module, '_preload_runtime', forbidden)
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


def test_startup_preloads_definitions_once_without_clients_network_or_sources(monkeypatch):
    calls, preload = [], module._preload_runtime
    def observed():
        calls.append('preload')
        preload()
    def forbidden(*args, **kwargs):
        raise AssertionError('Startup must not create clients, models, sessions or source grants')
    monkeypatch.setattr(module, '_preload_runtime', observed)
    monkeypatch.setattr(module, 'AdkConversationAgent', forbidden)
    monkeypatch.setattr(module, 'StudyGrants', forbidden)
    monkeypatch.setattr('google.cloud.firestore.AsyncClient', forbidden)
    monkeypatch.setattr('google.genai.Client', forbidden)
    monkeypatch.setattr('mcp.ClientSession', forbidden)
    monkeypatch.setattr('httpx.AsyncClient', forbidden)
    runtime = module.StudyAgentRuntime(*configuration())
    runtime.set_up()
    runtime.set_up()
    assert calls == ['preload'] and runtime._setup_ready
    restored = pickle.loads(pickle.dumps(runtime))
    assert not restored._setup_ready
    assert restored._setup_lock is not runtime._setup_lock
    assert restored.register_operations() == {'async': ['async_query']}


def test_concurrent_direct_queries_setup_once_off_loop_before_identity(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, identities = [], []
    def preload():
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
    class Identity:
        def __init__(self, *args): pass
        async def verify(self, request):
            identities.append('verified-after-setup')
            raise AccessDenied('foreign-service')
    monkeypatch.setattr(module, '_preload_runtime', preload)
    monkeypatch.setattr(module, 'ServiceIdentity', Identity)
    async def scenario():
        runtime = module.StudyAgentRuntime(*configuration())
        first = asyncio.create_task(runtime.async_query('a' * 43, 'synthetic-token'))
        second = asyncio.create_task(runtime.async_query('b' * 43, 'synthetic-token'))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert identities == []
            # A blocking importer must not prevent a separate route/task from
            # running in the same event loop while startup is still pending.
            assert await asyncio.wait_for(asyncio.sleep(0, result='responsive'), .25) == 'responsive'
        finally:
            release.set()
        assert await first == module._failure(403)
        assert await second == module._failure(403)
        assert len(calls) == 1 and calls[0] != threading.get_ident()
        assert identities == ['verified-after-setup', 'verified-after-setup']
    asyncio.run(scenario())


def test_cancellation_during_fallback_setup_never_reaches_identity_or_sources(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def preload():
        entered.set()
        assert release.wait(3)
    def forbidden(*args, **kwargs):
        raise AssertionError('Cancelled query reached an external boundary')
    monkeypatch.setattr(module, '_preload_runtime', preload)
    monkeypatch.setattr(module, 'ServiceIdentity', forbidden)
    monkeypatch.setattr(module, '_documents', forbidden)
    async def scenario():
        runtime = module.StudyAgentRuntime(*configuration())
        task = asyncio.create_task(runtime.async_query('a' * 43, 'synthetic-token'))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
        finally:
            release.set()
    asyncio.run(scenario())


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
