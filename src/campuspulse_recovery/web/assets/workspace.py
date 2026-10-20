"""Chat navigation with native popovers and modal editing dialogs."""
CHAT_NEW = '''<button id="chat-new" class="chat-new" type="button" disabled><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M13 5H5v14h14v-8M15 3l6 6M10 14l2-5 6-6 3 3-6 6Z"/></svg>Новый чат</button>'''
CHAT_NAV = '''<section id="chat-library" class="chat-library" aria-label="Проекты и чаты" hidden>
<div class="chat-library-heading"><h2 id="chat-projects-heading">Проекты</h2><button id="chat-folder-create" class="chat-icon-button" type="button" aria-label="Создать проект" title="Создать проект"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7V5h6l2 2h10v13H3ZM12 10v7m-3-3h6"/></svg></button></div>
<p id="chat-library-status" role="status" aria-live="polite"></p><div id="chat-project-list" aria-labelledby="chat-projects-heading"></div>
<div class="chat-library-heading chat-recents-heading"><h2 id="chat-library-heading">Чаты</h2></div><div id="chat-library-list" aria-labelledby="chat-library-heading"></div>
<div id="chat-action-menu" class="chat-action-menu" popover="auto" role="group" aria-label="Действия с чатом"></div>
<dialog id="chat-edit-dialog" class="chat-edit-dialog" aria-labelledby="chat-edit-heading">
<form id="chat-edit-form"><h2 id="chat-edit-heading">Изменить название чата</h2>
<label id="chat-edit-label" class="chat-edit-label" for="chat-edit-name">Название чата</label>
<input id="chat-edit-name" maxlength="80" required autocomplete="off" autofocus>
<label id="chat-move-label" class="chat-edit-label" for="chat-folder-select" hidden>Проект чата</label><select id="chat-folder-select" hidden></select>
<p id="chat-edit-status" role="status" aria-live="polite"></p>
<div class="chat-dialog-actions"><button id="chat-edit-cancel" type="button">Отмена</button><button id="chat-edit-save" type="submit">Изменить</button></div>
</form></dialog></section>'''

