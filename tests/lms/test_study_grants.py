"""Real durable transactions for read-only, per-tool study capabilities."""
import asyncio
from datetime import timedelta

import pytest

from campuspulse_recovery.cloud.study_grants import StudyGrants
from campuspulse_recovery.lms.multiuser import create_multiuser_app
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.storage.plans import _decode, _encode
from campuspulse_recovery.web.auth import WebSession
from tests.lms.test_local_google_calendar import NOW
from tests.lms.test_multiuser import ORIGIN, Verifier, client, login, run_async


async def setup(tmp_path):
    app = create_multiuser_app(origin=ORIGIN, client_id='client', directory=tmp_path,
        test_verifier=Verifier(), clock=lambda: NOW)
    async with client(app) as browser:
        await login(browser, 'alice')
    docs = app.state.documents
    index = (await docs.scan('cp_web_session_index'))[0]
    session = _decode(await docs.get(index['path']), 'web_session', WebSession)
    return docs, index, session


async def invocation(grants, session, **changes):
    args = dict(owner=session.binding.owner_id, session=session.binding.session_id,
        message='Составь план', history=[], timezone='Asia/Almaty')
    args.update(changes)
    return await grants.issue_invocation(**args)


@run_async
async def test_parent_replay_owner_scope_expiry_and_redacted_history(tmp_path):
    docs, _, session = await setup(tmp_path)
    now = NOW
    grants = StudyGrants(docs, clock=lambda: now)
    with pytest.raises(AccessDenied):
        await invocation(grants, session, owner='other-owner')
    ref = await invocation(grants, session, history=[
        {'status': 'completed', 'message': 'Предыдущее', 'response': 'Ответ', 'request_key': 'private-key'},
        {'status': 'failed', 'message': 'Не повторять', 'response': None}])
    values = await asyncio.gather(grants.consume_invocation(ref), grants.consume_invocation(ref),
                                  return_exceptions=True)
    successful = [value for value in values if isinstance(value, dict)]
    assert len(successful) == 1
    assert successful[0]['history'] == [{'message': 'Предыдущее', 'response': 'Ответ'}]
    assert 'request_key' not in str(successful)
    ref = await invocation(grants, session)
    now += timedelta(seconds=151)
    with pytest.raises(AccessDenied):
        await grants.consume_invocation(ref)


@run_async
async def test_child_exact_tool_binding_atomic_replay_and_tampered_parent(tmp_path):
    docs, _, session = await setup(tmp_path)
    grants = StudyGrants(docs, clock=lambda: NOW)
    ref = await invocation(grants, session)
    parent = await grants.consume_invocation(ref)
    with pytest.raises(AccessDenied):
        await grants.issue_tool({**parent, 'owner': 'foreign'}, 'read_assignments')
    with pytest.raises(AccessDenied):
        await grants.issue_tool(parent, 'apply_plan')
    child = await grants.issue_tool(parent, 'read_assignments')
    with pytest.raises(AccessDenied):
        await grants.consume_tool(child, 'read_schedule')
    values = await asyncio.gather(grants.consume_tool(child, 'read_assignments'),
        grants.consume_tool(child, 'read_assignments'), return_exceptions=True)
    assert sum(isinstance(value, dict) for value in values) == 1
    public = next(value for value in values if isinstance(value, dict))
    assert public['owner'] == session.binding.owner_id
    assert set(public) == {'invocation_id', 'owner', 'session', 'timezone', 'expires', 'tool'}


@run_async
async def test_expired_or_revoked_session_cannot_authorize_a_child(tmp_path):
    docs, index, session = await setup(tmp_path)
    now = NOW
    grants = StudyGrants(docs, clock=lambda: now)
    parent = await grants.consume_invocation(await invocation(grants, session))
    child = await grants.issue_tool(parent, 'read_schedule')
    now += timedelta(seconds=61)
    with pytest.raises(AccessDenied):
        await grants.consume_tool(child, 'read_schedule')
    now = NOW
    child = await grants.issue_tool(parent, 'read_schedule')
    async def revoke(tx):
        await tx.get(index['path'])
        tx.set(index['path'], _encode('web_session', session.model_copy(update={'revoked': True})))
    await docs.transact(revoke)
    with pytest.raises(AccessDenied):
        await grants.consume_tool(child, 'read_schedule')
    with pytest.raises(AccessDenied):
        await grants.issue_tool(parent, 'read_assignments')


@run_async
async def test_child_budget_and_prompt_payload_limits(tmp_path):
    docs, _, session = await setup(tmp_path)
    grants = StudyGrants(docs, clock=lambda: NOW)
    with pytest.raises(ValueError):
        await invocation(grants, session, message='x' * 4001)
    with pytest.raises(ValueError):
        await invocation(grants, session, history=[{'message': 'x' * 4000,
            'response': 'y' * 12000}] * 20)
    parent = await grants.consume_invocation(await invocation(grants, session))
    for _ in range(8):
        await grants.issue_tool(parent, 'read_assignments')
    with pytest.raises(AccessDenied):
        await grants.issue_tool(parent, 'read_schedule')


