import asyncio
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

from campuspulse_recovery.cloud.connected_calendar import EncryptedCalendarDocuments, CalendarReadGrants
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.storage.sqlite_documents import SqliteDocuments
from campuspulse_recovery.storage.plans import _decode, _encode
from campuspulse_recovery.web.auth import WebSession
from campuspulse_recovery.lms.multiuser import create_multiuser_app
from tests.lms.test_multiuser import ORIGIN, Verifier, client, login, run_async
from tests.lms.test_local_google_calendar import NOW


def test_cloud_read_includes_today_before_now_and_three_future_days():
    from datetime import datetime, UTC
    from campuspulse_recovery.cloud.connected_calendar import CloudConnectedCalendar
    calendar = CloudConnectedCalendar.__new__(CloudConnectedCalendar)
    now = datetime.fromisoformat('2026-10-21T17:00:00+00:00')
    start, end = calendar.window(now, 'Asia/Almaty')
    assert start == datetime.fromisoformat('2026-10-20T19:00:00+00:00')
    assert end == now + timedelta(hours=72)


@run_async
async def test_explicit_connected_calendar_can_differ_from_login_but_nonce_is_required(tmp_path, monkeypatch):
    from tests.lms.test_local_google_calendar import calendar, FakeFlow
    from campuspulse_recovery.integrations.local_calendar import CalendarConnectionError
    flow = FakeFlow()
    nonce = None
    monkeypatch.setattr('google.oauth2.id_token.verify_oauth2_token', lambda *a, **kw:
        {'sub': 'explicit-calendar-account', 'nonce': nonce})
    for valid in (False, True):
        cal = calendar(tmp_path / str(valid), lambda *_: {'primary': True, 'accessRole': 'owner',
            'id': 'calendar-account@example.test', 'timeZone': 'Asia/Almaty'})
        cal.require_matching_login = False
        monkeypatch.setattr(cal, 'flow', lambda **kwargs: flow)
        await cal.start(owner='website-owner', session='website-session')
        nonce = flow.nonce if valid else 'wrong'
        args = dict(owner='website-owner', session='website-session',
            principal='https://accounts.google.com#website-login-account', state='x'*32, code='authorization-code')
        if not valid:
            with pytest.raises(CalendarConnectionError):
                await cal.exchange(**args)
        else:
            result = await cal.exchange(**args)
            assert result['owner'] == 'website-owner' and result['subject'] == 'explicit-calendar-account'


@pytest.mark.parametrize('read_at,expected_date,label', [
    ('2026-10-20T18:59:00+00:00', '2026-10-20', 'завтра'),
    ('2026-10-20T19:00:00+00:00', '2026-10-21', 'сегодня'),
    ('2026-10-19T19:00:00+00:00', '2026-10-20', 'завтра')])
def test_agenda_day_labels_use_local_midnight(read_at, expected_date, label):
    from campuspulse_recovery.integrations.calendar_agenda import agenda_source
    source = agenda_source({'timezone': 'Asia/Almaty', 'read_at': read_at, 'horizon': {}, 'events': [
        {'summary': 'Завтрак', 'start': {'dateTime': '2026-10-21T09:00:00+05:00'}, 'end': {}},
        {'summary': 'All day', 'start': {'date': '2026-10-21'}, 'end': {}}]})
    assert source['current_date'] == expected_date
    assert [e['day_label'] for e in source['events']] == [label, label]
    assert [e['local_date'] for e in source['events']] == ['2026-10-21']*2


@run_async
async def test_cloud_oauth_ciphertext_binds_document_and_survives_restart(tmp_path):
    raw = SqliteDocuments(tmp_path, project_id='test-project', database_id='test')
    key = Fernet.generate_key()
    docs = EncryptedCalendarDocuments(raw, key)
    path = 'cp_local_google_calendars/alice'
    value = {'tokens': {'refresh_token': 'private-user-refresh'}, 'owner': 'alice'}
    async def save(tx):
        await tx.get(path)
        tx.create(path, value)
    await docs.transact(save)
    stored = await raw.get(path)
    assert set(stored) == {'ciphertext'} and 'private-user-refresh' not in str(stored)
    assert await EncryptedCalendarDocuments(raw, key).get(path) == value
    async def substitute(tx):
        await tx.get('cp_local_google_calendars/bob')
        tx.set('cp_local_google_calendars/bob', stored)
    await raw.transact(substitute)
    with pytest.raises(ValueError, match='binding_mismatch'):
        await docs.get('cp_local_google_calendars/bob')
    with pytest.raises(ValueError):
        await docs.get('cp_web_sessions/alice')


