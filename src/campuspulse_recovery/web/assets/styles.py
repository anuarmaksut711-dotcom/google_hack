"""Self-hosted light workspace. No external fonts or build step."""
STYLES = r"""
:root {
  color-scheme: light;
  --surround: #f5f5f3; --surface: #fff; --ink: #252624;
  --muted: #666964; --line: #e7e8e4; --subtle: #f7f8f6;
  --accent: #37654a; --selected: #e8ebe6; --focus: #376d50;
  --action: #292d29; --action-hover: #41483f; --radius: 12px;
  font-family: 'Segoe UI', system-ui, sans-serif; font-synthesis: none;
  color: var(--ink); background: var(--surround);
}
* { box-sizing: border-box; }
html { scroll-padding-top: 88px; scrollbar-color: #c8cbc4 transparent; }
body { margin: 0; font-size: 14px; line-height: 1.65; }
::selection { background: #dce9df; color: #183c28; }
[hidden] { display: none !important; }
a { color: var(--accent); text-underline-offset: 4px; }
svg { fill: none; stroke: currentColor; stroke-width: 1.65; stroke-linecap: round; stroke-linejoin: round; flex-shrink: 0; }
button, textarea { font: inherit; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 42px; padding: 10px 16px; border: 1px solid transparent; border-radius: 9px; font-size: 13px; font-weight: 550; cursor: pointer; background: var(--action); color: #fff; transition: background-color 160ms ease-out, color 160ms ease-out; }
button svg { width: 18px; height: 18px; }
button:hover { background: var(--action-hover); }
button:active { background: #151a16; }
button:disabled { cursor: default; color: #797e77; background: #edf0ea; border-color: transparent; }
button:focus-visible, a:focus-visible, summary:focus-visible, textarea:focus-visible, [tabindex]:focus-visible { outline: 2px solid var(--focus); outline-offset: 4px; }
.sidebar { position: fixed; inset: 0 auto 0 0; z-index: 6; display: flex; flex-direction: column; width: 236px; padding: 26px 16px 18px; }
.brand { display: flex; align-items: center; gap: 10px; width: fit-content; margin: 0 10px 34px; color: var(--ink); text-decoration: none; font-size: 17px; font-weight: 650; letter-spacing: -.03em; }
.brand-mark { width: 29px; height: 29px; stroke-width: 1.9; }
.sidebar nav { display: grid; gap: 5px; }
.nav-link { display: flex; align-items: center; gap: 12px; min-height: 44px; padding: 10px 14px; border-radius: 8px; color: #595e57; font-size: 13px; text-decoration: none; }
.nav-link svg { width: 19px; height: 19px; }
.nav-link:hover { background: #edeeea; color: var(--ink); }
.nav-link.current { color: #262c26; background: var(--selected); font-weight: 600; }
.nav-short { display: none; }
.workspace-tools { margin-top: 20px; }
.workspace-tools > summary { min-height: 36px; padding: 8px 14px; font-size: 12px; color: var(--muted); }
.workspace-tools > summary:hover { color: var(--ink); }
.sidebar-note { display: flex; gap: 9px; margin: auto 10px 22px; padding-top: 40px; color: var(--muted); font-size: 12px; }
.sidebar-note svg { width: 17px; height: 17px; margin-top: 3px; }
.sidebar-note p { margin: 0; line-height: 1.65; }
.sidebar-bottom { display: grid; gap: 4px; padding-top: 12px; border-top: 1px solid #dfe2da; }
.account-link, #signout { width: 100%; min-height: 44px; display: flex; justify-content: flex-start; align-items: center; gap: 12px; padding: 10px 14px; border-radius: 8px; font-size: 12px; font-weight: 400; color: var(--muted); background: transparent; text-decoration: none; }
.account-link svg, #signout svg { width: 18px; height: 18px; }
.account-link:hover, #signout:hover { background: var(--selected); color: var(--ink); }
.app-content { margin: 10px 10px 10px 236px; min-height: calc(100svh - 20px); border: 1px solid var(--line); border-radius: 14px; background: var(--surface); }
.topbar { position: sticky; top: 0; z-index: 4; display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 76px; padding: 16px 30px; border-radius: 14px 14px 0 0; border-bottom: 1px solid #eff0ec; background: var(--surface); }
.view-heading { display: flex; align-items: baseline; gap: 14px; min-width: 0; }
#view-title { font-size: 14px; font-weight: 600; }
#view-subtitle { color: var(--muted); font-size: 12px; }
#reload { background: transparent; color: var(--muted); min-height: 36px; padding: 8px 10px; font-size: 12px; }
#reload:hover { color: var(--ink); background: var(--subtle); }
#mobile-nav-toggle { display: none; }
main { max-width: 1140px; padding: 0 44px; margin: auto; }
section { min-width: 0; }
h1, h2, h3, h4 { text-wrap: balance; }
h1 { font-size: 32px; line-height: 1.3; font-weight: 500; letter-spacing: -.035em; margin: 0 0 12px; }
h2 { font-size: 21px; line-height: 1.4; font-weight: 600; letter-spacing: -.025em; margin: 0 0 12px; }
h3 { font-size: 15px; font-weight: 600; }
p { margin: 8px 0 16px; }
summary { cursor: pointer; }
summary:hover { color: var(--accent); }
.recovery-home { display: flex; flex-direction: column; align-items: center; min-height: calc(100svh - 180px); padding: 28px 0 18px; }
.demo-notice { align-self: flex-start; width: 100%; margin: 0; font-size: 12px; color: #79633d; }
.demo-notice summary { width: fit-content; }
.demo-notice p { max-width: 75ch; background: #faf7f0; padding: 12px 16px; border-radius: 8px; }
.intro { display: flex; flex-direction: column; justify-content: flex-end; align-items: center; width: 100%; text-align: center; margin: auto 0 30px; padding-top: 12px; }
.intro > p { max-width: 52ch; margin: 0; color: var(--muted); font-size: 14px; }
.welcome-logo { position: relative; width: 250px; max-width: 100%; aspect-ratio: 4 / 3; margin: 0 auto 12px; transition: opacity 180ms ease-out; }
.welcome-logo canvas { position: absolute; inset: 0; display: block; width: 100%; height: 100%; }
.welcome-logo[data-state=loading] canvas { visibility: hidden; }
.welcome-logo img { position: absolute; top: 50%; left: 50%; width: 56%; height: auto; transform: translate(-50%, -50%); }
.welcome-logo.is-dismissed { display: none; }
.command-bar { width: 100%; max-width: 720px; border: 1px solid #dcded7; border-radius: 16px; background: var(--surface); }
.command-bar:focus-within { border-color: #9ba897; }
.command-body { padding: 18px 20px 12px; }
.message-label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
#recovery-message { display: block; width: 100%; min-height: 58px; max-height: 200px; margin: 0; padding: 0; resize: vertical; border: 0; background: transparent; color: var(--ink); font-size: 15px; line-height: 1.7; caret-color: var(--accent); }
#recovery-message::placeholder { color: var(--muted); opacity: 1; }
#recovery-message:focus-visible { outline-offset: 5px; }
.chat-submit { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-top: 12px; }
#message-hint { margin: 0; font-size: 11px; color: var(--muted); }
#chat-send { display: grid; place-items: center; width: 36px; min-height: 36px; padding: 8px; border-radius: 10px; }
#chat-send svg { width: 19px; height: 19px; }
#chat-status { margin: 10px 0 0; max-width: 75ch; font-size: 12px; color: var(--muted); }
.prompt-suggestions { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; width: 100%; max-width: 760px; margin: 16px 0 26px; }
.prompt-chip { min-height: 36px; padding: 7px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: #5b6259; font-size: 12px; font-weight: 400; }
.prompt-chip svg { width: 13px; height: 13px; color: #7d8579; }
.prompt-chip:hover { background: var(--subtle); color: var(--ink); border-color: #cfd6ca; }
.recovery-controls { display: flex; align-items: center; justify-content: space-between; gap: 24px; width: 100%; max-width: 720px; padding: 20px 0; border-top: 1px solid var(--line); }
.recovery-controls h2 { font-size: 15px; letter-spacing: -.01em; margin: 0 0 4px; }
.recovery-controls p { margin: 0; max-width: 38ch; font-size: 12px; color: var(--muted); }
#start { flex-shrink: 0; }
.consent-note { display: flex; justify-content: center; gap: 7px; width: 100%; max-width: 680px; color: var(--muted); font-size: 12px; text-align: center; margin: 12px 0 0; }
.consent-note svg { width: 15px; height: 15px; margin-top: 3px; }
#feedback { max-width: 720px; font-size: 12px; color: var(--muted); margin: 12px auto 0; min-height: 20px; overflow-wrap: anywhere; text-align: center; }
#feedback[data-error=true] { position: sticky; bottom: 16px; z-index: 5; color: #8e362b; background: #fcf0ed; padding: 14px 18px; border-radius: 10px; text-align: left; }
.chat-history { width: 100%; max-width: 720px; margin: 20px auto 30px; }
.chat-message { max-width: 94%; padding: 14px 0; }
.chat-author { display: block; font-size: 12px; color: var(--muted); margin-bottom: 8px; font-weight: 550; }
.chat-message p { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.85; }
.chat-user { width: fit-content; margin-left: auto; padding: 14px 20px; background: #f1f3ee; border-radius: 14px; }
.chat-assistant { margin: 18px 0 24px; }
.chat-system > p { color: #805d25; }
.chat-message details { margin-top: 14px; font-size: 12px; color: var(--muted); }
.chat-message details p { font-size: 12px; }
.has-chat .intro, .has-chat .prompt-suggestions { display: none; }
.has-chat .recovery-home { padding-top: 12px; }
body[data-view=assistant] #moodle, body[data-view=assistant] #panel, body[data-view=assistant] .journal,
body[data-view=tasks] .recovery-home, body[data-view=tasks] #panel, body[data-view=tasks] .journal,
body[data-view=plan] .recovery-home, body[data-view=plan] #moodle, body[data-view=plan] .journal,
body[data-view=journal] .recovery-home, body[data-view=journal] #moodle, body[data-view=journal] #panel { display: none; }
#panel, .moodle, .journal { min-height: calc(100svh - 195px); padding: 34px 0 32px; }
#panel:focus { outline: none; }
#panel section { margin-bottom: 28px; }
.outcome { padding: 22px 24px; border-radius: var(--radius); background: #f2f4f0; }
.outcome p { max-width: 75ch; margin: 6px 0; font-size: 13px; color: var(--muted); }
.outcome h2 { font-size: 19px; margin-bottom: 6px; }
.outcome .technical { margin-top: 16px; font-size: 12px; }
.outcome[data-status=verified] { background: #edf6ef; }
.outcome[data-status=verified] h2 { color: #2d6240; }
.outcome[data-status=unknown], .outcome[data-status=partial], .outcome[data-status=applied_unverified], .outcome[data-status=needs_input], .outcome[data-status=stale] { background: #faf5e9; }
.outcome[data-status=unknown] h2, .outcome[data-status=partial] h2, .outcome[data-status=applied_unverified] h2, .outcome[data-status=needs_input] h2, .outcome[data-status=stale] h2 { color: #805d25; }
.outcome[data-status=verification_failed], .outcome[data-status=insufficient_capacity] { background: #fcf0ed; }
.outcome[data-status=verification_failed] h2, .outcome[data-status=insufficient_capacity] h2 { color: #963f33; }
.sources { display: grid; grid-template-columns: 1fr 1fr; gap: 28px; }
.sources section { min-width: 0; padding-top: 20px; }
.sources h2 { font-size: 16px; padding-bottom: 10px; border-bottom: 1px solid var(--line); }
.sources ul { list-style: none; padding: 0; margin: 0; }
.sources li { padding: 12px 0; border-bottom: 1px solid var(--line); font-size: 13px; overflow-wrap: anywhere; }
.sources p, #panel > section > p { font-size: 13px; color: var(--muted); max-width: 75ch; }
.sources li:last-child { border: 0; }
.technical, code, pre { font-family: Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
.reference { display: grid; grid-template-columns: 85px minmax(0, 1fr); gap: 8px 16px; padding: 16px 20px; background: var(--subtle); border-radius: 8px; font-size: 12px; }
dt { color: var(--muted); } dd { margin: 0; overflow-wrap: anywhere; }
.plan-details { margin: 16px 0; font-size: 12px; color: var(--muted); }
.plan-details summary { min-height: 32px; padding-block: 6px; }
.review-flow { display: flex; flex-wrap: wrap; gap: 8px 24px; padding: 0; list-style: none; margin: 0 0 24px; font-size: 12px; color: var(--muted); }
.review-flow li { display: flex; align-items: center; gap: 8px; }
.review-flow b { display: grid; place-items: center; width: 24px; height: 24px; border: 1px solid var(--line); border-radius: 50%; font-size: 11px; font-weight: 500; }
.review-flow [aria-current=step] { color: var(--ink); font-weight: 600; }
.review-flow [aria-current=step] b { color: #fff; background: var(--action); border-color: var(--action); }
.table-scroll { overflow-x: auto; margin: 20px 0; border: 1px solid var(--line); border-radius: var(--radius); }
table { border-collapse: collapse; width: 100%; min-width: 620px; text-align: left; font-size: 13px; font-variant-numeric: tabular-nums; }
caption { padding: 14px 16px; text-align: left; font-weight: 600; background: var(--subtle); }
th, td { padding: 16px; vertical-align: top; border-bottom: 1px solid var(--line); }
thead { color: var(--muted); font-size: 12px; }
tbody th { max-width: 320px; font-weight: 400; overflow-wrap: anywhere; }
tbody tr:last-child > * { border: 0; }
.operation-details { margin-top: 10px; font-size: 12px; color: var(--muted); }
.operation-details summary { min-height: 32px; }
.actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 20px; }
#trace, #trace-more, button[data-action=revoke] { background: transparent; color: var(--muted); border-color: var(--line); }
#trace:hover, #trace-more:hover, button[data-action=revoke]:hover { background: var(--subtle); color: var(--ink); }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.section-heading h2 { margin: 0; }
#moodle-status { max-width: 75ch; color: var(--muted); font-size: 13px; margin-block: 14px 8px; }
.moodle > p > a { font-size: 12px; }
.moodle h3 { margin: 28px 0 12px; }
.moodle h4 { font-size: 16px; line-height: 1.5; margin: 4px 0 12px; overflow-wrap: anywhere; }
.moodle ul { list-style: none; margin: 0; padding: 0; }
.moodle-course-list { display: flex; flex-wrap: wrap; gap: 8px; }
.moodle-course-list li { display: flex; gap: 12px; padding: 10px 14px; border-radius: 8px; background: var(--subtle); overflow-wrap: anywhere; font-size: 12px; }
.moodle-course-list span { color: var(--muted); }
.moodle-task-list { display: grid; gap: 0; }
.moodle-task-list > li { min-width: 0; padding: 22px 0; border-top: 1px solid var(--line); }
.moodle-subject { margin: 0; font-size: 12px; color: var(--muted); }
.moodle-description { white-space: pre-wrap; overflow-wrap: anywhere; max-width: 75ch; font-size: 13px; color: var(--muted); }
.moodle-description-details { margin-block: 8px 12px; font-size: 12px; color: var(--muted); }
.moodle-description-details summary { width: fit-content; min-height: 32px; }
.moodle-task-list dl { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 5px 12px; margin-block: 12px; font-size: 12px; }
.moodle-task-list a { display: inline-flex; margin-right: 16px; font-size: 12px; }
.moodle-task-list button { font-size: 12px; min-height: 36px; padding: 7px 12px; background: var(--subtle); color: #4f5b4a; border-color: var(--line); }
.moodle-task-list button:hover { background: var(--selected); }
.moodle-tasks > details { margin-top: 16px; }
.journal > p { color: var(--muted); font-size: 13px; max-width: 75ch; }
#trace-records { list-style: none; padding: 0; margin: 24px 0; }
#trace-records li { border-top: 1px solid var(--line); }
#trace-records summary { padding: 16px 4px; font-size: 13px; overflow-wrap: anywhere; }
#trace-records pre { padding: 18px; margin: 0 0 16px; background: var(--subtle); border-radius: 8px; color: #4c5d4d; white-space: pre-wrap; overflow-wrap: anywhere; }
footer { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 6px 20px; min-height: 48px; padding: 10px 0 14px; color: var(--muted); font-size: 11px; }
footer details { position: relative; }
footer p { max-width: 65ch; }
.skip { position: fixed; left: 250px; top: -100px; padding: 10px 14px; z-index: 10; background: var(--surface); border-radius: 8px; }
.skip:focus { top: 12px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
@media (min-height: 1000px) { .intro { margin-bottom: 36px; } .recovery-home { padding-bottom: 40px; } }
@media (max-width: 1100px) { .sidebar { width: 212px; padding-inline: 12px; } .app-content { margin-left: 212px; } main { padding-inline: 30px; } #view-subtitle { display: none; } }
@media (max-height: 780px) and (min-width: 761px) { .welcome-logo { width: 170px; } .recovery-home { padding-top: 18px; } .intro { margin-bottom: 20px; } .prompt-suggestions { margin-block: 12px 18px; } }
@media (max-width: 760px) {
  body { background: var(--surface); }
  .sidebar { width: 260px; transform: translateX(-100%); visibility: hidden; background: var(--surround); padding-top: 28px; border-right: 1px solid var(--line); transition: transform 200ms ease-out, visibility 200ms; }
  body[data-nav-open=true] .sidebar { transform: translateX(0); visibility: visible; }
  .app-content { margin: 0; min-height: 100svh; border: 0; border-radius: 0; }
  .topbar { min-height: 68px; padding: 12px 18px; gap: 12px; border-radius: 0; }
  .view-heading { margin-right: auto; }
  #mobile-nav-toggle { display: grid; place-items: center; width: 36px; min-height: 36px; padding: 8px; background: transparent; color: var(--ink); }
  #reload { width: 36px; padding: 8px; } #reload span { display: none; }
  main { padding-inline: 22px; }
  .recovery-home { min-height: calc(100svh - 160px); padding-top: 20px; }
  .intro { margin-top: auto; margin-bottom: 26px; padding-top: 20px; }
  .welcome-logo { width: 200px; margin-bottom: 12px; }
  h1 { font-size: 27px; letter-spacing: -.03em; }
  .intro > p { font-size: 13px; max-width: 37ch; }
  .command-body { padding: 16px; }
  #recovery-message { font-size: 16px; }
  #message-hint { font-size: 10px; }
  .prompt-suggestions { gap: 7px; justify-content: flex-start; margin-block: 12px 22px; }
  .prompt-chip { min-height: 36px; font-size: 11px; padding-inline: 10px; }
  .recovery-controls { flex-direction: column; align-items: stretch; gap: 14px; padding-block: 18px; }
  .recovery-controls p { max-width: 50ch; }
  #start { min-height: 46px; }
  .consent-note { font-size: 11px; text-align: left; justify-content: flex-start; }
  #feedback { font-size: 11px; text-align: left; width: 100%; }
  .chat-message { max-width: 100%; }
  #panel, .moodle, .journal { padding-top: 26px; min-height: calc(100svh - 164px); }
  h2 { font-size: 19px; }
  .sources { grid-template-columns: 1fr; gap: 0; }
  .outcome { padding: 18px; }
  .reference { grid-template-columns: 1fr; gap: 4px; padding: 14px; }
  .reference dd { margin-bottom: 8px; }
  .actions { flex-direction: column; align-items: stretch; }
  .section-heading { flex-wrap: wrap; gap: 12px; }
  .review-flow { gap: 10px; font-size: 11px; }
  .review-flow b { width: 20px; height: 20px; }
  .moodle-task-list > li { padding-block: 20px; }
  .skip { left: 16px; }
  footer { gap: 4px 12px; font-size: 10px; padding-block: 12px; }
}
/* Only the assistant owns a viewport-sized shell; other views scroll normally. */
body[data-view=assistant] { display: flow-root; height: 100dvh; overflow: hidden; }
body[data-view=assistant] .app-content { height: calc(100dvh - 20px); min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
body[data-view=assistant] .topbar { flex-shrink: 0; }
body[data-view=assistant] #workspace { flex: 1; min-height: 0; width: 100%; display: flex; flex-direction: column; }
body[data-view=assistant] .recovery-home { flex: 1; min-height: 0; overflow: hidden; padding: 0; }
.conversation-scroll { flex: 1; min-height: 0; width: 100%; overflow-y: auto; overscroll-behavior-y: contain; scrollbar-gutter: stable; display: flex; flex-direction: column; align-items: center; padding: 20px 8px 12px; }
.conversation-scroll > * { flex-shrink: 0; }
.composer-dock { flex-shrink: 0; display: flex; flex-direction: column; align-items: center; width: 100%; padding: 12px 0 max(8px, env(safe-area-inset-bottom)); background: var(--surface); }
body[data-view=assistant] #feedback, body[data-view=assistant] footer { flex-shrink: 0; }
body[data-view=assistant] #feedback { margin-top: 0; }
body[data-view=assistant] #recovery-message { max-height: min(200px, 20dvh); }
@media (max-width: 760px) {
  body[data-view=assistant] .app-content { height: 100dvh; }
  .conversation-scroll { padding: 16px 4px 8px; }
  .composer-dock { padding-top: 8px; }
}
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; } }
"""
