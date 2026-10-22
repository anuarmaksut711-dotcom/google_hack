"""Navigation-only bootstrap for signed-in pages without the agent chat client."""

STYLES = r'''
body[data-shared-navigation=true] { margin: 0; padding: 0; background: var(--surround); color: var(--ink); font-family: 'Segoe UI', system-ui, sans-serif; }
body[data-shared-navigation=true] [hidden] { display: none !important; }
.sidebar { position: fixed; inset: 0 auto 0 0; z-index: 6; display: flex; flex-direction: column; padding: 16px 12px 12px; background: var(--surface); color: var(--ink); border-right: 1px solid var(--line); font: 400 14px/1.65 'Segoe UI', system-ui, sans-serif; }
.sidebar, .sidebar * { box-sizing: border-box; }
.sidebar svg, .page-navigation-topbar svg { fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; flex-shrink: 0; width: 20px; height: 20px; }
.sidebar button { display: inline-flex; align-items: center; justify-content: center; gap: 8px; margin: 0; width: auto; min-height: 42px; padding: 10px 12px; border: 0; border-radius: 12px; background: transparent; color: var(--ink); font: inherit; font-size: 13px; cursor: pointer; }
.sidebar button:hover { background: var(--selected); filter: none; }
.sidebar button:disabled { cursor: default; opacity: .5; }
.sidebar .brand { display: flex; align-items: center; min-height: 36px; margin: 0; color: var(--ink); text-decoration: none; font-size: 22px; font-weight: 600; letter-spacing: -.025em; }
.sidebar nav { display: grid; gap: 4px; }
.nav-link, .account-link, #signout { display: flex; align-items: center; justify-content: flex-start; width: 100%; gap: 12px; min-height: 44px; padding: 10px 12px; border-radius: 12px; text-decoration: none; font-size: 13px; line-height: 1.5; color: var(--muted); }
.nav-link:hover, .account-link:hover { background: var(--subtle); color: var(--ink); }
.nav-link.current, .account-link.current { color: var(--ink); background: var(--selected); font-weight: 600; }
.sidebar-bottom { display: grid; gap: 4px; margin-top: auto; padding-top: 12px; border-top: 1px solid var(--line); }
.app-content { position: relative; min-height: 100svh; background: var(--surround); }
.page-navigation-topbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 72px; padding: 14px 28px; color: var(--ink); }
.page-navigation-topbar #view-title { color: var(--ink); font-size: 14px; font-weight: 600; }
.page-navigation-topbar button { display: inline-flex; align-items: center; justify-content: center; width: 40px; min-height: 40px; margin: 0; padding: 9px; border: 0; border-radius: 12px; color: var(--muted); background: transparent; cursor: pointer; }
.page-navigation-topbar button:hover { background: var(--selected); }
.page-navigation-topbar #mobile-nav-toggle { display: none; }
body[data-shared-navigation=true] .skip { position: fixed; left: 12px; top: -100px; z-index: 50; padding: 10px 16px; background: var(--selected); color: var(--ink); }
body[data-shared-navigation=true] .skip:focus { top: 10px; }
.sidebar :focus-visible, .page-navigation-topbar :focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }
body[data-shared-navigation=true] .planning-page > section,
body[data-shared-navigation=true] .planning-page > form { background: var(--surface); }
body[data-shared-navigation=true] .planning-page input,
body[data-shared-navigation=true] .planning-page select { background: var(--surround); color: var(--ink); border-color: var(--line); }
body[data-shared-navigation=true] .planning-page button { background: var(--subtle); color: var(--ink); border-color: var(--line); }
body[data-shared-navigation=true] .planning-page button[type=submit],
body[data-shared-navigation=true] .planning-page #classes-import { background: var(--action); border-color: var(--action); color: #fff; }
body[data-shared-navigation=true] .planning-page :focus-visible { outline-color: var(--focus); }
body[data-shared-navigation=true][data-view=calendar] main { box-sizing: border-box; width: 100%; max-width: 1000px; margin: 0 auto; padding: 32px 28px 64px; border-radius: 0; background: var(--surround); }
body[data-shared-navigation=true][data-view=calendar] main a { color: var(--accent); }
body[data-shared-navigation=true][data-view=calendar] main button { min-height: 44px; border: 1px solid var(--line); border-radius: 10px; background: var(--subtle); color: var(--ink); }
body[data-shared-navigation=true][data-view=calendar] main button:disabled { color: var(--muted); opacity: .6; cursor: default; }
@media (max-width: 760px) {
  .page-navigation-topbar { min-height: 64px; padding: 12px 16px; }
  .page-navigation-topbar #mobile-nav-toggle { display: inline-flex; }
  body[data-shared-navigation=true][data-view=calendar] main { padding: 20px 16px 48px; }
}
'''