@run_async
async def test_read_grants_check_owner_expiry_replay_and_revoked_sessions(tmp_path):
    app = create_multiuser_app(origin=ORIGIN, client_id='client', directory=tmp_path, test_verifier=Verifier(), clock=lambda: NOW)
    async with client(app) as browser:
        await login(browser, 'alice')
    raw = app.state.documents
    index = (await raw.scan('cp_web_session_index'))[0]
    session = _decode(await raw.get(index['path']), 'web_session', WebSession)
    owner, session_id = session.binding.owner_id, session.binding.session_id
    now = NOW
    grants = CalendarReadGrants(raw, clock=lambda: now)
    with pytest.raises(AccessDenied):
        await grants.issue('other-owner', session_id)
    reference = await grants.issue(owner, session_id)
    results = await asyncio.gather(grants.consume(reference), grants.consume(reference), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    reference = await grants.issue(owner, session_id)
    now += timedelta(seconds=61)
    with pytest.raises(AccessDenied):
        await grants.consume(reference)
    now = NOW
    reference = await grants.issue(owner, session_id)
    async def revoke(tx):
        await tx.get(index['path'])
        tx.set(index['path'], _encode('web_session', session.model_copy(update={'revoked': True})))
    await raw.transact(revoke)
    with pytest.raises(AccessDenied):
        await grants.consume(reference)


@run_async
async def test_chat_reads_only_its_connected_calendar_and_never_exposes_tokens(tmp_path):
    from tests.lms.test_local_google_calendar import calendar, connect, event
    from tests.lms.test_multiuser import Agent, post
    cal = calendar(tmp_path / 'calendar', lambda *_: {'items': [event(summary='Завтрак')]})
    calls = []
    app = create_multiuser_app(origin=ORIGIN, client_id='client', directory=tmp_path / 'app',
        test_verifier=Verifier(), clock=lambda: NOW, local_google_calendar=cal,
        agent_factory=lambda: Agent(calls))
    async with client(app) as alice, client(app) as bob:
        await login(alice, 'alice')
        index = (await app.state.documents.scan('cp_web_session_index'))[0]
        session = _decode(await app.state.documents.get(index['path']), 'web_session', WebSession)
        await connect(cal, owner=session.binding.owner_id)
        await login(bob, 'bob')
        for browser in (alice, bob):
            result = await post(browser, '/api/chat', {'request_key': 'daily-plan', 'message': 'Что у меня по планам?'})
            assert result.status_code == 200
            assert result.json()['turns'][0]['status'] == 'completed'
    assert calls[0]['sources']['google_calendar']['events'][0]['summary'] == 'Завтрак'
    assert calls[1]['sources']['google_calendar'] == {'status': 'not_connected'}
    assert 'refresh_token' not in str(calls) and 'private-refresh-token' not in str(calls)


@pytest.mark.parametrize('overrides', [{}, {'email': 'another-service@example.com'},
    {'email_verified': False}, {'aud': 'https://other.run.app'}, {'iss': 'https://evil.example'}, {'exp': 1}])
@run_async
async def test_private_calendar_transport_verifies_exact_backend_identity(monkeypatch, overrides):
    from datetime import datetime, UTC
    from starlette.requests import Request
    from campuspulse_recovery.cloud.connected_calendar_mcp import BackendIdentity
    from google.oauth2 import id_token
    audience, principal = 'https://mcp.example.run.app', 'website@example.com'
    now = datetime.now(UTC).timestamp()
    claims = {'aud': audience, 'email': principal, 'email_verified': True,
              'iss': 'https://accounts.google.com', 'sub': 'service-sub', 'iat': now-1, 'exp': now+60, **overrides}
    monkeypatch.setattr(id_token, 'verify_oauth2_token', lambda *a, **kw: claims)
    request = Request({'type': 'http', 'headers': [(b'authorization', b'Bearer signed-token')]})
    identity = BackendIdentity(audience, principal)
    if overrides:
        with pytest.raises(AccessDenied):
            await identity.verify(request)
    else:
        await identity.verify(request)
        duplicate = Request({'type': 'http', 'headers': [(b'authorization', b'Bearer signed-token')]*2})
        with pytest.raises(AccessDenied):
            await identity.verify(duplicate)
