"""One navigation structure for the workspace and authenticated utility pages."""
from html import escape
import re

from ..assets.workspace import CHAT_NAV


def _e(value):
    return escape(str(value), quote=True)


def _icon(path):
    return '<svg viewBox="0 0 24 24" aria-hidden="true">' + path + '</svg>'


def render_navigation(*, home_url='', planning_url=None, account_url=None,
                      active='assistant', moodle_enabled=False,
                      google_calendar_enabled=False):
    """Render identical controls; standalone links return to the real workspace."""
    def link(view, label, target, path, *, aria=None, element_id=None):
        current = active == view
        identifier = f' id="{_e(element_id)}"' if element_id else ''
        return (f'<a{identifier}'
                f' class="nav-link{" current" if current else ""}" data-view="{_e(view)}"'
                f' href="{_e(target)}" aria-label="{_e(aria or label)}" title="{_e(aria or label)}"'
                + (' aria-current="page"' if current else '') + '>'
                + _icon(path) + f'<span class="nav-label">{_e(label)}</span></a>')

    links = [link('assistant', 'Ассистент', home_url + '#workspace',
                  '<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5a9.5 9.5 0 1 1 19 0Z"/><path d="M7 10h8M7 14h5"/>',
                  element_id='focus-composer')]
    if moodle_enabled:
        links.append(link('tasks', 'Мои задания', home_url + '#moodle',
                          '<rect x="4" y="4" width="16" height="17" rx="2"/><path d="M8 2v4M16 2v4M8 11h8M8 15h5"/>'))
    links.append(link('plan', 'Мой план', home_url + '#panel',
                      '<path d="M8 3h8v3h4v15H4V6h4Zm0 0v5h8V3M8 12h8m-8 4h5"/>',
                      aria='Предложенное расписание'))
    if planning_url:
        links.append(link('planning', 'Пары и время', planning_url,
                          '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
                          aria='Пары и время для учёбы'))
    if google_calendar_enabled:
        links.append(link('calendar', 'Google Calendar', home_url + '#calendar',
                          '<rect x="4" y="5" width="16" height="16" rx="2"/><path d="M8 3v4m8-4v4M4 11h16"/>'))
    links.append(link('journal', 'Журнал агента', home_url + '#journal-heading',
                      '<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>'))
    account = ''
    if account_url:
        label = 'Настройки' if account_url == '/settings' else 'Моё подключение'
        account = (f'<a class="account-link{" current" if active == "settings" else ""}"'
                   f' href="{_e(account_url)}" aria-label="{_e(label)}" title="{_e(label)}"'
                   + (' aria-current="page"' if active == 'settings' else '') + '>'
                   + _icon('<circle cx="12" cy="8" r="3"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/>')
                   + f'<span class="nav-label">{_e(label)}</span></a>')
    return ('''<aside id="sidebar" class="sidebar" aria-label="Навигация Google AI">
<div class="sidebar-header"><button id="sidebar-toggle" type="button" aria-controls="sidebar" aria-expanded="true" aria-label="Свернуть боковую панель" title="Свернуть боковую панель">'''
            + _icon('<path d="M4 6h16M4 12h16M4 18h16"/>')
            + f'</button><a class="brand" href="{_e(home_url)}#workspace" aria-label="Google AI — к ассистенту"><span class="nav-label">Google AI</span></a></div>'
            + '<button id="chat-new" class="chat-new" type="button" aria-label="Новый чат" title="Новый чат" disabled>'
            + _icon('<path d="M13 5H5v14h14v-8M15 3l6 6M10 14l2-5 6-6 3 3-6 6Z"/>')
            + '<span class="nav-label">Новый чат</span></button><nav aria-label="Разделы расписания">'
            + ''.join(links) + '</nav>' + CHAT_NAV
            + '<div id="conversation-index" hidden></div><div class="sidebar-bottom">'
            + account + '<button id="signout" type="button" hidden aria-label="Выйти" title="Выйти">'
            + _icon('<path d="M9 5H5v14h4M10 12h10m-4-4 4 4-4 4"/>')
            + '<span class="nav-label">Выйти</span></button></div></aside>')


