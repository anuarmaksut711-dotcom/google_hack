"""Shared dark tokens and action layout for the existing application."""

THEME_STYLES = r"""
:root {
  color-scheme: dark;
  --surround: #101010; --surface: #151619; --ink: #e3e4e9;
  --muted: #a2a5af; --line: #2b2d33; --subtle: #1c1e23;
  --accent: #a6bcff; --selected: #272a32; --focus: #91acff;
  --action: #3559cd; --action-hover: #456be2;
  --theme-border-strong: #53607b; --theme-disabled: #82858f;
  --theme-disabled-surface: #282b33; --theme-error: #f2b1b3;
  --theme-error-surface: #2b1b20; --theme-warning: #e7cb90;
  --theme-warning-surface: #292419; --theme-success: #9cdab3;
  --theme-success-surface: #172a22;
}
.topbar-actions, .settings-actions {
  display: flex; align-items: center; justify-content: flex-end; gap: 10px;
}
.topbar-actions { flex-shrink: 0; margin-left: auto; }
.settings-actions { flex-wrap: wrap; }
@media (max-width: 600px) {
  .topbar-actions, .settings-actions { gap: 8px; }
}
"""
