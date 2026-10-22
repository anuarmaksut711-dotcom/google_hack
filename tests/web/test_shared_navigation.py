"""Shared shell preserves page controls and authenticated chat navigation."""
import asyncio
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

from fastapi import FastAPI
import httpx
import pytest

from campuspulse_recovery.web.assets.page_navigation import SCRIPT
from campuspulse_recovery.web.google_calendar import PAGE as CALENDAR_PAGE
from campuspulse_recovery.web.recovery_planning import PAGE as PLANNING_PAGE
from campuspulse_recovery.web.views.navigation import (
    install_page_navigation, render_navigation, wrap_page,
)


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_navigation_is_one_labeled_structure_with_workspace_controls_on_every_page():
    for home_url in ('', '/'):
        html = render_navigation(home_url=home_url, planning_url='/planning',
                                 account_url='/settings', moodle_enabled=True,
                                 google_calendar_enabled=True, active='planning')
        tags = Tags(html).tags
        ids = [attrs['id'] for _, attrs in tags if 'id' in attrs]
        assert len(ids) == len(set(ids))
        assert sum(tag == 'aside' for tag, _ in tags) == 1
        assert 'activity-rail' not in html
        assert {'sidebar', 'sidebar-toggle', 'chat-new', 'focus-composer', 'signout',
                'chat-library', 'chat-folder-create', 'chat-action-menu',
                'chat-edit-dialog', 'chat-edit-form', 'chat-library-retry'} <= set(ids)
        links = {attrs.get('data-view'): attrs for tag, attrs in tags
                 if tag == 'a' and 'data-view' in attrs}
        assert links['assistant']['href'] == home_url + '#workspace'
        assert links['tasks']['href'] == home_url + '#moodle'
        assert links['plan']['href'] == home_url + '#panel'
        assert links['calendar']['href'] == home_url + '#calendar'
        assert links['journal']['href'] == home_url + '#journal-heading'
        assert links['planning']['href'] == '/planning'
        assert links['planning']['aria-current'] == 'page'
        assert all('aria-current' not in value for key, value in links.items() if key != 'planning')
        assert all(attrs.get('aria-label') and attrs.get('title') for attrs in links.values())
        assert '<span class="nav-label">Новый чат</span>' in html
        assert '<span class="nav-label">Выйти</span>' in html


def test_navigation_respects_feature_flags_and_escapes_targets():
    html = render_navigation(home_url='/?source="<demo>', account_url='/account?x="',
                             active='settings')
    tags = Tags(html).tags
    views = [attrs.get('data-view') for _, attrs in tags]
    assert 'tasks' not in views and 'calendar' not in views and 'planning' not in views
    assert 'data-view="plan"' in html and 'data-view="journal"' in html
    assert '&quot;&lt;demo&gt;' in html
    assert '<demo>' not in html
    account = next(attrs for tag, attrs in tags if tag == 'a' and attrs.get('class', '').startswith('account-link'))
    assert account['href'] == '/account?x="'
    assert account['aria-current'] == 'page'


@pytest.mark.parametrize('page,active,title', [
    (PLANNING_PAGE, 'planning', 'Пары и время'),
    (CALENDAR_PAGE, 'calendar', 'Google Calendar'),
])
def test_standalone_shell_preserves_original_forms_notices_and_scripts(page, active, title):
    html = wrap_page(page, active=active, title=title)
    before, after = Tags(page).tags, Tags(html).tags
    original_controls = [(tag, attrs) for tag, attrs in before if tag in {'input', 'select', 'textarea', 'form', 'button'}]
    assert original_controls
    assert all(control in after for control in original_controls)
    scripts = [attrs for tag, attrs in after if tag == 'script']
    assert all(attrs in scripts for tag, attrs in before if tag == 'script')
    assert all('src' in attrs and not any(key.startswith('on') for key in attrs) for attrs in scripts)
    assert scripts[0]['src'] == '/assets/theme.js'
    assert sum(attrs.get('id') == 'sidebar' for _, attrs in after) == 1
    body = next(attrs for tag, attrs in after if tag == 'body')
    assert body['data-shared-navigation'] == 'true'
    assert body['data-view'] == active
    styles = [attrs['href'] for tag, attrs in after if tag == 'link' and attrs.get('rel') == 'stylesheet']
    assert styles[-1] == '/assets/theme.css'
    assert '/assets/campuspulse-navigation.css' in styles
    assert '/assets/campuspulse.js' not in html
    assert wrap_page(html, active=active, title=title) == html
    if active == 'planning':
        assert 'Перед добавлением событий в Google Calendar вы проверите и подтвердите расписание.' in html
        assert page[page.index('<main'):page.index('</main>') + len('</main>')] in html
    else:
        assert 'Добавить пары в Google Calendar' in html
        assert 'в настройках Google-аккаунта → Безопасность → Сторонние приложения.' in html


def test_wrap_preserves_body_attributes_without_adding_appearance_controls():
    page = ('<!doctype html><html lang="ru"><head><title>Old</title>'
            '<script src="/assets/theme.js"></script><link rel="stylesheet" href="/assets/theme.css">'
            '</head><body class="settings-page" data-extra="kept"><header></header>'
            '<main><form id="existing"><button type="submit">Сохранить</button></form></main>'
            '<script src="/assets/account.js"></script></body></html>')
    html = wrap_page(page, active='settings', title='Настройки <safe>')
    tags = Tags(html).tags
    assert next(attrs for tag, attrs in tags if tag == 'body')['class'] == 'settings-page'
    assert next(attrs for tag, attrs in tags if tag == 'body')['data-extra'] == 'kept'
    assert not any('data-theme-toggle' in attrs for _, attrs in tags)
    assert 'Настройки &lt;safe&gt; · Google AI' in html
    assert '<form id="existing"><button type="submit">Сохранить</button></form>' in html


def test_navigation_assets_are_self_hosted_idempotent_and_have_no_agent_client():
    async def scenario():
        app = FastAPI()
        install_page_navigation(app)
        install_page_navigation(app)
        paths = ('/assets/campuspulse-navigation.css', '/assets/campuspulse-navigation.js', '/assets/sidebar-head.js')
        for path in paths:
            assert sum(getattr(route, 'path', None) == path for route in app.routes) == 1
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://campus.test') as browser:
            for path in paths:
                response = await browser.get(path)
                assert response.status_code == 200
                assert 'set-cookie' not in response.headers
                if path.endswith('.css'):
                    assert 'text/css' in response.headers['content-type']
                    assert '--cp-sidebar-motion' in response.text
                    assert '.chat-edit-dialog' in response.text
                else:
                    assert 'text/javascript' in response.headers['content-type']
            script = (await browser.get(paths[1])).text
            assert 'window.CampusPulseChats = class' in script
            assert 'class PageChats extends window.CampusPulseChats' in script
            assert '/api/calendar/' not in script
            assert '/api/moodle/' not in script
    asyncio.run(scenario())


def test_standalone_navigation_auth_csrf_and_explicit_actions():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for shared navigation behavior')
    result = subprocess.run([node, str(Path(__file__).with_name('shared_navigation.cjs'))],
                            input=SCRIPT, capture_output=True, text=True,
                            encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
