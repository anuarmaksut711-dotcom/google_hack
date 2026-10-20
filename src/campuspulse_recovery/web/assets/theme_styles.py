"""Shared theme tokens and light variants for the existing application."""

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
:root[data-theme=light] {
  color-scheme: light;
  --surround: #f7f8fb; --surface: #ffffff; --ink: #202433;
  --muted: #5d6576; --line: #d7dce6; --subtle: #eef1f7;
  --accent: #335ab8; --selected: #e2e8f5; --focus: #365dcc;
  --action: #3559cd; --action-hover: #294bb7;
  --theme-border-strong: #8591a7; --theme-disabled: #697184;
  --theme-disabled-surface: #e4e7ee; --theme-error: #9f2938;
  --theme-error-surface: #fff0f1; --theme-warning: #805713;
  --theme-warning-surface: #fff6e3; --theme-success: #246646;
  --theme-success-surface: #eaf6ef;
}
.topbar-actions, .settings-actions {
  display: flex; align-items: center; justify-content: flex-end; gap: 10px;
}
.topbar-actions { flex-shrink: 0; margin-left: auto; }
.settings-actions { flex-wrap: wrap; }
.theme-toggle {
  display: inline-flex; align-items: center; justify-content: center; gap: 9px;
  box-sizing: border-box; flex-shrink: 0; min-width: 44px; min-height: 44px;
  margin: 0; padding: 10px 13px; border: 1px solid var(--line);
  border-radius: 12px; background: var(--surface); color: var(--ink);
  font: 500 13px/1.5 'Segoe UI', system-ui, sans-serif; cursor: pointer;
  transition: background-color 160ms ease-out, border-color 160ms ease-out;
}
.theme-toggle:hover { background: var(--subtle); border-color: var(--theme-border-strong); }
.theme-toggle:active { background: var(--selected); }
.theme-toggle:focus-visible { outline: 2px solid var(--focus); outline-offset: 4px; }
.theme-toggle [data-theme-label] { white-space: nowrap; }
.theme-toggle [data-theme-icon] { display: inline-flex; align-items: center; justify-content: center; }
.theme-toggle svg {
  display: block; width: 19px; height: 19px; fill: none; stroke: currentColor;
  stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round;
}
.theme-toggle [hidden] { display: none !important; }
body.auth-page { position: relative; }
.auth-theme { position: absolute; top: 20px; right: 24px; z-index: 2; }
html[data-theme=light] { scrollbar-color: #a4adbe transparent; }
html[data-theme=light] ::selection { background: #d7e2ff; color: #15294e; }
html[data-theme=light] body { color: var(--ink); }
html[data-theme=light] .sidebar,
html[data-theme=light] .settings-header { background: #f0f2f7; }
html[data-theme=light] .activity-rail { background: #e9edf4; }
html[data-theme=light] .app-content,
html[data-theme=light] .topbar { background: var(--surround); }
html[data-theme=light] body[data-view=assistant] .app-content {
  background: radial-gradient(ellipse at 50% 46%, #e3eaff 0%, #f0f3fc 35%, #f7f8fb 72%);
}
html[data-theme=light] body[data-view=assistant]:has(#recovery-message:not(:placeholder-shown)) .app-content,
html[data-theme=light] body[data-view=assistant]:has(#workspace.has-chat) .app-content {
  background: var(--surround);
}
html[data-theme=light] .nav-link,
html[data-theme=light] .activity-rail a,
html[data-theme=light] .conversation-entry,
html[data-theme=light] .prompt-chip,
html[data-theme=light] .chat-help,
html[data-theme=light] .composer-context a { color: var(--muted); }
html[data-theme=light] .nav-link:hover { background: var(--selected); color: var(--ink); }
html[data-theme=light] .nav-link.current,
html[data-theme=light] .sidebar .nav-link:hover,
html[data-theme=light] .write-link:hover,
html[data-theme=light] .composer-context a:hover { color: var(--ink); }
html[data-theme=light] .activity-rail a:hover,
html[data-theme=light] .activity-rail a.current { background: #d9e2f4; color: #28498b; }
html[data-theme=light] .activity-rail .rail-brand { color: var(--accent); }
html[data-theme=light] .sidebar-project a,
html[data-theme=light] .sidebar-bottom footer details[open] { background: var(--selected); }
html[data-theme=light] .sidebar-bottom { border-color: var(--line); }
html[data-theme=light] .command-bar {
  border-color: var(--line); background: var(--surface);
  box-shadow: 0 18px 50px -24px #22345b26;
}
html[data-theme=light] .command-bar:focus-within { border-color: var(--focus); }
html[data-theme=light] .prompt-chip svg { color: var(--muted); }
html[data-theme=light] .prompt-chip:hover { border-color: var(--theme-border-strong); }
html[data-theme=light] .composer-options .prompt-suggestions,
html[data-theme=light] .chat-help #chat-status {
  background: var(--surface); box-shadow: 0 8px 28px #22345b1f;
}
html[data-theme=light] .composer-options .prompt-chip { color: var(--ink); }
html[data-theme=light] .composer-options .prompt-chip:hover { background: var(--subtle); }
html[data-theme=light] .consent-note,
html[data-theme=light] body[data-view=assistant] footer { color: var(--muted); }
html[data-theme=light] .chat-user { background: #e7edf9; }
html[data-theme=light] .chat-system > p,
html[data-theme=light] .chat-system > .chat-answer { color: var(--theme-warning); }
html[data-theme=light] button:disabled {
  background: var(--theme-disabled-surface); color: var(--theme-disabled);
}
html[data-theme=light] .sidebar .chat-new:disabled,
html[data-theme=light] .chat-library button:disabled { background: transparent; }
html[data-theme=light] #voice-toggle[aria-pressed=true] { background: #a92d35; color: #ffffff; }
html[data-theme=light] .demo-notice { color: var(--theme-warning); }
html[data-theme=light] .demo-notice p { background: var(--theme-warning-surface); }
html[data-theme=light] #feedback[data-error=true] {
  background: var(--theme-error-surface); color: var(--theme-error);
}
html[data-theme=light] .outcome { background: #eaf0fa; }
html[data-theme=light] .outcome[data-status=verified] { background: var(--theme-success-surface); }
html[data-theme=light] .outcome[data-status=verified] h2 { color: var(--theme-success); }
html[data-theme=light] .outcome[data-status=unknown],
html[data-theme=light] .outcome[data-status=partial],
html[data-theme=light] .outcome[data-status=applied_unverified],
html[data-theme=light] .outcome[data-status=needs_input],
html[data-theme=light] .outcome[data-status=stale] { background: var(--theme-warning-surface); }
html[data-theme=light] .outcome[data-status=unknown] h2,
html[data-theme=light] .outcome[data-status=partial] h2,
html[data-theme=light] .outcome[data-status=applied_unverified] h2,
html[data-theme=light] .outcome[data-status=needs_input] h2,
html[data-theme=light] .outcome[data-status=stale] h2 { color: var(--theme-warning); }
html[data-theme=light] .outcome[data-status=verification_failed],
html[data-theme=light] .outcome[data-status=insufficient_capacity] { background: var(--theme-error-surface); }
html[data-theme=light] .outcome[data-status=verification_failed] h2,
html[data-theme=light] .outcome[data-status=insufficient_capacity] h2 { color: var(--theme-error); }
html[data-theme=light] .moodle-task-list button,
html[data-theme=light] #trace-records pre { color: var(--ink); }
html[data-theme=light] .chat-action-menu,
html[data-theme=light] .chat-edit-dialog {
  background: var(--surface); border: 1px solid var(--line);
  box-shadow: 0 16px 48px #22345b26;
}
html[data-theme=light] .chat-action-menu button:hover,
html[data-theme=light] .chat-action-menu button:focus-visible,
html[data-theme=light] .chat-dialog-actions button:hover { background: var(--subtle); }
html[data-theme=light] .chat-edit-dialog::backdrop { background: #202c4659; }
html[data-theme=light] .chat-edit-dialog input,
html[data-theme=light] .chat-edit-dialog select { color-scheme: light; background: var(--surface); }
html[data-theme=light] .chat-dialog-actions #chat-edit-save { background: var(--action); color: #ffffff; }
html[data-theme=light] .chat-dialog-actions #chat-edit-save:hover { background: var(--action-hover); }
html[data-theme=light] .chat-dialog-actions #chat-edit-save:disabled {
  background: var(--theme-disabled-surface); color: var(--theme-disabled);
}
html[data-theme=light] #chat-library-status[data-error=true] { color: var(--theme-error); }
html[data-theme=light] .settings-card { background: var(--surface); }
html[data-theme=light] .settings-card-icon { border-color: var(--line); }
html[data-theme=light] .settings-card[data-connected=true] #moodle-connection-status::before {
  background: var(--theme-success);
}
html[data-theme=light] .settings-card details { background: #f8f9fc; border-color: var(--line); }
html[data-theme=light] .settings-card input {
  background: var(--surface); border-color: var(--theme-border-strong); color-scheme: light;
}
html[data-theme=light] .settings-card input:hover { border-color: #63728c; }
html[data-theme=light] .settings-card input:focus-visible { border-color: var(--focus); }
html[data-theme=light] .settings-card #account-status:not(:empty) {
  background: #edf2ff; border-color: #a2b5df; color: #28478a;
}
html[data-theme=light] body.auth-page {
  --auth-ground: #f7f8fb; --auth-ink: #202433; --auth-muted: #5d6576; --auth-focus: #365dcc;
  color-scheme: light; scrollbar-color: #a4adbe var(--auth-ground);
}
html[data-theme=light] .auth-page ::selection { background: #d7e2ff; color: #15294e; }
html[data-theme=light] .auth-page .auth-card {
  background: var(--surface); border: 1px solid var(--line); box-shadow: 0 24px 64px -28px #22345b33;
}
html[data-theme=light] .auth-page .auth-title { color: var(--auth-ink); }
html[data-theme=light] .auth-page .auth-field {
  background: #eef1f7; color: var(--auth-ink); caret-color: var(--accent); border-color: #d7dce6;
}
html[data-theme=light] .auth-page .auth-field::placeholder { color: var(--auth-muted); }
html[data-theme=light] .auth-page .auth-field:hover { background: #e8edf6; }
html[data-theme=light] .auth-page .auth-field:user-invalid,
html[data-theme=light] .auth-page .auth-field[aria-invalid=true] { border-color: #b94551; }
html[data-theme=light] .auth-page .auth-field-error {
  background: var(--theme-error-surface); border-color: #e6b1b9; color: var(--theme-error);
}
html[data-theme=light] .auth-page .auth-field:-webkit-autofill {
  -webkit-text-fill-color: var(--auth-ink); box-shadow: 0 0 0 100px #eef1f7 inset;
}
html[data-theme=light] .auth-page #email-submit { background: var(--action); color: #ffffff; }
html[data-theme=light] .auth-page #email-submit:hover { background: var(--action-hover); }
html[data-theme=light] .auth-page #email-submit:active { background: #23429f; }
html[data-theme=light] .auth-page #email-submit:disabled {
  background: var(--theme-disabled-surface); color: var(--theme-disabled); opacity: 1;
}
html[data-theme=light] .auth-page #login-loading {
  background: var(--subtle); color: var(--auth-muted);
}
html[data-theme=light] .auth-page #login-status[data-state=error] { color: var(--theme-error); }
html[data-theme=light] .auth-page #login-retry { background: var(--selected); color: var(--auth-ink); }
html[data-theme=light] .auth-page #login-retry:hover { background: #d7e0f2; }
html[data-theme=light] .auth-page #login-retry:active { background: #cbd7ef; }
html[data-theme=light] .auth-page a { color: var(--accent); }
html[data-theme=light] .auth-page a:hover { color: #23429f; }
@media (max-width: 600px) {
  .theme-toggle { width: 44px; height: 44px; padding: 10px; }
  .theme-toggle [data-theme-label] { display: none; }
  .topbar-actions, .settings-actions { gap: 8px; }
  .auth-theme { top: 16px; right: 16px; }
  body.auth-page { padding-top: 84px; }
}
@media (max-height: 800px) and (min-width: 601px) {
  body.auth-page { padding-top: 84px; }
}
@media (prefers-reduced-motion: reduce) {
  .theme-toggle { transition: none; }
}
"""
