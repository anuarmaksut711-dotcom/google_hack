"""Shared browser appearance preference for public and authenticated pages."""
from fastapi.responses import Response

from .theme_styles import THEME_STYLES

THEME_HEAD = '<script src="/assets/theme.js"></script><link rel="stylesheet" href="/assets/theme.css">'

THEME_BUTTON = '''<button class="theme-toggle" type="button" data-theme-toggle aria-label="Включить светлую тему" title="Включить светлую тему">
<svg data-theme-icon="light" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></svg>
<svg data-theme-icon="dark" viewBox="0 0 24 24" aria-hidden="true" hidden><path d="M20.5 14.2A8.7 8.7 0 0 1 9.8 3.5a8.7 8.7 0 1 0 10.7 10.7Z"/></svg>
<span data-theme-label>Светлая тема</span></button>'''

THEME_SCRIPT = r"""'use strict';
(() => {
  const preferenceKey = 'campuspulse-theme';
  let current = 'dark';
  try {
    const saved = localStorage.getItem(preferenceKey);
    if (saved === 'light' || saved === 'dark') current = saved;
  } catch {}
  function apply(theme) {
    current = theme;
    document.documentElement.dataset.theme = theme;
    const scheme = document.querySelector('meta[name="color-scheme"]');
    if (scheme) scheme.content = theme;
    const next = theme === 'dark' ? 'light' : 'dark';
    const label = next === 'light' ? 'Светлая тема' : 'Тёмная тема';
    const action = next === 'light' ? 'Включить светлую тему' : 'Включить тёмную тему';
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      button.setAttribute('aria-label', action);
      button.title = action;
      button.querySelector('[data-theme-label]').textContent = label;
      button.querySelectorAll('[data-theme-icon]').forEach(icon => {
        icon.toggleAttribute('hidden', icon.dataset.themeIcon !== next);
      });
    });
  }
  apply(current);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => apply(current), {once: true});
  }
  document.addEventListener('click', event => {
    const button = event.target.closest?.('[data-theme-toggle]');
    if (!button || button.disabled) return;
    apply(current === 'dark' ? 'light' : 'dark');
    try { localStorage.setItem(preferenceKey, current); } catch {}
  });
  window.addEventListener('storage', event => {
    if (event.key !== preferenceKey && event.key !== null) return;
    apply(event.newValue === 'light' ? 'light' : 'dark');
  });
})();
"""


def install_theme(app):
    if getattr(app.state, 'theme_assets_installed', False):
        return
    app.state.theme_assets_installed = True

    @app.get('/assets/theme.js')
    async def script():
        return Response(THEME_SCRIPT, media_type='text/javascript')

    @app.get('/assets/theme.css')
    async def stylesheet():
        return Response(THEME_STYLES, media_type='text/css')
