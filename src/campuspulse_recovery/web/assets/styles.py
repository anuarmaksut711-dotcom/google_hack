"""Self-hosted CampusPulse workspace; no external assets or build step."""
STYLES = r"""
:root {
  color-scheme: light;
  --surround: #eef3f8; --surface: #fff; --ink: #24272d;
  --muted: #646d79; --line: #e5e9ee; --subtle: #f6f8fa;
  --accent: #326ce5; --selected: #e5edf9; --focus: #3066d4;
  font-family: 'Segoe UI', system-ui, sans-serif;
  font-synthesis: none; background: var(--surround); color: var(--ink);
}
* { box-sizing: border-box; }
html { scroll-padding-top: 28px; scrollbar-color: #cbd2db transparent; }
body { margin: 0; font-size: 15px; line-height: 1.6; }
::selection { background: #dbe8ff; color: #203e75; }
a { color: #315faf; text-underline-offset: 4px; }
svg { fill: none; stroke: currentColor; stroke-width: 1.65; stroke-linecap: round; stroke-linejoin: round; flex-shrink: 0; }
.sidebar { position: fixed; inset: 0 auto 0 0; width: 72px; display: flex; flex-direction: column; align-items: center; padding: 24px 10px; z-index: 3; }
.brand { width: 44px; height: 44px; display: grid; place-items: center; color: #303b49; border-radius: 12px; text-decoration: none; }
.brand:hover { background: #dfe7f1; }
.brand-mark { width: 30px; height: 30px; stroke-width: 1.8; }
nav { display: grid; gap: 14px; margin-top: 30px; }
.nav-link { position: relative; display: grid; place-items: center; width: 44px; height: 44px; color: #75808d; border-radius: 12px; text-decoration: none; }
.nav-link svg { width: 22px; height: 22px; }
.nav-link:hover { color: #29394f; background: #e3eaf2; }
.nav-link.current { color: #315fae; background: #dfe8f6; }
.nav-label { position: absolute; left: 55px; padding: 7px 12px; border-radius: 8px; white-space: nowrap; background: #29323e; color: #fff; font-size: 12px; visibility: hidden; opacity: 0; pointer-events: none; }
.nav-link:hover .nav-label, .nav-link:focus-visible .nav-label { visibility: visible; opacity: 1; }
.nav-short { display: none; }
.sidebar-bottom { margin-top: auto; }
.app-content { margin: 12px 12px 12px 72px; min-height: calc(100svh - 24px); border-radius: 20px; background: var(--surface); border: 1px solid #e2e8f0; }
.topbar { min-height: 64px; display: flex; align-items: center; gap: 14px; padding: 12px 28px; font-size: 13px; color: var(--muted); }
.wordmark { color: #303844; font-size: 15px; font-weight: 650; letter-spacing: -.025em; text-decoration: none; }
.topbar-divider { color: #b6bfca; }
.topbar-results { margin-left: auto; display: flex; gap: 6px; align-items: center; color: var(--muted); text-decoration: none; padding: 6px 8px; border-radius: 6px; }
.topbar-results svg { width: 15px; height: 15px; }
.topbar-results:hover { background: var(--subtle); color: var(--ink); }
main { max-width: 1120px; margin: auto; padding: 0 40px 24px; }
.recovery-home { min-height: calc(100svh - 110px); display: flex; flex-direction: column; padding: 4px 0 30px; border: 0; margin: 0 0 36px; }
.demo-notice { align-self: center; max-width: 850px; width: 100%; color: #6d6659; font-size: 12px; }
.demo-notice summary { width: fit-content; padding: 7px 10px; border-radius: 7px; background: #f6f4ef; }
.demo-notice p { padding: 12px; background: #f8f7f4; border-radius: 8px; margin-top: 8px; }
summary { cursor: pointer; }
.intro { flex: 1; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; padding: 56px 0 76px; }
.welcome-logo { position: relative; width: min(440px, 100%); aspect-ratio: 4 / 3; transition: opacity 180ms ease-out; }
.welcome-logo canvas { display: block; width: 100%; height: 100%; }
.welcome-logo[data-state=loading] canvas { visibility: hidden; }
.welcome-logo img { position: absolute; top: 50%; left: 50%; width: 56%; height: auto; transform: translate(-50%, -50%); }
.welcome-logo.is-dismissed { opacity: 0; visibility: hidden; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
h1 { font-size: 36px; line-height: 1.25; font-weight: 450; letter-spacing: -.028em; text-wrap: balance; margin: 0 0 16px; }
h2 { font-size: 20px; line-height: 1.4; font-weight: 600; letter-spacing: -.02em; margin: 0 0 12px; text-wrap: balance; }
p { margin: 8px 0 16px; }
.intro > p { max-width: 62ch; font-size: 14px; line-height: 1.8; color: var(--muted); margin: 0; }
.command-bar { align-self: center; width: 100%; max-width: 850px; border-radius: 16px; background: #fff; box-shadow: 0 5px 30px -9px #253d5b29; }
.context-strip { display: flex; justify-content: space-between; gap: 16px; align-items: center; padding: 12px 20px; background: #f5f6f8; border-radius: 16px 16px 0 0; font-size: 12px; color: var(--muted); }
.context-strip a { display: flex; align-items: center; gap: 8px; color: #4a5360; text-decoration: none; }
.context-strip a:hover { color: var(--accent); }
.context-strip svg { width: 17px; height: 17px; }
.command-body { padding: 18px 20px 6px; }
.message-label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 8px; }
#recovery-message { display: block; width: 100%; min-height: 64px; max-height: 180px; padding: 0; resize: vertical; border: 0; border-radius: 2px; background: transparent; color: var(--ink); font: inherit; font-size: 16px; line-height: 1.6; caret-color: var(--accent); }
#recovery-message::placeholder { color: var(--muted); opacity: 1; }
#recovery-message:focus-visible { outline: 2px solid var(--focus); outline-offset: 5px; }
#message-hint { margin: 6px 0 20px; font-size: 11px; color: var(--muted); line-height: 1.6; }
.chat-submit { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 12px; }
.chat-submit #message-hint { margin: 0; }
.voice-input { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; margin-bottom: 12px; }
#voice-status { flex: 1 1 200px; margin: 0; color: var(--muted); font-size: 12px; line-height: 1.6; }
#voice-toggle[aria-pressed="true"] { background: #a92d35; }
.voice-language, #voice-level-wrap { font-size: 12px; color: var(--muted); }
#voice-language { font: inherit; min-height: 44px; max-width: 100%; padding: 8px; border: 1px solid var(--line); border-radius: 8px; background: #fff; color: #292d34; }
#voice-level { width: 100px; vertical-align: middle; }
#chat-send { display: grid; place-items: center; flex: 0 0 44px; width: 44px; height: 44px; padding: 10px; border-radius: 50%; }
#chat-send svg { width: 21px; height: 21px; }
#chat-status { font-size: 12px; color: var(--muted); margin: 0 0 16px; overflow-wrap: anywhere; }
.chat-history { width: 100%; max-width: 850px; margin: 20px auto 36px; }
.chat-message { padding: 14px 0; max-width: 90%; }
.chat-author { display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
.chat-message p { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.8; color: var(--ink); }
.chat-user { margin-left: auto; padding: 16px 20px; background: var(--subtle); border-radius: 18px 18px 4px 18px; }
.chat-assistant { margin: 16px 0 28px; }
.chat-system > p { color: #805d25; }
.chat-message details { font-size: 11px; color: var(--muted); margin-top: 12px; }
.chat-message details summary { padding-block: 6px; }
.chat-message details p { color: var(--muted); font-size: 11px; }
.has-chat .intro { display: none; }
.has-chat .recovery-home { min-height: calc(100svh - 110px); }
.has-chat .command-bar { margin-top: auto; }
button { font: inherit; font-size: 13px; font-weight: 550; cursor: pointer; background: #292d34; color: #fff; border: 1px solid transparent; padding: 11px 16px; min-height: 44px; border-radius: 10px; transition: background-color 160ms ease-out, color 160ms ease-out; }
button:hover { background: #424a56; }
button:active { background: #151a21; }
button:disabled { background: #edf0f4; border-color: transparent; color: #777f8a; cursor: default; }
button#reload, button#trace, button#trace-more, button[data-action=revoke] { color: #5d6673; background: transparent; border-color: var(--line); }
button#reload { display: flex; align-items: center; gap: 8px; border: 0; padding-left: 0; }
button#reload svg { width: 17px; height: 17px; }
button#reload:hover, button#trace:hover, button#trace-more:hover, button[data-action=revoke]:hover { background: #f0f3f7; color: #293d5a; }
button#signout { display: grid; place-items: center; width: 44px; height: 44px; padding: 10px; background: #e1e9f2; color: #647185; border-radius: 12px; }
button#signout svg { width: 21px; height: 21px; }
button#signout:hover { background: #d5e0ee; color: #2b3f5a; }
button:focus-visible, a:focus-visible, summary:focus-visible, [tabindex]:focus-visible { outline: 2px solid var(--focus); outline-offset: 4px; }
.toolbar, .actions, .section-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.toolbar { justify-content: space-between; }
#start { display: flex; gap: 12px; align-items: center; padding: 10px 12px 10px 16px; }
#start svg { width: 20px; height: 20px; }
#feedback { font-size: 12px; color: var(--muted); margin: 10px 20px 0; padding-bottom: 14px; overflow-wrap: anywhere; min-height: 34px; }
#feedback[data-error=true] { color: #a23c32; }
#panel { scroll-margin-top: 28px; }
#panel:focus { outline: none; }
section { min-width: 0; margin-bottom: 32px; padding: 26px 0; border-top: 1px solid var(--line); }
section p { color: var(--muted); max-width: 75ch; }
.outcome { padding: 24px; background: #f6f8fb; border: 1px solid #e1e7ef; border-radius: 12px; }
.outcome > p { font-size: 14px; margin: 6px 0; }
.outcome h2 { color: #34465d; margin-bottom: 6px; }
.outcome .technical { margin-top: 16px; padding-top: 12px; border-top: 1px solid #e1e7ef; font-size: 12px; color: #657185; }
.outcome[data-status=verified] { background: #f0f8f2; border-color: #d6e9da; }
.outcome[data-status=verified] h2 { color: #2d6240; }
.outcome[data-status=unknown], .outcome[data-status=partial], .outcome[data-status=applied_unverified], .outcome[data-status=needs_input], .outcome[data-status=stale] { background: #fcf7ee; border-color: #e9dfcb; }
.outcome[data-status=unknown] h2, .outcome[data-status=partial] h2, .outcome[data-status=applied_unverified] h2, .outcome[data-status=needs_input] h2, .outcome[data-status=stale] h2 { color: #805d25; }
.outcome[data-status=verification_failed], .outcome[data-status=insufficient_capacity] { background: #fdf2f0; border-color: #efdad6; }
.outcome[data-status=verification_failed] h2, .outcome[data-status=insufficient_capacity] h2 { color: #963f33; }
.sources { display: grid; grid-template-columns: 1fr 1fr; gap: 40px; scroll-margin-top: 28px; }
.sources section { padding-top: 0; border: 0; margin-bottom: 20px; }
.sources h2 { font-size: 17px; padding-bottom: 12px; border-bottom: 1px solid var(--line); }
.sources ul { list-style: none; padding: 0; margin: 0; }
.sources li { padding: 13px 0; border-bottom: 1px solid #eef1f4; font-size: 14px; line-height: 1.8; overflow-wrap: anywhere; }
.sources li:last-child { border-bottom: 0; }
.sources p { font-size: 14px; }
.technical, code { font-family: Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
.reference { display: grid; grid-template-columns: 85px minmax(0, 1fr); gap: 8px 16px; padding: 16px 20px; border-radius: 10px; background: var(--subtle); font-size: 12px; }
dt { color: var(--muted); } dd { margin: 0; overflow-wrap: anywhere; }
.table-scroll { overflow-x: auto; margin: 24px 0; border: 1px solid var(--line); border-radius: 12px; }
table { border-collapse: collapse; width: 100%; min-width: 650px; text-align: left; font-size: 13px; font-variant-numeric: tabular-nums; }
caption { padding: 16px; text-align: left; font-weight: 600; background: #f6f8fa; }
th, td { padding: 16px; vertical-align: top; border-bottom: 1px solid var(--line); }
thead { font-size: 12px; color: var(--muted); }
tbody th { max-width: 340px; font-weight: 450; overflow-wrap: anywhere; }
tbody tr:last-child > * { border: 0; }
.operation-details { margin-top: 8px; color: var(--muted); font-size: 12px; }
.operation-details summary { min-height: 32px; padding-block: 4px; }
.section-heading { justify-content: space-between; }
.section-heading h2 { margin: 0; }
.moodle { margin-top: 24px; scroll-margin-top: 24px; }
.moodle h3 { font-size: 16px; margin: 24px 0 12px; }
.moodle h4 { font-size: 16px; margin: 4px 0 12px; overflow-wrap: anywhere; }
.moodle ul { list-style: none; padding: 0; margin: 0; }
.moodle-course-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr)); gap: 12px; }
.moodle-course-list li { display: flex; flex-direction: column; gap: 6px; background: var(--subtle); border-radius: 10px; padding: 16px; overflow-wrap: anywhere; }
.moodle-course-list span { color: var(--muted); }
.moodle-task-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr)); gap: 16px; }
.moodle-task-list li { min-width: 0; padding: 20px; border: 1px solid var(--line); border-radius: 12px; }
.moodle-task-list .moodle-subject { margin: 0; font-size: 12px; }
.moodle-description { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; }
.moodle-task-list dl { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 6px 12px; font-size: 13px; }
.moodle-task-list a { font-size: 13px; }
body[data-moodle-enabled=true] .recovery-home { min-height: auto; }
body[data-moodle-enabled=true] .intro { display: none; }
.journal > p { font-size: 12px; }
#trace-records { list-style: none; padding: 0; margin: 16px 0; }
#trace-records li { border-top: 1px solid var(--line); }
#trace-records summary { padding: 14px 4px; font-size: 13px; overflow-wrap: anywhere; }
#trace-records summary:hover { color: var(--accent); }
#trace-records pre { background: var(--subtle); padding: 18px; border-radius: 10px; margin: 0 0 16px; color: #4c5d74; font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
footer { max-width: 85ch; padding: 0 0 24px; font-size: 12px; line-height: 1.8; color: var(--muted); }
.skip { position: fixed; left: 80px; top: -80px; z-index: 5; background: var(--surface); padding: 12px; border-radius: 8px; }
.skip:focus { top: 12px; }
[hidden] { display: none !important; }
@media (max-width: 900px) { main { padding-inline: 28px; } .toolbar { gap: 16px; } #start { font-size: 12px; } .sources { gap: 28px; } }
@media (max-width: 640px) {
  body { background: #fff; }
  .sidebar { position: relative; inset: auto; width: 100%; padding: 10px 16px; flex-direction: row; justify-content: space-between; border-bottom: 1px solid var(--line); background: var(--surround); }
  .brand { width: 36px; }
  nav { display: flex; gap: 4px; margin: 0; }
  .nav-link { width: 52px; height: 52px; display: flex; flex-direction: column; gap: 3px; border-radius: 8px; color: #5d6979; }
  .nav-link svg { width: 19px; height: 19px; }
  .nav-link .nav-label { display: none; }
  .nav-short { display: block; font-size: 10px; line-height: 1.4; white-space: nowrap; }
  .sidebar-bottom { margin: 0; }
  button#signout { width: 36px; height: 40px; padding: 8px; }
  .app-content { border: 0; margin: 0; border-radius: 0; min-height: calc(100svh - 73px); }
  .topbar { padding: 14px 20px; min-height: 56px; gap: 10px; font-size: 11px; }
  .wordmark { font-size: 14px; }
  .topbar-results { font-size: 0; padding: 8px; }
  .topbar-results svg { width: 18px; height: 18px; }
  main { padding: 0 20px 8px; }
  .recovery-home { min-height: calc(100svh - 150px); padding-bottom: 24px; margin-bottom: 24px; }
  .demo-notice { font-size: 11px; }
  .intro { padding: 46px 0; }
  .welcome-logo { width: min(360px, 100%); }
  h1 { font-size: 28px; line-height: 1.25; }
  .intro > p { font-size: 13px; }
  .context-strip { padding: 12px 16px; font-size: 11px; gap: 8px; }
  .context-strip > span { font-size: 10px; }
  .context-strip a { gap: 6px; }
  .command-body { padding: 16px 16px 4px; }
  #recovery-message { font-size: 16px; }
  #message-hint { margin-bottom: 16px; }
  .toolbar { align-items: stretch; gap: 8px; flex-direction: column-reverse; }
  #start { justify-content: space-between; min-height: 48px; font-size: 12px; }
  button#reload { justify-content: center; padding: 10px; }
  #feedback { margin-inline: 16px; font-size: 11px; }
  h2 { font-size: 19px; }
  .sources { grid-template-columns: 1fr; gap: 12px; }
  .outcome { padding: 20px; }
  .reference { grid-template-columns: 1fr; gap: 0; }
  dd { margin-bottom: 12px; }
  .actions { flex-direction: column; align-items: stretch; }
  .section-heading { gap: 16px; }
}
@media (prefers-reduced-motion: reduce) { button { transition: none; } .welcome-logo { transition: opacity 150ms ease-out; } }
"""