WORKSPACE_STYLES = r'''
.sidebar { overflow-y: auto; scrollbar-width: thin; }
.chat-library { margin-top: 24px; padding: 0 4px; }
.chat-library button { font-weight: 400; }
.sidebar .chat-new { justify-content: flex-start; gap: 12px; width: 100%; min-height: 44px; padding: 10px 12px; margin-bottom: 16px; background: transparent; color: var(--ink); border-radius: 24px; font-weight: 400; }
.sidebar .chat-new:hover { background: var(--selected); }
.sidebar .chat-new:disabled { background: transparent; opacity: .5; }
.sidebar .chat-new svg { fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.chat-library-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 4px; padding-left: 12px; }
.chat-library-heading h2 { margin: 0; font-size: 13px; font-weight: 500; color: var(--muted); letter-spacing: normal; }
.chat-recents-heading { margin-top: 24px; margin-bottom: 8px; }
.chat-library .chat-icon-button { flex-shrink: 0; width: 36px; min-height: 36px; padding: 8px; border-radius: 50%; background: transparent; color: var(--muted); }
.chat-library .chat-icon-button:hover, .chat-library .chat-icon-button[aria-expanded=true] { background: var(--selected); color: var(--ink); }
.chat-library svg, .chat-action-menu svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
#chat-library-status { margin: 4px 12px; font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
#chat-library-status:empty { display: none; }
#chat-library-status[data-error=true] { color: var(--ink); }
.chat-list { display: grid; gap: 2px; list-style: none; margin: 0 0 8px; padding: 0; }
.chat-row { display: flex; align-items: center; min-width: 0; border-radius: 24px; }
.chat-row:hover, .chat-row:focus-within { background: var(--subtle); }
.chat-row:has(.chat-link[aria-current=page]) { background: var(--selected); }
.chat-link { flex: 1; min-width: 0; min-height: 40px; display: flex; align-items: center; padding: 9px 12px; border-radius: 24px; color: var(--ink); text-decoration: none; font-size: 13px; }
.chat-link span { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.chat-link[aria-current=page] { color: var(--ink); }
.chat-row > .chat-icon-button { opacity: 0; margin-right: 2px; }
.chat-row:hover > .chat-icon-button, .chat-row:focus-within > .chat-icon-button, .chat-row > .chat-icon-button[aria-expanded=true] { opacity: 1; }
.chat-folder { margin-top: 8px; }
.chat-folder-heading { display: flex; align-items: center; border-radius: 20px; }
.chat-folder-heading:hover { background: var(--subtle); }
.chat-library .chat-folder-toggle { flex: 1; min-width: 0; justify-content: flex-start; gap: 9px; min-height: 40px; padding: 9px 12px; background: transparent; color: var(--ink); border-radius: 20px; font-size: 13px; }
.chat-folder-toggle span { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.chat-folder-toggle .chat-chevron { width: 12px; height: 12px; flex-shrink: 0; transition: transform 140ms ease-out; }
.chat-folder-toggle[aria-expanded=true] .chat-chevron { transform: rotate(90deg); }
.chat-folder-heading > .chat-icon-button { opacity: 0; }
.chat-folder-heading > .chat-project-new { opacity: 1; }
.chat-folder-heading:hover > .chat-icon-button, .chat-folder-heading:focus-within > .chat-icon-button, .chat-folder-heading > .chat-icon-button[aria-expanded=true] { opacity: 1; }
.chat-folder-content { padding-left: 16px; }
.chat-folder-empty { margin: 8px 12px 12px; font-size: 12px; color: var(--muted); }
.chat-action-menu { position: fixed; inset: auto; margin: 0; padding: 6px; width: 228px; max-width: calc(100vw - 24px); background: var(--selected); color: var(--ink); border: 0; border-radius: 14px; box-shadow: 0 8px 28px #0006; }
.chat-action-menu button { width: 100%; min-height: 44px; display: flex; justify-content: flex-start; gap: 12px; padding: 10px 12px; background: transparent; color: var(--ink); border-radius: 9px; font-size: 13px; font-weight: 400; }
.chat-action-menu button:hover, .chat-action-menu button:focus-visible { background: var(--surface); }
.chat-edit-dialog { width: min(380px, calc(100vw - 32px)); max-height: calc(100dvh - 32px); padding: 24px; border: 0; border-radius: 28px; background: var(--selected); color: var(--ink); box-shadow: 0 16px 48px #0005; }
.chat-edit-dialog::backdrop { background: #0008; }
.chat-edit-dialog form { margin: 0; }
.chat-edit-dialog h2 { margin: 0 0 28px; font-size: 20px; line-height: 1.35; font-weight: 500; letter-spacing: normal; }
.chat-edit-label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.chat-edit-dialog input, .chat-edit-dialog select { box-sizing: border-box; min-width: 0; width: 100%; min-height: 48px; padding: 11px 14px; border: 1px solid var(--muted); border-radius: 14px; background: transparent; color: var(--ink); font: inherit; font-size: 15px; caret-color: var(--accent); color-scheme: dark; }
.chat-edit-dialog input:focus-visible, .chat-edit-dialog select:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.chat-edit-dialog option { background: var(--surface); color: var(--ink); }
#chat-edit-status { margin: 12px 0 0; font-size: 13px; color: var(--ink); overflow-wrap: anywhere; }
#chat-edit-status:empty { display: none; }
.chat-dialog-actions { display: flex; justify-content: flex-end; gap: 4px; margin-top: 20px; }
.chat-dialog-actions button { min-height: 40px; padding: 9px 16px; border-radius: 24px; background: transparent; color: var(--ink); font-weight: 500; }
.chat-dialog-actions button:hover { background: var(--surface); }
.chat-dialog-actions #chat-edit-save { background: var(--accent); color: #142344; }
.chat-dialog-actions #chat-edit-save:disabled { background: #373a42; color: #a2a5af; }
.chat-library button:disabled { background: transparent; opacity: .5; }
.sidebar-note { margin-top: 24px; padding-top: 12px; }
.sidebar-bottom { margin-top: auto; }
.conversation-navigation > summary { min-height: 40px; padding: 8px 10px; color: var(--muted); font-size: 12px; }
.conversation-navigation .conversation-index { max-height: 200px; margin: 0; overflow-y: auto; }
#view-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 760px), (hover: none), (pointer: coarse) { .chat-row > .chat-icon-button, .chat-folder-heading > .chat-icon-button { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .chat-folder-toggle .chat-chevron { transition: none; } }
'''

