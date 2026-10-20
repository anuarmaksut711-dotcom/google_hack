"""Synthetic email/browser actions against durable scoped account handlers."""

from datetime import UTC, datetime, timedelta
import pytest

from campuspulse_recovery.demo.adapters import demo_config
from campuspulse_recovery.domain.ports import PortError
from campuspulse_recovery.lms.multiuser import account_config, create_multiuser_app
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.storage.plans import _decode
from campuspulse_recovery.storage.sqlite_documents import SqliteDocuments
from campuspulse_recovery.web.auth import CHALLENGE_COOKIE, CSRF_COOKIE, SESSION_COOKIE, WebSession, digest
from campuspulse_recovery.web.passwords import EMAIL_ISSUER
from tests.lms.test_multiuser import Agent, ORIGIN, Verifier, client, fetch, login, post, run_async, url
import httpx


PASSWORD = 'A synthetic long passphrase 42'


def application(directory, calls=None, **changes):
    return create_multiuser_app(origin=ORIGIN, client_id='test.apps.googleusercontent.com',
        directory=directory, test_verifier=Verifier(), transport=httpx.MockTransport(fetch),
        agent_factory=lambda: Agent([] if calls is None else calls), **changes)


async def email_action(person, email, password=PASSWORD, *, register=False):
    challenge = (await person.get('/auth/challenge')).json()
    return await person.post('/auth/email/' + ('register' if register else 'signin'),
        json={'email': email, 'password': password}, headers={'X-CSRF-Token': challenge['csrf_token']})


async def session_for(app, person):
    return _decode(await app.state.documents.get('cp_web_sessions/' + digest(person.cookies[SESSION_COOKIE])),
                   'web_session', WebSession)


@run_async
async def test_unverified_email_cannot_acquire_google_recovery_binding(tmp_path):
    resolved = []

    def recovery(principal, *, clock):
        resolved.append(principal)
        return None

    app = application(tmp_path, recovery_factory=recovery)
    async with client(app) as email, client(app) as google:
        assert (await email_action(email, 'alice@gmail.com', register=True)).status_code == 200
        assert not resolved
        await login(google, 'alice')
        assert resolved == ['https://accounts.google.com#alice']


@run_async
async def test_email_signup_signout_relogin_restart_and_google_owner_isolation(tmp_path):
    calls = []
    app = application(tmp_path, calls)
    async with client(app) as alice, client(app) as bob, client(app) as google:
        for person, email in ((alice, 'Alice@Example.com'), (bob, 'bob@example.com')):
            result = await email_action(person, email, register=True)
            assert result.status_code == 200, result.text
            assert result.json()['status'] == 'authenticated'
            assert (await person.get('/')).headers['location'] == '/account'
        await login(google, 'alice')
        alice_session, bob_session, google_session = [await session_for(app, person) for person in (alice, bob, google)]
        assert alice_session.principal_id.startswith(EMAIL_ISSUER + '#')
        assert alice_session.binding.owner_id.startswith('email-')
        assert len({s.binding.owner_id for s in (alice_session, bob_session, google_session)}) == 3
        assert google_session.binding.owner_id == 'google-' + digest('https://accounts.google.com#alice')
        for person, user in ((alice, 42), (bob, 43), (google, 44)):
            assert (await post(person, '/api/account/moodle', {'url': url(user)})).status_code == 200
        assert (await post(alice, '/api/moodle/task-status', {'task_id': 'shared-event', 'done': True})).status_code == 200
        for person in (bob, google):
            assert (await person.get('/api/moodle')).json()['snapshot']['tasks'][0]['local_status'] == 'active'
        assert (await post(alice, '/api/chat', {'request_key': 'email-alice-plan', 'message': 'Мой личный план'})).status_code == 200
        for person in (bob, google):
            assert not (await person.get('/api/chat')).json()['turns']
        assert (await post(alice, '/auth/signout', {})).status_code == 200
        assert (await alice.get('/api/moodle')).status_code == 403
        assert SESSION_COOKIE not in alice.cookies
        assert (await email_action(alice, 'alice@example.COM')).status_code == 200
        assert (await session_for(app, alice)).principal_id == alice_session.principal_id
        assert len((await alice.get('/api/chat')).json()['turns']) == 1
    # Recreate both the dispatcher and encrypted durable adapter; an email or
    # Google account must recover only its own existing state.
    restarted = application(tmp_path, calls)
    async with client(restarted) as alice, client(restarted) as google:
        assert (await email_action(alice, 'ALICE@example.com')).status_code == 200
        assert (await session_for(restarted, alice)).principal_id == alice_session.principal_id
        assert (await alice.get('/api/moodle')).json()['snapshot']['tasks'][0]['local_status'] == 'done'
        assert len((await alice.get('/api/chat')).json()['turns']) == 1
        await login(google, 'alice')
        assert (await google.get('/api/moodle')).json()['snapshot']['tasks'][0]['title'] == 'Private homework 44'
        assert not (await google.get('/api/chat')).json()['turns']
    stored = (tmp_path / 'documents.sqlite3').read_bytes()
    assert PASSWORD.encode() not in stored
    assert b'alice@example.com' not in stored
    assert b'authtoken' not in stored


