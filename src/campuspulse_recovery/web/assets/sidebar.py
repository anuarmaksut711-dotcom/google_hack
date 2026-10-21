"""Shared animated navigation, including an accessible icon rail and mobile drawer."""

# Synchronous, self-hosted head script preserves the strict script-src policy.
HEAD_SCRIPT = r'''
(() => {
  try {
    const saved = JSON.parse(localStorage.getItem('campuspulse.sidebar.v1'));
    const root = document.documentElement;
    root.dataset.sidebarCollapsed = String(saved?.collapsed === true);
    if (Number.isFinite(saved?.width) && saved.width > 0) {
      const maximum = Math.max(240, Math.min(400, Math.floor(root.clientWidth * .35)));
      root.style.setProperty('--cp-sidebar-preferred', Math.min(maximum, Math.max(240, saved.width)) + 'px');
    }
  } catch { /* A blocked or invalid preference never prevents navigation. */ }
})();
'''

STYLES = r'''
body { --cp-sidebar-width: var(--cp-sidebar-preferred, 280px); --cp-sidebar-motion: 240ms; }
.sidebar { left: 0; width: var(--cp-sidebar-width); max-width: none; padding: 14px 12px 12px; border-radius: 0; overflow-x: hidden; overflow-y: auto; scrollbar-width: thin; }
.sidebar-header { display: flex; align-items: center; flex-shrink: 0; gap: 12px; min-height: 44px; margin-bottom: 20px; }
#sidebar-toggle { display: grid; place-items: center; flex: 0 0 44px; width: 44px; min-height: 44px; padding: 11px; border-radius: 50%; background: transparent; color: var(--muted); }
#sidebar-toggle:hover { background: var(--selected); color: var(--ink); }
#sidebar-toggle svg { width: 22px; height: 22px; }
.sidebar-header .brand { min-width: 0; min-height: 44px; margin: 0; font-size: 21px; white-space: nowrap; }
.sidebar .nav-label { display: block; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; opacity: 1; transform: translateX(0); transition: opacity 140ms ease-out 65ms, transform var(--cp-sidebar-motion) ease-out; }
.sidebar nav { flex-shrink: 0; gap: 4px; margin: 0; }
.sidebar .nav-link, .sidebar .chat-new, .sidebar .account-link, .sidebar #signout { min-height: 44px; justify-content: flex-start; gap: 12px; padding: 10px 12px; border-radius: 22px; font-size: 14px; white-space: nowrap; overflow: hidden; }
.sidebar .nav-link svg, .sidebar .chat-new svg, .sidebar .account-link svg, .sidebar #signout svg { width: 20px; height: 20px; flex: 0 0 20px; }
.sidebar .nav-link.current { background: var(--selected); color: var(--ink); font-weight: 500; }
.sidebar .chat-new { flex-shrink: 0; width: 100%; margin-bottom: 18px; }
.sidebar .chat-library { flex: 1 1 auto; min-height: 0; overflow-y: auto; overflow-x: hidden; margin-top: 24px; padding: 0; scrollbar-width: thin; opacity: 1; visibility: visible; transition: opacity 150ms ease-out 65ms, visibility 0s; }
.sidebar .sidebar-bottom { flex-shrink: 0; gap: 4px; margin-top: auto; padding-top: 12px; }
.sidebar .sidebar-expanded-only { opacity: 1; visibility: visible; transition: opacity 140ms ease-out 65ms, visibility 0s; }
#sidebar-resize { display: none; }
@media (min-width: 761px) {
  .sidebar { transition: width var(--cp-sidebar-motion) cubic-bezier(.2, 0, 0, 1); }
  .app-content { margin-left: var(--cp-sidebar-width); transition: margin-left var(--cp-sidebar-motion) cubic-bezier(.2, 0, 0, 1); }
  html[data-sidebar-collapsed=true] body, body[data-sidebar-collapsed=true] { --cp-sidebar-width: 72px !important; }
  body[data-sidebar-collapsed=true] .sidebar, html[data-sidebar-collapsed=true] .sidebar { scrollbar-width: none; }
  body[data-sidebar-collapsed=true] .sidebar .nav-label, html[data-sidebar-collapsed=true] .sidebar .nav-label { opacity: 0; transform: translateX(-8px); pointer-events: none; transition-delay: 0s; }
  body[data-sidebar-collapsed=true] .sidebar-header .brand, html[data-sidebar-collapsed=true] .sidebar-header .brand { visibility: hidden; pointer-events: none; }
  body[data-sidebar-collapsed=true] .sidebar .chat-library, html[data-sidebar-collapsed=true] .sidebar .chat-library,
  body[data-sidebar-collapsed=true] .sidebar .sidebar-expanded-only, html[data-sidebar-collapsed=true] .sidebar .sidebar-expanded-only { max-height: 0; margin-block: 0; padding-block: 0; opacity: 0; visibility: hidden; pointer-events: none; transition: opacity 100ms ease-out, visibility 0s 100ms; }
  #sidebar-resize { position: fixed; display: block; inset: 0 auto 0 calc(var(--cp-sidebar-width) - 5px); z-index: 8; width: 10px; cursor: col-resize; touch-action: none; transition: left var(--cp-sidebar-motion) cubic-bezier(.2, 0, 0, 1); }
  #sidebar-resize::after { content: ''; position: absolute; inset: 0 auto 0 4px; width: 2px; background: transparent; }
  #sidebar-resize:hover::after, #sidebar-resize:focus-visible::after { background: var(--muted); }
  #sidebar-resize:focus { outline: none; }
  #sidebar-resize:focus-visible { outline: 1px solid var(--muted); outline-offset: -4px; }
  body[data-sidebar-resizing=true] { user-select: none; cursor: col-resize; }
  body[data-sidebar-resizing=true] .sidebar, body[data-sidebar-resizing=true] .app-content { pointer-events: none; transition: none; }
  body[data-sidebar-resizing=true] #sidebar-resize { transition: none; }
}
@media (max-width: 760px) {
  .sidebar { z-index: 8; left: 0; width: min(300px, calc(100vw - 44px)); max-width: none; padding: 16px 12px 12px; transform: translateX(-100%); visibility: hidden; transition: transform var(--cp-sidebar-motion) cubic-bezier(.2, 0, 0, 1), visibility 0s var(--cp-sidebar-motion); }
  body[data-nav-open=true] .sidebar { transform: translateX(0); visibility: visible; transition-delay: 0s; box-shadow: 16px 0 40px #0006; }
  body[data-nav-open=true] { overflow: hidden; }
  .sidebar .chat-library { margin-top: 16px; }
  .sidebar-header { margin-bottom: 12px; }
  .sidebar-header .brand { font-size: 20px; }
  .app-content { margin-left: 0; margin-right: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .sidebar, .app-content, #sidebar-resize, .sidebar .nav-label, .sidebar .chat-library, .sidebar .sidebar-expanded-only { transition: none !important; }
}
'''

