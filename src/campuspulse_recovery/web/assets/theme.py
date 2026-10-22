"""Fixed dark appearance shared by public and authenticated pages."""
from fastapi.responses import Response

from .theme_styles import THEME_STYLES

THEME_HEAD = '<script src="/assets/theme.js"></script><link rel="stylesheet" href="/assets/theme.css">'

# Existing page compositions can keep their shared appearance slot.
THEME_BUTTON = ''

THEME_SCRIPT = r"""'use strict';
(() => {
  document.documentElement.dataset.theme = 'dark';
  const scheme = document.querySelector('meta[name="color-scheme"]');
  if (scheme) scheme.content = 'dark';
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