@run_async
async def test_progress_is_session_turn_bound_one_use_and_expires_without_running_forever(tmp_path):
    docs, index, session = await setup(tmp_path)
    now = NOW
    grants = StudyGrants(docs, clock=lambda: now)
    binding = dict(owner=session.binding.owner_id, session=session.binding.session_id,
                   chat_id='default', request_key='real_turn')
    progress_id = grants.progress_id(**binding)
    reference = await invocation(grants, session, progress_id=progress_id)
    assert (await grants.read_progress(**binding)).phase == 'queued'
    assert await grants.read_progress(**{**binding, 'request_key': 'other_turn'}) is None
    with pytest.raises(AccessDenied, match='study_progress_replay'):
        await invocation(grants, session, progress_id=progress_id)
    parent = await grants.consume_invocation(reference)
    await grants.progress(parent, phase='thinking')
    await grants.issue_tool(parent, 'read_assignments')
    await grants.progress(parent, call={'call_id': 1, 'tool': 'read_assignments', 'status': 'running'})
    live = await grants.read_progress(**binding)
    assert live.phase == 'tools' and live.tool_calls[0].status == 'running'
    with pytest.raises(AccessDenied):
        await grants.progress({**parent, 'owner': 'other-owner'}, phase='composing')
    with pytest.raises(AccessDenied):
        await grants.progress(parent, call={'call_id': 1, 'tool': 'read_schedule', 'status': 'completed'})
    with pytest.raises(AccessDenied):
        await grants.progress(parent, call={'call_id': 2, 'tool': 'read_schedule', 'status': 'running'})
    now += timedelta(seconds=151)
    expired = await grants.read_progress(**binding)
    assert expired.phase == 'failed' and expired.tool_calls[0].status == 'unknown'
    with pytest.raises(AccessDenied):
        await grants.progress(parent, phase='composing')
    now = NOW
    async def revoke(tx):
        await tx.get(index['path'])
        tx.set(index['path'], _encode('web_session', session.model_copy(update={'revoked': True})))
    await docs.transact(revoke)
    with pytest.raises(AccessDenied):
        await grants.read_progress(**binding)


@run_async
async def test_parallel_progress_calls_preserve_all_results_and_terminal_state(tmp_path):
    docs, _, session = await setup(tmp_path)
    grants = StudyGrants(docs, clock=lambda: NOW)
    binding = dict(owner=session.binding.owner_id, session=session.binding.session_id,
                   chat_id='default', request_key='parallel_turn')
    parent = await grants.consume_invocation(await invocation(grants, session,
        progress_id=grants.progress_id(**binding)))
    for index, tool in enumerate(('read_assignments', 'read_schedule'), 1):
        reference, reserved = await grants.issue_tool(parent, tool, with_call_id=True)
        assert isinstance(reference, str) and reserved == index
    await asyncio.gather(*(grants.progress(parent,
        call={'call_id': index, 'tool': tool, 'status': 'running'})
        for index, tool in enumerate(('read_assignments', 'read_schedule'), 1)))
    await asyncio.gather(*(grants.progress(parent, call={'call_id': index, 'tool': tool,
        'status': status, 'duration_ms': 42, 'result_count': count})
        for index, tool, status, count in [(1, 'read_assignments', 'completed', 15),
                                          (2, 'read_schedule', 'not_connected', 0)]))
    await grants.progress(parent, phase='composing')
    live = await grants.read_progress(**binding)
    assert live.phase == 'composing'
    assert [(item.call_id, item.status, item.result_count) for item in live.tool_calls] == [
        (1, 'completed', 15), (2, 'not_connected', 0)]
    await grants.progress(parent, phase='completed')
    with pytest.raises(AccessDenied, match='study_progress_settled'):
        await grants.progress(parent, phase='thinking')


@run_async
async def test_github_grant_is_bound_to_the_explicit_repository_and_single_use(tmp_path):
    docs, _, session = await setup(tmp_path)
    grants = StudyGrants(docs, clock=lambda: NOW)
    parent = await grants.consume_invocation(await invocation(grants, session,
        message='Разбери https://github.com/student/lab.git, составь чеклист.'))
    with pytest.raises(AccessDenied, match='study_github_repository_rejected'):
        await grants.issue_tool(parent, 'read_github_assignment', repository_url='https://github.com/other/lab')
    with pytest.raises(AccessDenied):
        await grants.issue_tool(parent, 'read_assignments', repository_url='https://github.com/student/lab')
    child = await grants.issue_tool(parent, 'read_github_assignment',
        repository_url='https://github.com/student/lab')
    public = await grants.consume_tool(child, 'read_github_assignment')
    assert public['repository_url'] == 'https://github.com/student/lab'
    assert public['owner'] == session.binding.owner_id
    with pytest.raises(AccessDenied):
        await grants.consume_tool(child, 'read_github_assignment')
    async def tamper(tx):
        from campuspulse_recovery.web.auth import digest
        path = 'cp_study_tool_grants/' + digest(child)
        value = await tx.get(path)
        tx.set(path, {**value, 'consumed': False, 'repository_url': 'https://github.com/other/lab'})
    await docs.transact(tamper)
    with pytest.raises(AccessDenied, match='study_github_repository_rejected'):
        await grants.consume_tool(child, 'read_github_assignment')


@pytest.mark.parametrize('message', [
    'Проверь мой GitHub',
    'Проверь https://github.com/student/first и https://github.com/student/second',
    'Проверь https://github.com.evil.example/student/lab',
])
@run_async
async def test_github_does_not_infer_a_repository_from_history_or_other_urls(tmp_path, message):
    docs, _, session = await setup(tmp_path)
    grants = StudyGrants(docs, clock=lambda: NOW)
    parent = await grants.consume_invocation(await invocation(grants, session, message=message,
        history=[{'message': 'https://github.com/student/lab', 'response': 'Предыдущий разбор'}]))
    with pytest.raises(AccessDenied, match='study_github_repository_rejected'):
        await grants.issue_tool(parent, 'read_github_assignment', repository_url='https://github.com/student/lab')
