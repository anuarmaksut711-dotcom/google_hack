ACCOUNT_STYLES = r"""
body.settings-page { min-height: 100svh; background: var(--surround); color: var(--ink); }
.settings-header { display: flex; align-items: center; justify-content: space-between; gap: 24px; min-height: 84px; padding: 20px 40px; border-bottom: 1px solid var(--line); background: #141518; }
.settings-brand { color: var(--ink); font-size: 24px; font-weight: 600; letter-spacing: -.035em; text-decoration: none; }
.settings-back { display: inline-flex; align-items: center; gap: 10px; min-height: 44px; padding: 8px 14px; border: 1px solid var(--line); border-radius: 12px; color: var(--muted); text-decoration: none; }
.settings-back:hover { background: var(--selected); color: var(--ink); }
.settings-back svg { width: 17px; height: 17px; }
main.settings-main { width: min(100%, 904px); padding: 64px 32px 80px; margin: 0 auto; }
.settings-main > h1 { margin: 0 0 32px; font-size: clamp(28px, 3vw, 36px); font-weight: 450; }
.settings-card { overflow: hidden; padding: 32px; border: 1px solid var(--line); border-radius: 24px; background: #1d1e22; }
.settings-card-heading { display: flex; align-items: center; gap: 14px; margin-bottom: 24px; }
.settings-card-heading h2 { margin: 0; font-size: 22px; }
.settings-card-icon { display: grid; place-items: center; width: 44px; height: 44px; border: 1px solid #363943; border-radius: 13px; background: var(--selected); color: var(--accent); }
.settings-card-icon svg { width: 23px; height: 23px; }
.settings-card p { max-width: 76ch; color: var(--muted); }
.settings-card #moodle-connection-status { margin: 0 0 24px; color: var(--ink); font-size: 15px; line-height: 1.7; }
.settings-card[data-connected=true] #moodle-connection-status { padding-left: 22px; position: relative; }
.settings-card[data-connected=true] #moodle-connection-status::before { position: absolute; left: 0; top: 10px; width: 7px; height: 7px; border-radius: 50%; background: #8ebda8; content: ""; }
.settings-card details { border: 1px solid #363943; border-radius: 14px; background: #18191d; }
.settings-card summary { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 56px; padding: 14px 18px; border-radius: 13px; list-style: none; color: var(--ink); font-weight: 550; }
.settings-card summary::-webkit-details-marker { display: none; }
.settings-card summary::after { width: 7px; height: 7px; margin-right: 3px; border-right: 1.5px solid currentColor; border-bottom: 1.5px solid currentColor; transform: rotate(45deg); content: ""; }
.settings-card summary:hover { background: var(--selected); color: var(--ink); }
.settings-card details[open] summary { margin-bottom: 18px; border-bottom: 1px solid var(--line); border-radius: 13px 13px 0 0; }
.settings-card details[open] summary::after { transform: rotate(225deg); }
.settings-card details > :not(summary) { margin-left: 20px; margin-right: 20px; }
.settings-card details > :last-child { margin-bottom: 20px; }
.settings-card ol { padding-left: 22px; color: var(--muted); }
.settings-card li + li { margin-top: 8px; }
.settings-card form { display: grid; gap: 12px; margin: 24px 0 0; padding-top: 24px; border-top: 1px solid var(--line); }
.settings-card label { color: var(--ink); font-weight: 550; }
.settings-card input { width: 100%; min-width: 0; min-height: 48px; padding: 12px 14px; border: 1px solid #3c3f48; border-radius: 10px; background: #101114; color: var(--ink); font: inherit; font-size: 16px; caret-color: var(--accent); }
.settings-card input:hover { border-color: #626775; }
.settings-card input:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; border-color: var(--focus); }
.settings-card form button { justify-self: start; min-height: 44px; padding: 11px 20px; border-radius: 10px; }
.settings-card #moodle-url-help { margin: -4px 0 4px; font-size: 12px; }
.settings-sync { margin: 28px 0 0 !important; padding-top: 24px; border-top: 1px solid var(--line); font-size: 12px; line-height: 1.8; }
.settings-card #account-status:empty { display: none; }
.settings-card #account-status:not(:empty) { margin: 20px 0 0; padding: 12px 16px; border: 1px solid #53607b; border-radius: 10px; background: #232a38; color: #dde5ff; overflow-wrap: anywhere; }
@media (max-width: 600px) {
  .settings-header { gap: 16px; min-height: 72px; padding: 14px 20px; flex-wrap: wrap; }
  .settings-brand { font-size: 21px; }
  .settings-back { padding: 7px 10px; font-size: 12px; }
  main.settings-main { padding: 32px 16px 48px; }
  .settings-main > h1 { margin-bottom: 24px; }
  .settings-card { padding: 24px 20px; border-radius: 18px; }
  .settings-card details > :not(summary) { margin-left: 14px; margin-right: 14px; }
  .settings-card form button { width: 100%; }
}
"""
