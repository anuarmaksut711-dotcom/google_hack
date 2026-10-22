"""Shared appearance assets and browser behavior without external services."""
import asyncio
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess

import pytest

from campuspulse_recovery.web.assets.theme import THEME_SCRIPT, install_theme
from campuspulse_recovery.web.assets.theme_styles import THEME_STYLES
from tests.lms.test_multiuser import application, client, login, post, url
from tests.web.test_login_surface import Page, application as login_application


def test_theme_assets_are_public_and_installation_is_idempotent():
    async def scenario():
        setup, service = login_application(email_password=True)
        install_theme(service)
        for path in ('/assets/theme.js', '/assets/theme.css'):
            assert sum(getattr(route, 'path', None) == path for route in service.routes) == 1
        snapshot = deepcopy(setup.documents.data)
        async with setup.client(service) as browser:
            for path, media, content in (
                ('/assets/theme.js', 'text/javascript', THEME_SCRIPT),
                ('/assets/theme.css', 'text/css', THEME_STYLES),
            ):
                response = await browser.get(path)
                assert response.status_code == 200 and response.text == content
                assert response.headers['content-type'].split(';')[0] == media
                assert response.headers['x-content-type-options'] == 'nosniff'
                assert response.headers['cache-control'] == 'no-store'
                assert 'set-cookie' not in response.headers
            assert not browser.cookies
        assert setup.documents.data == snapshot
    asyncio.run(scenario())


def test_multiuser_pages_share_theme_without_weakening_content_security(tmp_path):
    async def scenario():
        service = application(tmp_path, [])
        async with client(service) as browser:
            for path in ('/assets/theme.js', '/assets/theme.css'):
                response = await browser.get(path)
                assert response.status_code == 200
                assert 'set-cookie' not in response.headers
            assert not browser.cookies
            responses = [(path, await browser.get(path)) for path in ('/login', '/register')]
            await login(browser, 'alice')
            assert (await post(browser, '/api/account/moodle', {'url': url(42)})).status_code == 200
            responses.extend([(path, await browser.get(path)) for path in ('/', '/account', '/settings')])
            for path, response in responses:
                assert response.status_code == 200
                page = Page(response.text)
                scripts = [attributes for tag, attributes in page.elements if tag == 'script']
                assert scripts[0]['src'] == '/assets/theme.js'
                assert 'defer' not in scripts[0] and 'async' not in scripts[0]
                assert response.text.index('/assets/theme.js') < response.text.index('</head>')
                styles = [attributes['href'] for tag, attributes in page.elements
                    if tag == 'link' and attributes.get('rel') == 'stylesheet']
                assert styles[-1] == '/assets/theme.css'
                assert len(styles) >= 2
                buttons = [(tag, attributes) for tag, attributes in page.elements if 'data-theme-toggle' in attributes]
                if path in ('/account', '/settings'):
                    assert len(buttons) == 1
                    tag, attributes = buttons[0]
                    assert tag == 'button' and attributes['type'] == 'button'
                    assert attributes['aria-label'] and attributes['title']
                    assert 'onclick' not in attributes
                    assert 'id="appearance-heading"' in response.text
                else:
                    assert not buttons
                directives = dict(part.strip().split(' ', 1)
                    for part in response.headers['content-security-policy'].split(';') if part.strip())
                assert directives['default-src'] == "'none'"
                assert directives['script-src'] in ("'self'", "'self' https://accounts.google.com/gsi/client")
                assert directives['style-src'].startswith("'self'")
                assert directives['frame-ancestors'] == "'none'"
                assert directives['form-action'] == "'self'"
                assert "'unsafe-inline'" not in response.headers['content-security-policy']
                assert "'unsafe-eval'" not in response.headers['content-security-policy']
                assert 'set-cookie' not in response.headers
    asyncio.run(scenario())


def test_shipped_theme_script_persists_and_synchronizes_appearance():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for theme state regressions')
    result = subprocess.run([node, str(Path(__file__).with_name('theme_state.cjs'))],
        input=THEME_SCRIPT, capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
