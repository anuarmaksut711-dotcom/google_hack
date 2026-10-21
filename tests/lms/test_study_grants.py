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
