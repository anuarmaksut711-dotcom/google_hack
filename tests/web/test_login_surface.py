"""Public auth screens preserve verified Google sign-in and read-only navigation."""

import asyncio
from copy import deepcopy
from html.parser import HTMLParser
import re

import pytest

from campuspulse_recovery.cloud.login import install_login
from campuspulse_recovery.web.auth import CHALLENGE_COOKIE, SESSION_COOKIE, WebAuth
from tests.web.test_approval import ORIGIN, Setup


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        self.elements.append((tag, dict(attributes)))

    def by_id(self, identifier):
        return next((tag, attrs) for tag, attrs in self.elements if attrs.get('id') == identifier)


def application(*, moodle=True, client_id='test.apps.googleusercontent.com', email_password=False):
    harness = Setup()
    app = harness.app()
    auth = WebAuth(harness.documents, harness.policy, verifier=harness.verifier,
        origin=ORIGIN, environment='local', clock=harness.clock)
    install_login(app, auth, client_id, moodle=moodle, email_password=email_password)
    return harness, app


def test_login_and_registration_are_read_only_google_surfaces_with_safe_client_id():
    async def scenario():
        client_id = 'client" data-injected="unsafe<value>'
        harness, app = application(client_id=client_id)
        before = deepcopy(harness.documents.data)
        async with harness.client(app) as client:
            responses = [await client.get(path) for path in ('/login', '/register')]
            for response, mode, destination in zip(responses,
                    ('signin', 'signup'), ('/register', '/login'), strict=True):
                assert response.status_code == 200
                page = Page(response.text)
                body = next(attrs for tag, attrs in page.elements if tag == 'body')
                assert body['data-client-id'] == client_id
                assert 'data-injected' not in body
                assert body['data-auth-mode'] == mode
                assert page.by_id('google-signin')[0] == 'div'
                assert page.by_id('login-status')[1]['role'] == 'status'
                assert page.by_id('login-retry')[1]['type'] == 'button'
                assert not any(tag in ('input', 'form') for tag, _ in page.elements)
                assert destination in [attrs.get('href') for tag, attrs in page.elements if tag == 'a']
                assert [attrs.get('src') for tag, attrs in page.elements if tag == 'script'] == [
                    '/assets/theme.js', 'https://accounts.google.com/gsi/client', '/assets/login.js']
                assert 'set-cookie' not in response.headers
            assert not client.cookies
        assert harness.documents.data == before
    asyncio.run(scenario())


def test_both_auth_screens_have_google_popup_security_headers():
    async def scenario():
        harness, app = application()
        async with harness.client(app) as client:
            login = await client.get('/login')
            registration = await client.get('/register')
            policies = [re.sub(r"'nonce-[A-Za-z0-9_-]+'", "'nonce-PER_RESPONSE'",
                response.headers['content-security-policy']) for response in (login, registration)]
            assert policies[0] == policies[1]
            for response in (login, registration):
                directives = dict(part.strip().split(' ', 1)
                    for part in response.headers['content-security-policy'].split(';') if part.strip())
                assert directives['default-src'] == "'none'"
                assert directives['script-src'] == "'self' https://accounts.google.com/gsi/client"
                assert directives['frame-src'] == 'https://accounts.google.com/gsi/'
                assert directives['form-action'] == "'self'"
                assert directives['frame-ancestors'] == "'none'"
                assert "'unsafe-inline'" not in response.headers['content-security-policy']
                assert "'unsafe-eval'" not in response.headers['content-security-policy']
                assert response.headers['cross-origin-opener-policy'] == 'same-origin-allow-popups'
                assert response.headers['cache-control'] == 'no-store'
            for path, media in (('/assets/login.css', 'text/css'), ('/assets/login.js', 'text/javascript')):
                response = await client.get(path)
                assert response.status_code == 200
                assert response.headers['content-type'].split(';')[0] == media
    asyncio.run(scenario())


def test_google_style_nonce_is_fresh_and_matches_the_provider_script_only():
    async def scenario():
        harness, app = application()
        async with harness.client(app) as client:
            challenge = (await client.get('/auth/challenge')).json()
            nonces = []
            for path in ('/login', '/register', '/login'):
                response = await client.get(path)
                policy = response.headers['content-security-policy']
                nonce_sources = re.findall(r"'nonce-([A-Za-z0-9_-]+)'", policy)
                assert len(nonce_sources) == 1
                nonce = nonce_sources[0]
                assert len(nonce) >= 24
                scripts = [attrs for tag, attrs in Page(response.text).elements if tag == 'script']
                provider = next(attrs for attrs in scripts if attrs.get('src') == 'https://accounts.google.com/gsi/client')
                assert provider['nonce'] == nonce
                assert all('nonce' not in attrs for attrs in scripts if attrs is not provider)
                assert challenge['nonce'] not in response.text
                assert challenge['nonce'] not in policy
                style_directive = next(part.strip() for part in policy.split(';') if part.strip().startswith('style-src '))
                assert f"'nonce-{nonce}'" in style_directive
                assert "'unsafe-inline'" not in policy
                assert "'unsafe-eval'" not in policy
                nonces.append(nonce)
            assert len(set(nonces)) == len(nonces)
    asyncio.run(scenario())


