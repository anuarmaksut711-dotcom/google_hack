"""Saved connection UX across sessions and app instances; no live Google/Moodle."""
from html.parser import HTMLParser

import httpx

from campuspulse_recovery.lms.multiuser import create_multiuser_app
from tests.lms.test_multiuser import (ORIGIN, Verifier, application, client, fetch, login,
                                     post, run_async, url)


class SettingsForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.details = []
        self.url_requires_explicit_change = False
        self.input_value = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'details':
            self.details.append(attrs)
        if tag == 'input' and attrs.get('id') == 'moodle-url':
            self.url_requires_explicit_change = any(
                d.get('id') == 'moodle-change' and 'open' not in d for d in self.details)
            self.input_value = attrs.get('value')

    def handle_endtag(self, tag):
        if tag == 'details':
            self.details.pop()


def assert_saved_settings(response):
    assert response.status_code == 200
    assert 'Moodle подключён.' in response.text
    form = SettingsForm()
    form.feed(response.text)
    assert form.url_requires_explicit_change and form.input_value is None
    assert 'authtoken' not in response.text and url(42) not in response.text


@run_async
async def test_same_google_account_connect_once_new_session_and_server_restart(tmp_path):
    app = application(tmp_path, [])
    async with client(app) as first:
        await login(first, 'alice')
        form = SettingsForm()
        form.feed((await first.get('/settings')).text)
        assert not form.url_requires_explicit_change
        assert (await post(first, '/api/account/moodle', {'url': url(42)})).status_code == 200
        assert (await first.get('/')).status_code == 200
        assert 'href="/settings"' in (await first.get('/')).text
        assert_saved_settings(await first.get('/settings'))
        assert (await post(first, '/auth/signout', {})).status_code == 200

    # New cookies and a new Google sign-in, with no URL submission.
    async with client(app) as second:
        await login(second, 'alice')
        assert (await second.get('/')).status_code == 200
        assert_saved_settings(await second.get('/account'))  # Old link still works.
        assert (await second.get('/api/moodle')).json()['snapshot']['tasks'][0]['title'] == 'Private homework 42'

    recreated = application(tmp_path, [])
    async with client(recreated) as third, client(recreated) as other:
        await login(third, 'alice')
        assert (await third.get('/')).status_code == 200
        assert_saved_settings(await third.get('/settings'))
        await login(other, 'bob')
        assert (await other.get('/')).headers['location'] == '/account'
        assert 'Moodle подключён.' not in (await other.get('/settings')).text
        assert (await other.get('/api/moodle')).json()['connected'] is False


@run_async
async def test_settings_and_login_reuse_saved_binding_without_refetching_feed(tmp_path):
    calls = []
    available = True

    def transport(request):
        calls.append(request.url.path)
        return fetch(request) if available else httpx.Response(503)

    def service():
        return create_multiuser_app(origin=ORIGIN, client_id='test.apps.googleusercontent.com',
            directory=tmp_path, test_verifier=Verifier(), transport=httpx.MockTransport(transport))

    app = service()
    async with client(app) as first:
        await login(first, 'alice')
        assert (await post(first, '/api/account/moodle', {'url': url(42)})).status_code == 200
    assert len(calls) == 1
    available = False
    async with client(service()) as returning:
        await login(returning, 'alice')
        assert (await returning.get('/')).status_code == 200
        assert_saved_settings(await returning.get('/settings'))
        assert (await returning.get('/api/moodle')).json()['connected'] is True
        assert len(calls) == 1  # Re-entry never validates the saved URL over the network.
        # A failed replacement must preserve the existing saved connection.
        assert (await post(returning, '/api/account/moodle', {'url': url(43)})).status_code == 400
        assert_saved_settings(await returning.get('/settings'))
        assert (await returning.get('/api/moodle')).json()['snapshot']['tasks'][0]['title'] == 'Private homework 42'


@run_async
async def test_replacing_connection_in_settings_is_seen_by_existing_local_replica(tmp_path):
    first, second = application(tmp_path, []), application(tmp_path, [])
    async with client(first) as one, client(second) as two:
        await login(one, 'alice')
        await login(two, 'alice')  # Cache an unconnected app on another replica.
        assert (await two.get('/api/moodle')).json()['connected'] is False
        assert (await post(one, '/api/account/moodle', {'url': url(42)})).status_code == 200
        assert_saved_settings(await two.get('/settings'))
        assert (await two.get('/api/moodle')).json()['snapshot']['tasks'][0]['title'] == 'Private homework 42'
        assert (await post(two, '/api/account/moodle', {'url': url(43)})).status_code == 200
        assert (await one.get('/api/moodle')).json()['snapshot']['tasks'][0]['title'] == 'Private homework 43'


@run_async
async def test_settings_require_session_and_do_not_grant_connection_write(tmp_path):
    app = application(tmp_path, [])
    async with client(app) as anonymous, client(app) as signed_in:
        assert (await anonymous.get('/settings')).headers['location'] == '/login'
        await login(signed_in, 'alice')
        assert (await signed_in.get('/settings')).status_code == 200
        assert (await signed_in.get('/')).headers['location'] == '/account'
        assert (await signed_in.post('/api/account/moodle', json={'url': url(42)})).status_code == 403
        assert (await signed_in.get('/api/moodle')).json()['connected'] is False
