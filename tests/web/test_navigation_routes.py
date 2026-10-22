"""The real authenticated route composition shares navigation without side effects."""
import asyncio
from html.parser import HTMLParser

import httpx
import pytest

from campuspulse_recovery.lms.multiuser import create_multiuser_app
from tests.lms.test_local_google_calendar import calendar, config
from tests.lms.test_multiuser import ORIGIN, Verifier, client, fetch, login, post, url


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.items = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.items.append((tag, dict(attrs)))


@pytest.mark.parametrize('with_calendar', [False, True])
def test_authenticated_routes_share_navigation_and_keep_existing_controls(tmp_path, with_calendar):
    async def scenario():
        calls = []
        cal = calendar(tmp_path / 'calendar', lambda *args: calls.append(args) or {'items': []}) if with_calendar else None
        service = create_multiuser_app(origin=ORIGIN,
            client_id=config()['web']['client_id'], directory=tmp_path / 'users',
            test_verifier=Verifier(), transport=httpx.MockTransport(fetch), local_google_calendar=cal)
        async with client(service) as browser, client(service) as anonymous:
            await login(browser, 'alice')
            assert (await post(browser, '/api/account/moodle', {'url': url(42)})).status_code == 200
            paths = ['/', '/planning', '/settings', '/account'] + (['/google-calendar'] if with_calendar else [])
            for path in paths:
                response = await browser.get(path)
                assert response.status_code == 200, (path, response.text)
                assert 'set-cookie' not in response.headers
                assert "script-src 'self'" in response.headers['content-security-policy']
                tags = Elements(response.text).items
                ids = [attrs['id'] for _, attrs in tags if 'id' in attrs]
                assert len(ids) == len(set(ids)), (path, ids)
                assert ids.count('sidebar') == 1
                assert {'sidebar-toggle', 'chat-library', 'chat-new', 'chat-edit-dialog', 'signout'} <= set(ids)
                links = {attrs['data-view']: attrs for tag, attrs in tags if tag == 'a' and 'data-view' in attrs}
                assert {'assistant', 'tasks', 'plan', 'planning', 'journal'} <= set(links)
                assert ('calendar' in links) == with_calendar
                assert links['planning']['href'] == '/planning'
                assert any(tag == 'a' and attrs.get('href') == '/settings' and
                           'account-link' in attrs.get('class', '') for tag, attrs in tags)
                scripts = [attrs['src'] for tag, attrs in tags if tag == 'script' and 'src' in attrs]
                assert '/assets/sidebar-head.js' in scripts
                if path != '/':
                    assert '/assets/campuspulse-navigation.js' in scripts
                    assert '/assets/campuspulse.js' not in scripts
                    assert links['assistant']['href'] == '/#workspace'
                if path == '/planning':
                    assert {'class-schedule', 'classes-file', 'classes-rows', 'classes-calendar'} <= set(ids)
                    assert not {'classes-reviewed', 'classes-week-complete'} & set(ids)
                    assert '/assets/planning-classes.js' in scripts
                if path in ('/settings', '/account'):
                    assert {'connect-moodle', 'moodle-url', 'account-status'} <= set(ids)
                    assert '/assets/account.js' in scripts
                if path == '/google-calendar':
                    assert {'calendar-connect', 'calendar-refresh', 'calendar-disconnect', 'calendar-classes-review'} <= set(ids)
                    assert '/assets/google-calendar.js' in scripts
            for path in ('/assets/sidebar-head.js', '/assets/campuspulse-navigation.js', '/assets/campuspulse-navigation.css'):
                response = await browser.get(path)
                assert response.status_code == 200 and 'set-cookie' not in response.headers
                assert response.headers['x-content-type-options'] == 'nosniff'
            # Public identity and OAuth bridges retain their original isolated layout.
            assert 'id="sidebar"' not in (await anonymous.get('/login')).text
            assert (await anonymous.get('/planning')).status_code == 403
            if with_calendar:
                for path in ('/google-calendar/connect', '/auth/google-calendar/callback'):
                    response = await anonymous.get(path)
                    assert response.status_code == 200
                    assert 'id="sidebar"' not in response.text
                assert (await browser.get('/api/account/google-calendar')).json() == {'connected': False}
            assert calls == [], 'Rendering/navigation must not read or write Calendar events.'
    asyncio.run(scenario())