def test_single_user_deployment_does_not_advertise_or_create_registration():
    async def scenario():
        harness, app = application(moodle=False)
        async with harness.client(app) as client:
            response = await client.get('/login')
            assert response.status_code == 200
            assert '/register' not in [attrs.get('href') for tag, attrs in Page(response.text).elements if tag == 'a']
            assert (await client.get('/register')).status_code == 404
    asyncio.run(scenario())


def test_enabled_email_forms_have_labels_password_manager_hints_and_keep_google():
    async def scenario():
        harness, app = application(email_password=True)
        async with harness.client(app) as client:
            for path, autocomplete, minimum in (('/register', 'new-password', '15'),
                    ('/login', 'current-password', '1')):
                response = await client.get(path)
                page = Page(response.text)
                assert page.by_id('google-signin')[0] == 'div'
                assert page.by_id('email-auth')[1]['method'] == 'post'
                email = page.by_id('auth-email')[1]
                assert email['type'] == 'email' and email['autocomplete'] == 'username'
                password = page.by_id('auth-password')[1]
                assert password['type'] == 'password'
                assert password['autocomplete'] == autocomplete
                assert password['minlength'] == minimum
                assert password['maxlength'] == '128'
                labels = {attrs['for'] for tag, attrs in page.elements if tag == 'label'}
                assert labels == {'auth-email', 'auth-password'}
                assert 'disabled' in page.by_id('email-submit')[1]
            script = (await client.get('/assets/login.js')).text
            assert '/auth/email/register' in script and '/auth/email/signin' in script
            assert 'password: form.elements.password.value}' in script
            assert 'password.value.trim' not in script
        assert not harness.documents.data
    asyncio.run(scenario())


@pytest.mark.parametrize('path', ['/login', '/register'])
@pytest.mark.parametrize('headers', [{'Origin': 'https://foreign.test'}, {'Sec-Fetch-Site': 'cross-site'}])
def test_public_auth_navigation_still_checks_request_origin(path, headers):
    async def scenario():
        harness, app = application()
        async with harness.client(app) as client:
            response = await client.get(path, headers=headers)
            assert response.status_code == 403
            assert not harness.documents.data
            assert not client.cookies
    asyncio.run(scenario())


def test_registration_navigation_does_not_bypass_or_consume_google_challenge():
    async def scenario():
        harness, app = application()
        async with harness.client(app) as client:
            challenge = (await client.get('/auth/challenge')).json()
            saved = deepcopy(harness.documents.data)
            assert CHALLENGE_COOKIE in client.cookies
            assert (await client.get('/register')).status_code == 200
            assert harness.documents.data == saved
            assert SESSION_COOKIE not in client.cookies
            headers = {'Origin': ORIGIN, 'X-CSRF-Token': challenge['csrf_token']}
            forged = await client.post('/auth/signin', headers=headers, json={'credential': 'forged-token'})
            assert forged.status_code == 403
            assert SESSION_COOKIE not in client.cookies
            signed = {'credential': 'synthetic-signed-token'}
            assert (await client.post('/auth/signin', headers=headers, json=signed)).status_code == 200
            assert SESSION_COOKIE in client.cookies
            assert (await client.post('/auth/signin', headers=headers, json=signed)).status_code == 403
    asyncio.run(scenario())


@pytest.mark.parametrize('path', ['/login', '/register'])
def test_external_top_level_links_open_public_auth_without_creating_authority(path):
    async def scenario():
        harness, app = application(email_password=True)
        before = deepcopy(harness.documents.data)
        async with harness.client(app) as client:
            headers = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate',
                       'Sec-Fetch-Dest': 'document'}
            assert (await client.get(path, headers=headers)).status_code == 200
            assert not client.cookies and harness.documents.data == before
            assert (await client.get('/auth/challenge', headers=headers)).status_code == 403
            assert (await client.post('/auth/signin', headers=headers,
                json={'credential': 'forged-token'})).status_code == 403
            assert (await client.get(path, headers={**headers,
                'Sec-Fetch-Dest': 'iframe'})).status_code == 403
    asyncio.run(scenario())