SCRIPT = r"""'use strict';
window.CampusPulseChats = class {
  constructor({api, selected, navigate, changed, blocked, session}) {
    Object.assign(this, {api, selected, navigate, changed, blocked, session});
    this.data = null; this.epoch = 0; this.mutating = false; this.openFolders = new Set(); this.pending = null; this.editing = null; this.menuTrigger = null;
    this.$ = id => document.getElementById(id);
    this.$('chat-new').addEventListener('click', () => this.change({action: 'create_chat', title: 'Новый чат', folder_id: null}));
    this.$('chat-folder-create').addEventListener('click', event => this.edit('create_folder', null, event.currentTarget));
    this.$('chat-edit-cancel').addEventListener('click', () => this.$('chat-edit-dialog').close());
    this.$('chat-edit-dialog').addEventListener('close', () => { this.editing = null; this.restoreFocus(); });
    this.$('chat-edit-name').addEventListener('input', () => this.controls());
    this.$('chat-folder-select').addEventListener('change', () => this.controls());
    this.$('chat-edit-form').addEventListener('submit', event => {
      event.preventDefault(); if (!this.editing || this.$('chat-edit-save').disabled) return;
      const {action, item} = this.editing, change = {action};
      if (item) change.item_id = item.chat_id || item.folder_id;
      if (action === 'move_chat') change.folder_id = this.$('chat-folder-select').value || null;
      else change.title = this.$('chat-edit-name').value.trim();
      this.change(change);
    });
    this.$('chat-action-menu').addEventListener('toggle', event => {
      if (event.newState === 'closed' && this.menuTrigger) this.menuTrigger.setAttribute('aria-expanded', 'false');
    });
    // Native popover owns Escape, outside-click dismissal and tab traversal.
    window.addEventListener('resize', () => this.closeMenu());
    document.querySelector('.sidebar')?.addEventListener('scroll', () => this.closeMenu(), {passive: true});
  }
  restoreFocus() {
    const original = this.editTrigger;
    const replacement = original?.dataset.actionKey && [...this.$('chat-library').querySelectorAll('[data-action-key]')].find(button => button.dataset.actionKey === original.dataset.actionKey);
    (original?.isConnected ? original : replacement || this.$('chat-new')).focus();
  }
  closeMenu() { const menu = this.$('chat-action-menu'); if (menu.matches(':popover-open')) menu.hidePopover(); }
  folderState(save = false) {
    if (!this.session()) return;
    try {
      const key = 'campuspulse-projects-v1:' + this.session();
      if (save) sessionStorage.setItem(key, JSON.stringify([...this.openFolders]));
      else {
        const ids = JSON.parse(sessionStorage.getItem(key));
        if (Array.isArray(ids)) this.openFolders = new Set(ids.filter(id => this.data.folders.some(folder => folder.folder_id === id)));
      }
    } catch { /* The navigation remains usable without browser storage. */ }
  }
  message(text, error = false) {
    this.$('chat-library-status').textContent = text; this.$('chat-library-status').dataset.error = String(error);
    if (this.$('chat-edit-dialog').open) this.$('chat-edit-status').textContent = text;
  }
  reset() {
    ++this.epoch; this.data = null; this.pending = null; this.mutating = false; this.openFolders.clear(); this.closeMenu();
    if (this.$('chat-edit-dialog').open) this.$('chat-edit-dialog').close();
    this.$('chat-library-list').replaceChildren(); this.$('chat-project-list').replaceChildren(); this.$('chat-edit-name').value = ''; this.$('chat-folder-select').replaceChildren(); this.$('chat-library').hidden = true;
  }
  controls() {
    const disabled = !this.data || this.mutating || this.blocked();
    this.$('chat-new').disabled = disabled;
    this.$('chat-library').querySelectorAll('button').forEach(button => { button.disabled = disabled && button.id !== 'chat-edit-cancel'; });
    this.$('chat-edit-name').disabled = disabled; this.$('chat-folder-select').disabled = disabled;
    if (this.editing && !disabled) {
      const {action, item} = this.editing;
      this.$('chat-edit-save').disabled = action === 'move_chat'
        ? (this.$('chat-folder-select').value || null) === item.folder_id
        : !this.$('chat-edit-name').value.trim() || this.$('chat-edit-name').value.trim() === item?.title;
    }
  }
  async load() {
    const epoch = this.epoch;
    try {
      const data = await (await this.api('/api/chat/workspace')).json(); if (epoch !== this.epoch) return;
      this.data = data; this.folderState(); this.$('chat-library').hidden = false; this.message(''); this.render();
    } catch {
      if (epoch !== this.epoch) return; this.data = null; this.$('chat-library').hidden = false;
      this.$('chat-library-list').replaceChildren(); this.message('Список чатов недоступен. Нажмите «Обновить состояние».', true); this.controls();
      this.$('chat-project-list').replaceChildren();
    }
  }
  async change(change) {
    if (!this.data || this.mutating || this.blocked()) return;
    if (this.pending && JSON.stringify(change) !== JSON.stringify(this.pending.change)) { this.message('Предыдущее изменение не подтверждено. Повторите то же действие.', true); return; }
    if (!this.pending) this.pending = {change, payload: {...change, revision: this.data.revision, request_key: crypto.randomUUID()}};
    const epoch = this.epoch, payload = this.pending.payload; this.mutating = true; this.controls(); this.message('Сохраняем…');
    try {
      const data = await (await this.api('/api/chat/workspace', payload)).json(); if (epoch !== this.epoch) return;
      this.pending = null; this.data = data;
      if (change.action === 'create_folder') this.openFolders.add(data.item_id);
      if (change.action === 'move_chat' && change.folder_id) this.openFolders.add(change.folder_id);
      this.folderState(true);
      this.message(''); this.render(); this.changed();
      if (this.$('chat-edit-dialog').open) this.$('chat-edit-dialog').close();
      if (change.action === 'create_chat') this.navigate(data.item_id);
    } catch (error) {
      if (epoch !== this.epoch) return;
      if (error.status && error.status < 500) { this.pending = null; if (error.status === 409) await this.load(); }
      this.message(error.status === 409 ? 'Список изменился или достигнут лимит: 120 чатов и 40 проектов. Проверьте список перед повтором.' : 'Изменение не подтверждено. Повторите это же действие.', true);
    } finally { if (epoch === this.epoch) { this.mutating = false; this.controls(); } }
  }
  icon(kind) {
    const paths = {more: 'M12 5h.01M12 12h.01M12 19h.01', rename: 'M4 20l5-1L20 8l-4-4L5 15ZM14 6l4 4', folder: 'M3 7V5h6l2 2h10v13H3Z', move: 'M3 7V5h6l2 2h10v13H3ZM8 14h8m-3-3 3 3-3 3', plus: 'M12 5v14M5 12h14', chevron: 'M9 5l7 7-7 7'};
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'), path = document.createElementNS(ns, 'path');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    if (kind === 'more') { svg.style.strokeWidth = '3'; svg.style.strokeLinecap = 'round'; }
    if (kind === 'chevron') svg.classList.add('chat-chevron');
    path.setAttribute('d', paths[kind]); svg.append(path); return svg;
  }
  actionButton(item, folder = false) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'chat-icon-button';
    button.dataset.actionKey = (folder ? 'folder-' : 'chat-') + (item.chat_id || item.folder_id);
    button.setAttribute('aria-label', 'Действия: ' + item.title); button.title = 'Действия';
    button.setAttribute('aria-expanded', 'false'); button.setAttribute('aria-controls', 'chat-action-menu'); button.append(this.icon('more'));
    button.addEventListener('click', () => this.menu(item, folder, button)); return button;
  }
  menu(item, folder, trigger) {
    const menu = this.$('chat-action-menu'), same = this.menuTrigger === trigger && menu.matches(':popover-open'); this.closeMenu(); if (same) return;
    this.menuTrigger = trigger; menu.replaceChildren(); menu.setAttribute('aria-label', 'Действия: ' + item.title);
    const actions = folder ? [['create_chat', 'plus', 'Новый чат в проекте'], ['rename_folder', 'rename', 'Переименовать']] : [['rename_chat', 'rename', 'Переименовать'], ['move_chat', 'move', 'Переместить в проект']];
    for (const [action, icon, title] of actions) {
      const button = document.createElement('button'); button.type = 'button'; button.append(this.icon(icon), document.createTextNode(title));
      button.addEventListener('click', () => { this.closeMenu(); if (action === 'create_chat') this.change({action, title: 'Новый чат', folder_id: item.folder_id}); else this.edit(action, item, trigger); }); menu.append(button);
    }
    menu.showPopover(); trigger.setAttribute('aria-expanded', 'true');
    const rect = trigger.getBoundingClientRect(), bounds = menu.getBoundingClientRect();
    menu.style.left = Math.max(12, Math.min(rect.right - bounds.width, window.innerWidth - bounds.width - 12)) + 'px';
    menu.style.top = Math.max(12, Math.min(rect.bottom + 4, window.innerHeight - bounds.height - 12)) + 'px';
    menu.querySelector('button').focus(); this.controls();
  }
  edit(action, item, trigger) {
    if (!this.data || this.mutating || this.blocked()) return;
    this.editing = {action, item}; this.editTrigger = trigger;
    const move = action === 'move_chat', folder = action === 'rename_folder' || action === 'create_folder';
    this.$('chat-edit-heading').textContent = move ? 'Переместить чат' : action === 'create_folder' ? 'Новый проект' : folder ? 'Изменить название проекта' : 'Изменить название чата';
    this.$('chat-edit-label').textContent = folder ? 'Название проекта' : 'Название чата';
    this.$('chat-edit-name').hidden = move; this.$('chat-edit-name').required = !move; this.$('chat-edit-name').value = item?.title || '';
    this.$('chat-edit-label').hidden = move; this.$('chat-move-label').hidden = !move; this.$('chat-folder-select').hidden = !move;
    this.$('chat-folder-select').replaceChildren(new Option('Без проекта', ''));
    for (const folder of this.data.folders) this.$('chat-folder-select').append(new Option(folder.title, folder.folder_id));
    this.$('chat-folder-select').value = move ? item.folder_id || '' : '';
    this.$('chat-edit-save').textContent = move ? 'Переместить' : action === 'create_folder' ? 'Создать' : 'Изменить';
    this.$('chat-edit-status').textContent = ''; this.controls(); this.$('chat-edit-dialog').showModal();
    if (move) this.$('chat-folder-select').focus(); else { this.$('chat-edit-name').focus(); this.$('chat-edit-name').select(); }
  }
  list(entries) {
    const list = document.createElement('ul'); list.className = 'chat-list';
    const recent = [...entries].reverse().sort((a, b) => (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0));
    for (const chat of recent) {
      const item = document.createElement('li'), link = document.createElement('a'), label = document.createElement('span'); item.className = 'chat-row';
      link.className = 'chat-link'; link.dataset.chatId = chat.chat_id; link.href = chat.chat_id === 'default' ? '#workspace' : '#chat/' + chat.chat_id;
      link.title = chat.title; label.textContent = chat.title; link.append(label); item.append(link, this.actionButton(chat)); list.append(item);
    }
    return list;
  }
  selection() {
    if (!this.data) return; const current = this.data.chats.find(chat => chat.chat_id === this.selected());
    this.$('chat-library').querySelectorAll('.chat-link').forEach(link => {
      if (link.dataset.chatId === this.selected()) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    if (current?.folder_id) {
      this.openFolders.add(current.folder_id);
      this.folderState(true);
      const folder = this.$('chat-project-list').querySelector('[data-folder-id="' + current.folder_id + '"]');
      if (folder) { folder.querySelector('.chat-folder-content').hidden = false; folder.querySelector('.chat-folder-toggle').setAttribute('aria-expanded', 'true'); }
    }
  }
  title() { return this.data?.chats.find(chat => chat.chat_id === this.selected())?.title || 'Ассистент'; }
  render() {
    if (!this.data) return; this.closeMenu();
    const container = this.$('chat-library-list'); container.replaceChildren(this.list(this.data.chats.filter(chat => !chat.folder_id)));
    const projects = this.$('chat-project-list'); projects.replaceChildren();
    if (!this.data.folders.length) {
      const empty = document.createElement('p'); empty.className = 'chat-folder-empty'; empty.textContent = 'Объедините чаты по предмету или задаче'; projects.append(empty);
    }
    for (const folder of this.data.folders) {
      const section = document.createElement('div'), heading = document.createElement('div'), toggle = document.createElement('button'), label = document.createElement('span'), content = document.createElement('div');
      section.className = 'chat-folder'; section.dataset.folderId = folder.folder_id; heading.className = 'chat-folder-heading';
      toggle.type = 'button'; toggle.className = 'chat-folder-toggle'; toggle.setAttribute('aria-expanded', String(this.openFolders.has(folder.folder_id))); toggle.setAttribute('aria-controls', 'chat-folder-' + folder.folder_id);
      label.textContent = folder.title; toggle.append(this.icon('chevron'), this.icon('folder'), label);
      const create = document.createElement('button'); create.type = 'button'; create.className = 'chat-icon-button chat-project-new';
      create.setAttribute('aria-label', 'Новый чат в проекте «' + folder.title + '»'); create.title = 'Новый чат в проекте'; create.append(this.icon('plus'));
      create.addEventListener('click', () => this.change({action: 'create_chat', title: 'Новый чат', folder_id: folder.folder_id}));
      heading.append(toggle, create, this.actionButton(folder, true));
      content.className = 'chat-folder-content'; content.id = 'chat-folder-' + folder.folder_id; content.hidden = !this.openFolders.has(folder.folder_id);
      toggle.addEventListener('click', () => { content.hidden = !content.hidden; toggle.setAttribute('aria-expanded', String(!content.hidden)); if (content.hidden) this.openFolders.delete(folder.folder_id); else this.openFolders.add(folder.folder_id); this.folderState(true); });
      const entries = this.data.chats.filter(chat => chat.folder_id === folder.folder_id);
      if (entries.length) content.append(this.list(entries));
      else { const empty = document.createElement('p'); empty.className = 'chat-folder-empty'; empty.textContent = 'Пока нет чатов'; content.append(empty); }
      section.append(heading, content); projects.append(section);
    }
    this.selection(); this.controls();
  }
};
"""