def wrap_page(html, *, active, title, moodle_enabled=True,
              planning_url='/planning', account_url='/settings',
              google_calendar_enabled=True):
    """Add the shared shell without rewriting forms, notices or page scripts.

    Some existing templates intentionally omit head/body tags. Normalize only
    that outer document structure; the original page content remains verbatim.
    """
    if 'data-shared-navigation="true"' in html:
        return html
    head_match = re.search(r'<head\b[^>]*>(.*?)</head\s*>', html, re.I | re.S)
    body_match = re.search(r'<body\b([^>]*)>(.*?)(?:</body\s*>|</html\s*>|$)', html, re.I | re.S)
    if head_match:
        head = head_match.group(1)
    else:
        prefix = re.sub(r'^.*?<html\b[^>]*>', '', html, count=1, flags=re.I | re.S)
        head = re.split(r'<(?:body|main|header)\b', prefix, maxsplit=1, flags=re.I)[0]
    if body_match:
        body_attrs, content = body_match.groups()
    else:
        body_attrs = ''
        content_start = re.search(r'<(?:main|header)\b', html, re.I)
        if content_start is None:
            raise ValueError('shared_navigation_page_content_missing')
        content = re.sub(r'</html\s*>\s*$', '', html[content_start.start():], flags=re.I)
    if re.search(r'<title\b', head, re.I):
        head = re.sub(r'<title\b[^>]*>.*?</title\s*>', lambda _: f'<title>{_e(title)} · Google AI</title>',
                      head, count=1, flags=re.I | re.S)
    else:
        head += f'<title>{_e(title)} · Google AI</title>'
    if '/assets/theme.js' not in head:
        head += '<script src="/assets/theme.js"></script>'
    # Shared appearance tokens stay last, as on the main workspace.
    theme_css = '<link rel="stylesheet" href="/assets/theme.css">'
    head = head.replace(theme_css, '')
    head += ('<script src="/assets/sidebar-head.js"></script>'
             '<link rel="stylesheet" href="/assets/campuspulse-navigation.css">'
             '<script src="/assets/campuspulse-navigation.js" defer></script>') + theme_css
    navigation = render_navigation(home_url='/', planning_url=planning_url,
                                   account_url=account_url, active=active,
                                   moodle_enabled=moodle_enabled,
                                   google_calendar_enabled=google_calendar_enabled)
    topbar = ('''<header class="topbar page-navigation-topbar"><button id="mobile-nav-toggle" type="button" aria-label="Открыть навигацию" aria-controls="sidebar" aria-expanded="false">'''
              + _icon('<path d="M4 6h16M4 12h16M4 18h16"/>')
              + f'</button><div class="view-heading"><span id="view-title">{_e(title)}</span></div></header>')
    return ('<!doctype html><html lang="ru"><head>' + head + '</head><body' + body_attrs
            + f' data-shared-navigation="true" data-view="{_e(active)}">'
            + '<a class="skip" href="#page-content">К содержимому</a>' + navigation
            + '<div class="app-content" id="page-content" tabindex="-1">' + topbar + content
            + '</div></body></html>')


def install_page_navigation(app):
    """Self-host shell assets once for every authenticated page composition."""
    if getattr(app.state, 'page_navigation_installed', False):
        return
    app.state.page_navigation_installed = True
    from fastapi.responses import Response
    from ..assets.page_navigation import SCRIPT, STYLES
    from ..assets.sidebar import HEAD_SCRIPT, SCRIPT as SIDEBAR_SCRIPT, STYLES as SIDEBAR_STYLES
    from ..assets.workspace import SCRIPT as WORKSPACE_SCRIPT, WORKSPACE_STYLES

    @app.get('/assets/campuspulse-navigation.css')
    async def navigation_style():
        return Response(STYLES + WORKSPACE_STYLES + SIDEBAR_STYLES, media_type='text/css')

    @app.get('/assets/campuspulse-navigation.js')
    async def navigation_script():
        return Response(WORKSPACE_SCRIPT + SCRIPT + SIDEBAR_SCRIPT, media_type='text/javascript')

    @app.get('/assets/sidebar-head.js')
    async def sidebar_head():
        return Response(HEAD_SCRIPT, media_type='text/javascript')
