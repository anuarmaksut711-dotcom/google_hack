"""Responsive timetable review over the existing shared dark workspace."""

CSS = r'''
:root{color-scheme:dark;--ink:#e3e4e9;--muted:#a2a5af;--line:#2b2d33;--accent:#a6bcff;--paper:#101010}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 'Segoe UI',system-ui,sans-serif}
main.planning-page{width:min(100%,1180px);margin:0 auto;padding:24px 32px 56px}
.planning-heading{margin:0 0 24px}
.planning-heading h1{margin:0 0 5px;font-size:30px;line-height:1.3;font-weight:600;letter-spacing:-.025em}
.planning-heading p{margin:0;color:var(--muted);font-size:15px}
.planning-page h2{margin:0;font-size:19px;line-height:1.4;font-weight:600;letter-spacing:-.01em}
.planning-page h3{margin:0;font-size:15px;line-height:1.5;font-weight:600}
.planning-page p{max-width:86ch}
.planning-page a{color:var(--accent);text-underline-offset:3px}
.planning-page a:hover{text-decoration-thickness:2px}
.planning-page button,.planning-page input,.planning-page select{font:inherit;font-size:14px;border-radius:9px;min-height:44px;max-width:100%}
.planning-page input,.planning-page select{width:100%;padding:9px 12px;border:1px solid var(--line);background:var(--surround,#101010);color:var(--ink)}
.planning-page button{display:inline-flex;align-items:center;justify-content:center;gap:8px;margin:0;padding:10px 16px;border:1px solid var(--line);background:var(--subtle,#1c1e23);color:var(--ink);cursor:pointer;font-weight:500;line-height:1.4}
.planning-page button[type=submit],.planning-page #classes-import{background:var(--action,#3559cd);border-color:var(--action,#3559cd);color:#fff}
.planning-page button:disabled,.planning-page input:disabled,.planning-page select:disabled{opacity:.5;cursor:default}
.planning-page button:hover:not(:disabled){filter:brightness(1.12)}
.planning-page :focus-visible{outline:2px solid var(--focus,#91acff);outline-offset:3px}
.planning-page [hidden]{display:none!important}
.planning-page label{display:block;margin:0 0 6px;font-size:13px;font-weight:500;color:var(--muted)}
.planning-page fieldset{min-width:0}
.planning-page .classes-sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.planning-page [role=status]{margin:12px 0 0;font-size:14px;overflow-wrap:anywhere}
.planning-page details{font-size:13px;color:var(--muted)}
.planning-page summary{width:fit-content;cursor:pointer;padding:6px 0;color:var(--muted)}
.planning-page details p{margin:8px 0}
body[data-shared-navigation=true][data-view=planning] .page-navigation-topbar{min-height:8px;padding:0 28px}
body[data-shared-navigation=true][data-view=planning] .page-navigation-topbar .view-heading,
body[data-shared-navigation=true][data-view=planning] .page-navigation-topbar .topbar-actions{display:none}
body[data-shared-navigation=true] .planning-page>.classes-section{margin:0;padding:0;border:0;background:transparent}
#class-schedule{margin:0}
#classes-fields{border:0;padding:0;margin:0;min-width:0}
#classes-fields>legend{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.classes-upload,.classes-period,.classes-list-panel{padding:20px 24px;margin:0 0 12px;border:1px solid var(--line);border-radius:14px;background:var(--surface,#151619)}
.classes-upload-heading{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px 16px;margin-bottom:12px}
.classes-intro{flex:1;min-width:0}
.classes-intro p{margin:4px 0 0;font-size:13px;color:var(--muted)}
.classes-upload-heading>div>p{margin:4px 0 0;font-size:13px;color:var(--muted)}
.classes-upload-note{margin:8px 0 0;font-size:12px;color:var(--muted)}
.planning-page #classes-reload{padding:4px 0;min-height:32px;border:0;background:transparent;color:var(--muted);font-size:13px}
.classes-file-line{display:flex;align-items:center;gap:12px}
.classes-file-control{flex:1;min-width:0}
.classes-file-line>input[type=file]{flex:1;min-width:0}
.classes-file-control label{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.planning-page input[type=file]{padding:6px;color:var(--muted);font-size:13px;overflow:hidden}
.planning-page input[type=file]::file-selector-button{font:inherit;min-height:32px;padding:4px 12px;margin-right:12px;border:0;border-radius:6px;background:var(--subtle,#1c1e23);color:var(--ink);cursor:pointer}
.planning-page #classes-import{flex-shrink:0;min-width:168px}
.classes-upload-help{margin-top:8px}
.classes-upload-help summary{font-size:12px}
.classes-date-fields{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.classes-period>p{margin:8px 0 0;font-size:12px;color:var(--muted)}
.classes-list-heading{margin-bottom:14px}
.classes-empty{padding:20px 0;color:var(--muted);font-size:14px}
#classes-rows{display:grid;gap:14px}
.classes-day{border:1px solid var(--line);border-radius:10px;min-width:0;overflow:hidden}
.classes-day-heading{display:flex;align-items:baseline;gap:9px;padding:11px 16px 8px;background:var(--subtle,#1c1e23)}
.classes-day-count{color:var(--muted);font-size:13px;font-weight:400}
.classes-entry{margin:0 12px;border:0;border-top:1px solid var(--line);padding:0;min-width:0}
.classes-day-rows>.classes-entry:first-child{border-top:0}
.classes-summary{display:grid;grid-template-columns:132px minmax(0,1fr) 40px;align-items:center;gap:12px;min-height:48px;padding:3px 0}
.classes-time{font-size:14px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.classes-subject{font-size:14px;line-height:1.45;overflow-wrap:anywhere}
.planning-page .classes-edit{width:40px;min-height:40px;padding:9px;border:0;background:transparent;color:var(--muted)}
.classes-edit svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.classes-edit span{font-size:23px;line-height:1}
.planning-page .classes-edit:hover{background:var(--selected,#272a32);color:var(--ink)}
.classes-editor{padding:12px 0 16px}
.classes-editor-fields{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1.3fr) minmax(0,1fr) minmax(0,1fr);gap:12px}
.classes-editor-fields label{font-size:12px}
.classes-editor-actions{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;margin-top:12px}
.planning-page .classes-remove{background:transparent;color:var(--theme-error,#f2b1b3);font-size:13px}
.planning-page .classes-done{min-width:100px}
.planning-page #class-add{margin-top:14px;background:transparent;min-height:40px;padding:8px 14px;color:var(--accent)}
.classes-actions{display:flex;align-items:center;flex-wrap:wrap;gap:10px;justify-content:flex-end;margin-top:16px}
.planning-page #classes-save{order:2;min-width:205px}
.planning-page #classes-calendar{order:1;background:transparent;min-width:0;font-size:13px}
.classes-calendar-actions{margin-top:12px;font-size:13px}
.classes-calendar-actions p{margin:0}
.classes-section>[role=status]{padding:0 2px}
.classes-list-panel>p{font-size:12px;color:var(--muted);margin:12px 0 0}
.planning-study-note{border-top:1px solid var(--line);margin-top:28px;padding-top:18px}
.planning-study-note h2{font-size:16px}
.planning-study-note p{margin:6px 0;font-size:13px;color:var(--muted)}
.planning-study-note>a{font-size:13px}
.planning-study-note details{margin-top:8px}
.planning-return{font-size:13px;margin:14px 0 0}
#planning-inputs{margin-top:24px;padding:22px 24px;background:var(--surface,#151619);border:1px solid var(--line);border-radius:14px}
#planning-inputs>p{font-size:14px;color:var(--muted)}
#planning-inputs form{border-top:1px solid var(--line);margin-top:20px;padding-top:20px}
#planning-inputs form>h2{margin-bottom:14px}
#planning-inputs form>label{margin-top:12px}
#planning-inputs form>button{margin:12px 8px 0 0}
#planning-inputs input[type=checkbox]{width:20px;min-height:20px}
#planning-inputs ul{padding-left:20px}
@media(max-width:1000px){main.planning-page{padding-left:24px;padding-right:24px}.classes-editor-fields{grid-template-columns:1fr 1fr}.classes-summary{grid-template-columns:120px minmax(0,1fr) 40px;gap:8px}}
@media(max-width:760px){
 body[data-shared-navigation=true][data-view=planning] .page-navigation-topbar{min-height:56px;padding:8px 16px}
 main.planning-page{padding:12px 16px 40px}.planning-heading{margin-bottom:20px}.planning-heading h1{font-size:27px}.planning-heading p{font-size:14px}
 .classes-upload,.classes-period,.classes-list-panel{padding:16px;border-radius:12px}.classes-file-line{align-items:stretch;flex-direction:column}.planning-page #classes-import{min-width:0;width:100%}
 .classes-date-fields{gap:12px}.planning-page h2{font-size:18px}.classes-day-heading{padding-left:12px;padding-right:12px}.classes-entry{margin:0 10px}
 .classes-summary{grid-template-columns:104px minmax(0,1fr) 36px;gap:6px;min-height:54px}.classes-time{font-size:12px}.classes-subject{font-size:13px}.planning-page .classes-edit{width:36px;min-height:40px;padding:8px}
 .classes-actions{align-items:stretch;flex-direction:column}.planning-page #classes-save{order:0;width:100%;min-width:0}.planning-page #classes-calendar{order:1;width:100%}
 .classes-editor-fields{gap:12px 10px}.classes-editor-field-subject,.classes-editor-field-weekday{grid-column:1/-1}
 #planning-inputs{padding:16px}
}
@media(prefers-reduced-motion:reduce){.planning-page *{scroll-behavior:auto}}
'''