SCRIPT = r'''
(() => {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  const desktop = window.matchMedia('(min-width: 761px)');
  const storageKey = 'campuspulse.sidebar.v1';
  let toggle = document.getElementById('sidebar-toggle');
  if (!toggle) {
    toggle = document.createElement('button'); toggle.id = 'sidebar-toggle'; toggle.type = 'button';
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('viewBox', '0 0 24 24'); icon.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(icon.namespaceURI, 'path');
    path.setAttribute('d', 'M4 6h16M4 12h16M4 18h16'); icon.append(path); toggle.append(icon);
    (sidebar.querySelector('.sidebar-header') || sidebar).prepend(toggle);
  }
  toggle.setAttribute('aria-controls', 'sidebar');
  let handle = document.getElementById('sidebar-resize');
  if (!handle) { handle = document.createElement('div'); handle.id = 'sidebar-resize'; sidebar.after(handle); }
  handle.tabIndex = 0; handle.setAttribute('role', 'separator');
  handle.setAttribute('aria-label', 'Ширина боковой панели');
  handle.setAttribute('aria-orientation', 'vertical'); handle.setAttribute('aria-controls', 'sidebar');
  handle.title = 'Потяните для изменения ширины. Стрелки — изменить, Home — свернуть, End — максимум';
  const brand = sidebar.querySelector('.brand');
  const expandedOnly = sidebar.querySelectorAll('.chat-library, .sidebar-expanded-only');
  const mobileToggle = document.getElementById('mobile-nav-toggle');
  const ownsMobile = document.body.dataset.sharedNavigation === 'true';
  let preferredWidth = 280, collapsed = false, drag = null;
  const readSaved = value => {
    try {
      const saved = JSON.parse(value);
      preferredWidth = Number.isFinite(saved?.width) && saved.width > 0 ? saved.width : 280;
      collapsed = saved?.collapsed === true;
    } catch { preferredWidth = 280; collapsed = false; }
  };
  try { readSaved(localStorage.getItem(storageKey)); } catch {}
  const maximum = () => Math.max(240, Math.min(400, Math.floor(document.documentElement.clientWidth * .35)));
  const minimum = () => Math.min(240, maximum());
  const clamp = value => Math.min(maximum(), Math.max(minimum(), value));
  const save = () => { try { localStorage.setItem(storageKey, JSON.stringify({width: preferredWidth, collapsed})); } catch {} };
  function render() {
    const closed = desktop.matches && collapsed;
    const drawerClosed = !desktop.matches && document.body.dataset.navOpen !== 'true';
    document.body.dataset.sidebarCollapsed = String(closed);
    document.documentElement.dataset.sidebarCollapsed = String(closed);
    document.body.style.setProperty('--cp-sidebar-width', (closed ? 72 : clamp(preferredWidth)) + 'px');
    // Desktop icons stay interactive; concealed projects/history leave the tab order.
    if ((closed && [...expandedOnly].some(item => item.contains(document.activeElement))) ||
        (closed && brand?.contains(document.activeElement))) toggle.focus({preventScroll: true});
    for (const item of expandedOnly) {
      item.inert = closed;
      if (closed) item.setAttribute('aria-hidden', 'true'); else item.removeAttribute('aria-hidden');
    }
    if (brand) brand.inert = closed;
    sidebar.inert = drawerClosed;
    if (drawerClosed) sidebar.setAttribute('aria-hidden', 'true'); else sidebar.removeAttribute('aria-hidden');
    toggle.setAttribute('aria-expanded', String(desktop.matches ? !closed : !drawerClosed));
    toggle.setAttribute('aria-label', desktop.matches ? (closed ? 'Развернуть панель' : 'Свернуть панель') : 'Закрыть меню');
    toggle.title = toggle.getAttribute('aria-label');
    mobileToggle?.setAttribute('aria-expanded', String(!drawerClosed && !desktop.matches));
    handle.tabIndex = desktop.matches ? 0 : -1;
    handle.setAttribute('aria-valuemin', '72'); handle.setAttribute('aria-valuemax', String(maximum()));
    handle.setAttribute('aria-valuenow', String(closed ? 72 : clamp(preferredWidth)));
    handle.setAttribute('aria-valuetext', closed ? 'Панель свёрнута' : Math.round(clamp(preferredWidth)) + ' пикселей');
  }
  function mobileOpen(open, restoreFocus = false) {
    document.body.dataset.navOpen = String(open); render();
    if (open) toggle.focus({preventScroll: true});
    else if (restoreFocus) mobileToggle?.focus({preventScroll: true});
  }
  toggle.addEventListener('click', () => {
    if (!desktop.matches) { mobileOpen(false, true); return; }
    collapsed = !collapsed; render(); save();
  });
  function resizeTo(value) { collapsed = value < 156; if (!collapsed) preferredWidth = clamp(value); render(); }
  handle.addEventListener('pointerdown', event => {
    if (!desktop.matches || !event.isPrimary || event.button !== 0) return;
    event.preventDefault(); handle.focus({preventScroll: true});
    drag = {id: event.pointerId, x: event.clientX, width: collapsed ? 72 : clamp(preferredWidth), preferredWidth, collapsed};
    handle.setPointerCapture(event.pointerId); document.body.dataset.sidebarResizing = 'true';
  });
  handle.addEventListener('pointermove', event => { if (drag?.id === event.pointerId) resizeTo(drag.width + event.clientX - drag.x); });
  function finish(cancelled = false) {
    if (!drag) return;
    const previous = drag; drag = null;
    if (cancelled) { preferredWidth = previous.preferredWidth; collapsed = previous.collapsed; }
    delete document.body.dataset.sidebarResizing;
    if (handle.hasPointerCapture(previous.id)) handle.releasePointerCapture(previous.id);
    render(); if (!cancelled) save();
  }
  handle.addEventListener('pointerup', () => finish());
  handle.addEventListener('pointercancel', () => finish(true));
  handle.addEventListener('lostpointercapture', () => finish(true));
  handle.addEventListener('keydown', event => {
    if (event.key === 'Escape' && drag) { event.preventDefault(); finish(true); return; }
    if (!desktop.matches || !['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Home') collapsed = true;
    else if (event.key === 'End') { collapsed = false; preferredWidth = maximum(); }
    else if (event.key === 'Enter') collapsed = !collapsed;
    else if (event.key === 'ArrowRight') { preferredWidth = collapsed ? minimum() : clamp(preferredWidth + (event.shiftKey ? 40 : 16)); collapsed = false; }
    else if (!collapsed) {
      const width = clamp(preferredWidth) - (event.shiftKey ? 40 : 16);
      if (width < minimum()) collapsed = true; else preferredWidth = width;
    }
    render(); save();
  });
  window.addEventListener('resize', () => { finish(true); render(); });
  desktop.addEventListener('change', () => { finish(true); if (desktop.matches) document.body.dataset.navOpen = 'false'; render(); });
  window.addEventListener('storage', event => { if (event.key === storageKey) { finish(true); readSaved(event.newValue); render(); } });
  // The main chat owns its view-navigation lifecycle; standalone pages use this
  // controller so they do not inherit chat-specific element assumptions.
  if (ownsMobile) {
    mobileToggle?.addEventListener('click', () => mobileOpen(document.body.dataset.navOpen !== 'true'));
    document.addEventListener('click', event => {
      if (desktop.matches || document.body.dataset.navOpen !== 'true') return;
      if (event.target.closest('.nav-link, .chat-link')) mobileOpen(false);
      else if (!event.target.closest('#sidebar, #mobile-nav-toggle')) mobileOpen(false);
    });
    document.addEventListener('keydown', event => {
      if (desktop.matches || document.body.dataset.navOpen !== 'true' || document.querySelector('dialog[open], [popover]:popover-open')) return;
      if (event.key === 'Escape') { event.preventDefault(); mobileOpen(false, true); }
      if (event.key === 'Tab') {
        const focusable = [...sidebar.querySelectorAll('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]')]
          .filter(item => !item.closest('[hidden], [inert]') && item.getClientRects().length);
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || !sidebar.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || !sidebar.contains(document.activeElement))) { event.preventDefault(); first?.focus(); }
      }
    });
  }
  new MutationObserver(render).observe(document.body, {attributes: true, attributeFilter: ['data-nav-open']});
  render();
})();
'''