SCRIPT = r'''
'use strict';
(() => {
  if (document.body.dataset.sharedNavigation !== 'true') return;
  const $ = id => document.getElementById(id);
  if (!$('sidebar') || !window.CampusPulseChats) return;
  let scope = null, loading = false, signingOut = false;
  const errorFor = status => Object.assign(new Error('Навигация сейчас недоступна. Повторите попытку.'), {status});
  const request = async (path, options = {}) => {
    const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      signal: AbortSignal.timeout(15000), ...options});
    if (!response.ok) throw errorFor(response.status);
    return response;
  };
  async function csrf() {
    const expected = scope;
    for (let attempt = 0; attempt < 2; ++attempt) {
      let response;
      try { response = await request('/auth/session', {headers: {'X-CSRF-Bootstrap': '1'}}); }
      catch (error) { if (error.status === 409 && attempt === 0) continue; throw error; }
      const session = await response.json();
      if (!expected || scope !== expected || session.session_id !== expected) throw errorFor(403);
      if (typeof session.csrf_token !== 'string' || !/^[A-Za-z0-9_-]{20,256}$/.test(session.csrf_token)) throw errorFor(403);
      return session.csrf_token;
    }
    throw errorFor(409);
  }
  async function api(path, payload) {
    if (!['/auth/session', '/api/chat/workspace', '/auth/signout'].includes(path)) throw errorFor(403);
    const expected = scope;
    try {
      const options = {};
      if (payload !== undefined) {
        const token = await csrf();
        Object.assign(options, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token},
          body: JSON.stringify(payload)});
      }
      const response = await request(path, options);
      if (path === '/api/chat/workspace') {
        // Another tab may change the signed-in account while a list/change is
        // in flight. Publish neither old titles nor mutation results afterward.
        let current, data;
        try {
          data = await response.json();
          current = await (await request('/auth/session')).json();
        }
        catch (error) { invalidate(); throw error; }
        if (!expected || expected !== scope || current.status !== 'authenticated' || current.session_id !== expected)
          throw errorFor(403);
        return {json: async () => data};
      }
      return response;
    } catch (error) {
      if (error.status === 403) invalidate();
      throw error;
    }
  }
  const chatHash = id => id === 'default' ? '#workspace' : id === 'draft' ? '#new'
    : /^draft-[a-f0-9]{32}$/.test(id) ? '#new/' + id.slice(6)
    : /^[a-f0-9]{32}$/.test(id) ? '#chat/' + id : null;
  class PageChats extends window.CampusPulseChats {
    render() {
      super.render();
      this.$('chat-library').querySelectorAll('.chat-link').forEach(link => {
        const hash = chatHash(link.dataset.chatId);
        if (hash) link.href = '/' + hash;
      });
    }
  }
  const chats = new PageChats({api, selected: () => 'page', session: () => scope,
    navigate: id => { const hash = chatHash(id); if (hash) location.assign('/' + hash); },
    changed: () => {}, removed: () => {}, blocked: () => !scope || loading || signingOut});
  function invalidate() {
    scope = null; chats.reset(); $('signout').hidden = true; $('chat-library').hidden = false;
    chats.message('Не удалось проверить вход. Обновите страницу или войдите в приложение заново.', true);
    chats.controls();
  }
  async function load() {
    if (loading || signingOut || chats.mutating) return;
    loading = true; chats.controls();
    try {
      const session = await (await api('/auth/session')).json();
      if (session.status !== 'authenticated' || typeof session.session_id !== 'string' ||
          !/^[A-Za-z0-9_-]{1,256}$/.test(session.session_id)) throw errorFor(403);
      if (scope && scope !== session.session_id) chats.reset();
      scope = session.session_id;
      $('signout').hidden = false;
      await chats.load();
    } catch {
      scope = null; chats.reset(); $('signout').hidden = true; $('chat-library').hidden = false;
      chats.message('Не удалось загрузить чаты. Обновите страницу или войдите в приложение заново.', true);
    } finally { loading = false; chats.controls(); }
  }
  $('signout').addEventListener('click', async () => {
    if (!scope || loading || signingOut || chats.mutating) return;
    signingOut = true; $('signout').disabled = true; chats.controls();
    try {
      await api('/auth/signout', {});
      const draftPrefix = 'campuspulse-draft-v1:' + scope + ':';
      try { for (const key of Object.keys(sessionStorage)) if (key.startsWith(draftPrefix)) sessionStorage.removeItem(key); } catch {}
      scope = null; chats.reset(); location.assign('/login');
    } catch { chats.message('Выход не подтверждён. Повторите попытку.', true); }
    finally { signingOut = false; $('signout').disabled = false; chats.controls(); }
  });
  load();
})();
'''
