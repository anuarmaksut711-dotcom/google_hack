"""Resizable desktop navigation; mobile retains the existing drawer."""

STYLES = r'''
#sidebar-toggle, #sidebar-resize { display: none; }
@media (min-width: 761px) {
  body { --cp-sidebar-width: min(328px, calc(35vw - 72px)); }
  body[data-sidebar-collapsed=true] { --cp-sidebar-width: 0px !important; }
  .sidebar { left: 72px; width: var(--cp-sidebar-width); max-width: calc(35vw - 72px); padding-inline: 18px; overflow-x: hidden; }
  .sidebar .nav-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .app-content { margin-left: calc(72px + var(--cp-sidebar-width)); }
  body[data-sidebar-collapsed=true] .sidebar { display: none; }
  .activity-rail #sidebar-toggle { display: grid; place-items: center; flex-shrink: 0; width: 44px; min-height: 36px; padding: 8px; border-radius: 10px; background: transparent; color: var(--muted); }
  .activity-rail #sidebar-toggle:hover { background: var(--selected); color: var(--ink); }
  #sidebar-toggle svg { width: 20px; height: 20px; }
  body[data-sidebar-collapsed=true] #sidebar-toggle svg { transform: rotate(180deg); }
  #sidebar-resize { position: fixed; display: block; inset: 0 auto 0 calc(72px + var(--cp-sidebar-width) - 5px); z-index: 8; width: 10px; cursor: col-resize; touch-action: none; }
  #sidebar-resize::after { content: ''; position: absolute; inset: 0 auto 0 4px; width: 2px; background: transparent; }
  #sidebar-resize:hover::after, #sidebar-resize:focus-visible::after,
  body[data-sidebar-resizing=true] #sidebar-resize::after { background: var(--focus); }
  #sidebar-resize:focus-visible { outline-offset: -3px; }
  body[data-sidebar-resizing=true] { user-select: none; cursor: col-resize; }
  body[data-sidebar-resizing=true] .sidebar, body[data-sidebar-resizing=true] .app-content { pointer-events: none; }
}
'''

SCRIPT = r'''
(() => {
  const sidebar = document.getElementById('sidebar');
  const rail = document.querySelector('.activity-rail');
  if (!sidebar || !rail) return;
  const desktop = window.matchMedia('(min-width: 761px)');
  const storageKey = 'campuspulse.sidebar.v1';
  const toggle = document.createElement('button');
  toggle.id = 'sidebar-toggle'; toggle.type = 'button';
  toggle.setAttribute('aria-controls', 'sidebar');
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('viewBox', '0 0 24 24'); icon.setAttribute('aria-hidden', 'true');
  const outline = document.createElementNS(icon.namespaceURI, 'rect');
  for (const [name, value] of Object.entries({x: 3, y: 4, width: 18, height: 16, rx: 3})) outline.setAttribute(name, value);
  const arrow = document.createElementNS(icon.namespaceURI, 'path');
  arrow.setAttribute('d', 'M9 4v16m7-11-3 3 3 3');
  icon.append(outline, arrow); toggle.append(icon);
  rail.prepend(toggle);
  const handle = document.createElement('div');
  handle.id = 'sidebar-resize'; handle.tabIndex = 0;
  handle.setAttribute('role', 'separator');
  handle.setAttribute('aria-label', 'Ширина боковой панели');
  handle.setAttribute('aria-orientation', 'vertical');
  handle.setAttribute('aria-controls', 'sidebar');
  handle.title = 'Потяните для изменения ширины. Стрелки — изменить, Home — свернуть, End — максимум';
  sidebar.after(handle);
  let preferredWidth = 328, collapsed = false, drag = null;
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (Number.isFinite(saved?.width) && saved.width > 0) preferredWidth = saved.width;
    collapsed = saved?.collapsed === true;
  } catch { /* Navigation works when storage is unavailable. */ }
  // The 35% budget includes the existing 72px activity rail.
  const maximum = () => Math.max(0, Math.floor(document.documentElement.clientWidth * .35) - 72);
  const minimum = () => Math.min(200, maximum());
  const clamp = value => Math.min(maximum(), Math.max(minimum(), value));
  const save = () => {
    try { localStorage.setItem(storageKey, JSON.stringify({width: preferredWidth, collapsed})); } catch {}
  };
  function render() {
    const closed = desktop.matches && collapsed;
    document.body.dataset.sidebarCollapsed = String(closed);
    document.body.style.setProperty('--cp-sidebar-width', (closed ? 0 : clamp(preferredWidth)) + 'px');
    sidebar.inert = closed;
    if (closed) sidebar.setAttribute('aria-hidden', 'true');
    else sidebar.removeAttribute('aria-hidden');
    toggle.setAttribute('aria-expanded', String(!closed));
    toggle.setAttribute('aria-label', closed ? 'Развернуть панель' : 'Свернуть панель');
    toggle.title = closed ? 'Развернуть панель' : 'Свернуть панель';
    handle.setAttribute('aria-valuemin', '0');
    handle.setAttribute('aria-valuemax', String(maximum()));
    handle.setAttribute('aria-valuenow', String(closed ? 0 : clamp(preferredWidth)));
    handle.setAttribute('aria-valuetext', closed ? 'Панель свёрнута' : Math.round(72 + clamp(preferredWidth)) + ' пикселей вместе с навигацией');
  }
  toggle.addEventListener('click', () => { collapsed = !collapsed; render(); save(); });
  function resizeTo(value) {
    collapsed = value < Math.min(120, minimum());
    if (!collapsed) preferredWidth = clamp(value);
    render();
  }
  handle.addEventListener('pointerdown', event => {
    if (!desktop.matches || !event.isPrimary || event.button !== 0) return;
    event.preventDefault(); handle.focus({preventScroll: true});
    drag = {id: event.pointerId, x: event.clientX, width: collapsed ? 0 : clamp(preferredWidth), preferredWidth, collapsed};
    handle.setPointerCapture(event.pointerId);
    document.body.dataset.sidebarResizing = 'true';
  });
  handle.addEventListener('pointermove', event => {
    if (drag?.id === event.pointerId) resizeTo(drag.width + event.clientX - drag.x);
  });
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
    else if (event.key === 'ArrowRight') {
      preferredWidth = collapsed ? minimum() : clamp(preferredWidth + (event.shiftKey ? 40 : 16)); collapsed = false;
    } else if (!collapsed) {
      const width = clamp(preferredWidth) - (event.shiftKey ? 40 : 16);
      if (width < minimum()) collapsed = true; else preferredWidth = width;
    }
    render(); save();
  });
  window.addEventListener('resize', () => { finish(true); render(); });
  desktop.addEventListener('change', () => { finish(true); render(); });
  render();
})();
'''
