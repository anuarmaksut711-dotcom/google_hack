"""Self-hosted stylesheet; no fonts/CDNs or build-time external resources."""
STYLES = r"""
:root { color-scheme: light; font-family: 'Segoe UI', system-ui, sans-serif; color: #19352e; background: #f1f5f3; }
* { box-sizing: border-box; }
body { margin: 0; font-size: 16px; line-height: 1.55; }
header { display: flex; align-items: center; gap: 24px; padding: 20px max(24px, calc((100vw - 1160px)/2)); border-bottom: 1px solid #c1d2c9; background: #fff; }
.brand { font-size: 23px; font-weight: 750; text-decoration: none; color: #115846; }
header span { flex: 1; color: #50655d; }
main { max-width: 1208px; margin: auto; padding: 40px 24px 24px; }
.intro { max-width: 760px; margin-bottom: 28px; }
.eyebrow { font-size: 14px; font-weight: 650; color: #416657; margin: 0 0 8px; }
h1 { font-size: clamp(32px, 4vw, 48px); font-weight: 650; line-height: 1.1; letter-spacing: -0.025em; margin: 0 0 20px; }
h2 { font-size: 22px; line-height: 1.3; margin: 0 0 14px; }
p { margin: 8px 0 16px; }
button { font: inherit; font-weight: 600; cursor: pointer; background: #17644e; color: #fff; border: 1px solid #17644e; padding: 11px 18px; min-height: 46px; border-radius: 8px; }
button:hover { background: #104b3b; }
button:disabled { background: #d9e3de; border-color: #c1d2c9; color: #52665c; cursor: default; }
button#reload, button#trace, button#trace-more, button#signout, button[data-action=revoke] { background: #fff; color: #19352e; border-color: #8aa899; }
button:focus-visible, a:focus-visible, [tabindex]:focus-visible { outline: 3px solid #a34b05; outline-offset: 4px; }
.toolbar, .actions, .section-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
#feedback { min-height: 28px; color: #435c50; }
#feedback[data-error=true] { color: #8c2818; }
section { background: #fff; padding: 24px; border: 1px solid #c1d2c9; border-radius: 12px; margin-bottom: 20px; min-width: 0; }
.outcome { background: #e1eee7; border-left: 5px solid #17644e; }
.outcome[data-status=unknown], .outcome[data-status=partial], .outcome[data-status=applied_unverified] { background: #fff4de; border-left-color: #8a5c0d; }
.outcome[data-status=verification_failed], .outcome[data-status=insufficient_capacity] { background: #fcece6; border-left-color: #9d3824; }
.sources { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.sources li { margin-bottom: 8px; }
.technical, code { font-family: Consolas, monospace; font-size: 13px; overflow-wrap: anywhere; }
.reference { display: grid; grid-template-columns: 85px minmax(0, 1fr); gap: 8px 16px; background: #f1f5f3; padding: 16px; }
dt { color: #50655d; } dd { margin: 0; overflow-wrap: anywhere; }
.table-scroll { overflow-x: auto; margin: 24px 0; }
table { border-collapse: collapse; width: 100%; min-width: 650px; text-align: left; }
caption { text-align: left; font-weight: 600; margin-bottom: 12px; }
th, td { vertical-align: top; border-bottom: 1px solid #c1d2c9; padding: 14px 16px 14px 0; }
thead { color: #50655d; } tbody th { font-weight: 500; max-width: 360px; overflow-wrap: anywhere; }
.section-heading { justify-content: space-between; } .section-heading h2 { margin: 0; }
#trace-records { padding-left: 24px; } #trace-records li { border-bottom: 1px solid #c1d2c9; padding: 16px 0; }
#trace-records pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; background: #f1f5f3; padding: 16px; }
a { color: #115846; } footer { color: #50655d; font-size: 14px; padding: 8px 0 32px; }
.skip { position: absolute; top: -80px; left: 16px; background: #fff; padding: 12px; z-index: 2; } .skip:focus { top: 8px; }
[hidden] { display: none !important; }
@media (max-width: 640px) { main { padding: 28px 16px; } header { padding: 16px; gap: 12px; flex-wrap: wrap; } header span { font-size: 14px; } section { padding: 20px 16px; } .sources { grid-template-columns: 1fr; gap: 0; } .toolbar button { width: 100%; } .reference { grid-template-columns: 1fr; gap: 0; } dd { margin-bottom: 12px; } }
"""