@run_async
async def test_email_generic_failed_signin_duplicate_registration_and_one_use_challenge(tmp_path):
    app = application(tmp_path)
    async with client(app) as alice, client(app) as anonymous:
        assert (await email_action(alice, 'alice@example.com', register=True)).status_code == 200
        before = await session_for(app, alice)
        wrong = await email_action(anonymous, 'alice@example.com', 'Another long synthetic passphrase')
        missing = await email_action(anonymous, 'missing@example.com', 'Another long synthetic passphrase')
        assert wrong.status_code == missing.status_code == 401
        assert wrong.json() == missing.json() == {'error': 'invalid_credentials'}
        assert SESSION_COOKIE not in anonymous.cookies
        duplicate = await email_action(anonymous, 'ALICE@EXAMPLE.COM', register=True)
        assert duplicate.status_code == 409
        assert duplicate.json() == {'error': 'registration_unavailable'}
        assert SESSION_COOKIE not in anonymous.cookies
        challenge = (await anonymous.get('/auth/challenge')).json()
        cookie = anonymous.cookies[CHALLENGE_COOKIE]
        csrf = challenge['csrf_token']
        payload = {'email': 'alice@example.com', 'password': PASSWORD}
        headers = {'X-CSRF-Token': csrf}
        assert (await anonymous.post('/auth/email/signin', json=payload, headers=headers)).status_code == 200
        assert (await session_for(app, anonymous)).principal_id == before.principal_id
        # Reinstall the exact old browser challenge and CSRF cookies to exercise
        # durable consumption rather than merely the cookie deletion.
        anonymous.cookies.set(CHALLENGE_COOKIE, cookie)
        anonymous.cookies.set(CSRF_COOKIE, csrf)
        assert (await anonymous.post('/auth/email/signin', json=payload, headers=headers)).status_code == 403


@run_async
async def test_email_origin_csrf_expiry_and_browser_identity_fields_fail_closed(tmp_path):
    now = datetime.now(UTC)
    app = application(tmp_path, clock=lambda: now)
    async with client(app) as person:
        payload = {'email': 'alice@example.com', 'password': PASSWORD}
        assert (await person.post('/auth/email/register', json=payload)).status_code == 403
        challenge = (await person.get('/auth/challenge')).json()
        good = {'X-CSRF-Token': challenge['csrf_token']}
        assert (await person.post('/auth/email/register', json=payload)).status_code == 403
        for headers in ({**good, 'Origin': 'https://other.example'}, {**good, 'Sec-Fetch-Site': 'cross-site'},
                        {**good, 'Sec-Fetch-Site': 'same-site'}, {**good, 'X-CSRF-Token': 'forged'}):
            assert (await person.post('/auth/email/register', json=payload, headers=headers)).status_code == 403
        for extra in ({'principal_id': EMAIL_ISSUER + '#' + 'a' * 32}, {'approved': True}, {'owner_id': 'other'}, {'proof': {}}):
            assert (await person.post('/auth/email/register', json={**payload, **extra}, headers=good)).status_code == 400
        assert (await person.post('/auth/email/register?owner=other', json=payload, headers=good)).status_code == 400
        assert (await person.post('/auth/email/register', content='x' * 16385,
                headers={**good, 'Content-Type': 'application/json'})).status_code == 400
        now += timedelta(minutes=6)
        assert (await person.post('/auth/email/register', json=payload, headers=good)).status_code == 403
        assert not await app.state.documents.scan('cp_web_sessions')
        # None of the rejected requests may have provisioned an account.
        now += timedelta(seconds=1)
        assert (await email_action(person, payload['email'], register=True)).status_code == 200


@run_async
async def test_email_validation_and_storage_error_do_not_create_session(tmp_path, monkeypatch):
    app = application(tmp_path)
    async with client(app) as person:
        invalid = await email_action(person, 'not-an-email', register=True)
        assert invalid.status_code == 400
        assert invalid.json() == {'error': 'email_invalid'}
        short = await email_action(person, 'alice@example.com', 'too short', register=True)
        assert short.status_code == 400
        assert short.json() == {'error': 'password_length'}
        challenge = (await person.get('/auth/challenge')).json()
        async def unavailable(callback):
            raise PortError('synthetic_storage_failure')
        monkeypatch.setattr(app.state.documents, 'transact', unavailable)
        result = await person.post('/auth/email/register', json={'email': 'alice@example.com', 'password': PASSWORD},
            headers={'X-CSRF-Token': challenge['csrf_token']})
        assert result.status_code == 503
        assert result.json() == {'error': 'signin_unavailable'}
        assert SESSION_COOKIE not in person.cookies


def test_account_subjects_and_cloud_verifier_guards_remain_explicit(tmp_path):
    google = 'https://accounts.google.com#alice'
    assert account_config(google).student.trusted_user_id == 'google-' + digest(google)
    email = EMAIL_ISSUER + '#' + '1' * 32
    assert account_config(email).student.trusted_user_id == 'email-' + digest(email)
    for principal in ('alice@example.com', EMAIL_ISSUER + '#alice@example.com',
                      EMAIL_ISSUER + '#' + 'A' * 32, 'https://evil.example#' + '1' * 32):
        with pytest.raises(AccessDenied):
            account_config(principal)
    values = demo_config().model_dump()
    values['mode'] = 'cloud'
    config = type(demo_config()).model_validate(values)
    documents = SqliteDocuments(tmp_path, project_id=config.project_id, database_id=config.firestore_database)
    with pytest.raises(ValueError, match='explicit_cloud_dependencies_required'):
        create_multiuser_app(origin=ORIGIN, client_id='test.apps.googleusercontent.com', directory=tmp_path,
            application=config, documents=documents, feed_factory=lambda **arguments: None, test_verifier=Verifier())
