"""Chat navigation uses native links, disclosures, forms and selects."""
CHAT_NAV = '''<section id="chat-library" class="chat-library" aria-labelledby="chat-library-heading" hidden>
<div class="chat-library-heading"><h2 id="chat-library-heading">Чаты</h2><button id="chat-new" type="button" disabled>Новый чат</button></div>
<details id="chat-folder-create"><summary>Новая папка</summary><form id="chat-folder-form">
<label for="chat-folder-name">Название папки</label><input id="chat-folder-name" maxlength="80" required autocomplete="off"><button type="submit">Создать папку</button></form></details>
<p id="chat-library-status" role="status" aria-live="polite"></p><div id="chat-library-list" aria-label="Ваши чаты"></div>
<details id="chat-settings"><summary>Настройки чата</summary><form id="chat-rename-form">
<label for="chat-name">Название чата</label><input id="chat-name" maxlength="80" required autocomplete="off"><button type="submit">Переименовать</button></form>
<form id="chat-move-form"><label for="chat-folder-select">Папка чата</label><select id="chat-folder-select"></select><button type="submit">Переместить</button></form></details></section>'''

WORKSPACE_STYLES = r'''
.sidebar { overflow-y: auto; scrollbar-width: thin; }
.chat-library { margin-top: 24px; padding: 0 4px; }
.chat-library-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.chat-library-heading h2 { margin: 0; padding-left: 10px; font-size: 13px; letter-spacing: normal; }
.chat-library button { min-height: 36px; padding: 7px 10px; font-size: 12px; }
.chat-library-heading button { color: var(--muted); background: transparent; }
.chat-library-heading button:hover { color: var(--ink); background: var(--selected); }
.chat-library summary { min-height: 40px; padding: 8px 10px; font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.chat-library summary:hover { background: var(--selected); border-radius: 8px; color: var(--ink); }
.chat-library form { display: grid; gap: 8px; padding: 8px 10px 12px; }
.chat-library label { font-size: 12px; color: var(--muted); }
.chat-library input, .chat-library select { min-width: 0; width: 100%; min-height: 38px; padding: 7px 9px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--ink); font: inherit; font-size: 13px; caret-color: var(--accent); }
.chat-library input:focus-visible, .chat-library select:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }
.chat-library input:hover, .chat-library select:hover { border-color: var(--muted); }
#chat-library-status { margin: 4px 10px; font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
#chat-library-status:empty { display: none; }
#chat-library-status[data-error=true] { color: #963f33; }
.chat-list { display: grid; gap: 2px; list-style: none; margin: 4px 0 10px; padding: 0; }
.chat-link { display: flex; align-items: center; gap: 9px; min-height: 40px; padding: 8px 10px; border-radius: 8px; color: var(--muted); text-decoration: none; font-size: 13px; }
.chat-link svg, .chat-folder > summary svg { width: 17px; height: 17px; vertical-align: middle; }
.chat-link span { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.chat-link:hover { color: var(--ink); background: var(--selected); }
.chat-link[aria-current=page] { color: var(--ink); background: var(--selected); font-weight: 600; }
.chat-folder > summary { font-size: 13px; color: var(--ink); }
.chat-folder > summary svg { margin-right: 6px; }
.chat-folder .chat-list, .chat-folder-settings { padding-left: 14px; }
.chat-folder-empty { margin: 4px 10px 8px 26px; font-size: 12px; color: var(--muted); }
.sidebar-note { margin-top: 24px; padding-top: 12px; }
.sidebar-bottom { margin-top: auto; }
#view-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
'''

SCRIPT = r"""'use strict';
window.CampusPulseChats = class {
  constructor({api, selected, navigate, changed, blocked}) {
    Object.assign(this, {api, selected, navigate, changed, blocked});
    this.data = null; this.epoch = 0; this.mutating = false; this.openFolders = new Set(); this.pending = null;
    this.$ = id => document.getElementById(id);
    this.$('chat-new').addEventListener('click', () => this.change({action: 'create_chat', title: 'Новый чат', folder_id: null}));
    this.form('chat-folder-form', () => ({action: 'create_folder', title: this.$('chat-folder-name').value.trim()}));
    this.form('chat-rename-form', () => ({action: 'rename_chat', item_id: this.selected(), title: this.$('chat-name').value.trim()}));
    this.form('chat-move-form', () => ({action: 'move_chat', item_id: this.selected(), folder_id: this.$('chat-folder-select').value || null}));
  }
  form(id, value) { this.$(id).addEventListener('submit', event => { event.preventDefault(); const change = value(); if (change.title !== '') this.change(change); }); }
  message(text, error = false) { this.$('chat-library-status').textContent = text; this.$('chat-library-status').dataset.error = String(error); }
  reset() {
    ++this.epoch; this.data = null; this.pending = null; this.mutating = false; this.openFolders.clear();
    this.$('chat-library-list').replaceChildren(); this.$('chat-name').value = ''; this.$('chat-folder-name').value = '';
    this.$('chat-folder-select').replaceChildren(); this.$('chat-library').hidden = true;
  }
  controls() { const disabled = !this.data || this.mutating || this.blocked(); this.$('chat-library').querySelectorAll('button').forEach(button => { button.disabled = disabled; }); }
  async load() {
    const epoch = this.epoch;
    try {
      const data = await (await this.api('/api/chat/workspace')).json(); if (epoch !== this.epoch) return;
      this.data = data; this.$('chat-library').hidden = false; this.message(''); this.render();
    } catch {
      if (epoch !== this.epoch) return; this.data = null; this.$('chat-library').hidden = false;
      this.$('chat-library-list').replaceChildren(); this.message('Список чатов недоступен. Нажмите «Обновить состояние».', true); this.controls();
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
      if (change.action === 'create_folder') { this.openFolders.add(data.item_id); this.$('chat-folder-form').reset(); this.$('chat-folder-create').open = false; }
      if (change.action === 'move_chat' && change.folder_id) this.openFolders.add(change.folder_id);
      this.message(''); this.render(); this.changed();
      if (change.action === 'create_chat') this.navigate(data.item_id);
    } catch (error) {
      if (epoch !== this.epoch) return;
      if (error.status && error.status < 500) { this.pending = null; if (error.status === 409) await this.load(); }
      this.message(error.status === 409 ? 'Список изменился или достигнут лимит: 120 чатов и 40 папок. Проверьте список перед повтором.' : 'Изменение не подтверждено. Повторите это же действие.', true);
    } finally { if (epoch === this.epoch) { this.mutating = false; this.controls(); } }
  }
  icon(folder = false) {
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'), path = document.createElementNS(ns, 'path');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    path.setAttribute('d', folder ? 'M3 7V5h6l2 2h10v13H3Z' : 'M4 4h16v13H8l-4 4Z'); svg.append(path); return svg;
  }
  list(entries) {
    const list = document.createElement('ul'); list.className = 'chat-list';
    for (const chat of entries) {
      const item = document.createElement('li'), link = document.createElement('a'), label = document.createElement('span');
      link.className = 'chat-link'; link.dataset.chatId = chat.chat_id; link.href = chat.chat_id === 'default' ? '#workspace' : '#chat/' + chat.chat_id;
      link.title = chat.title; label.textContent = chat.title; link.append(this.icon(), label); item.append(link); list.append(item);
    }
    return list;
  }
  selection() {
    if (!this.data) return; const current = this.data.chats.find(chat => chat.chat_id === this.selected());
    this.$('chat-settings').hidden = !current;
    if (current) { this.$('chat-name').value = current.title; this.$('chat-folder-select').value = current.folder_id || ''; }
    this.$('chat-library-list').querySelectorAll('.chat-link').forEach(link => {
      if (link.dataset.chatId === this.selected()) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    const folder = current?.folder_id && this.$('chat-library-list').querySelector('[data-folder-id="' + current.folder_id + '"]'); if (folder) folder.open = true;
  }
  title() { return this.data?.chats.find(chat => chat.chat_id === this.selected())?.title || 'Ассистент'; }
  render() {
    if (!this.data) return;
    const container = this.$('chat-library-list'), select = this.$('chat-folder-select');
    container.replaceChildren(this.list(this.data.chats.filter(chat => !chat.folder_id))); select.replaceChildren(new Option('Без папки', ''));
    for (const folder of this.data.folders) {
      const details = document.createElement('details'), summary = document.createElement('summary');
      details.className = 'chat-folder'; details.dataset.folderId = folder.folder_id; details.open = this.openFolders.has(folder.folder_id);
      details.addEventListener('toggle', () => { if (details.open) this.openFolders.add(folder.folder_id); else this.openFolders.delete(folder.folder_id); });
      summary.append(this.icon(true), document.createTextNode(folder.title)); details.append(summary);
      const entries = this.data.chats.filter(chat => chat.folder_id === folder.folder_id);
      if (entries.length) details.append(this.list(entries));
      else { const empty = document.createElement('p'); empty.className = 'chat-folder-empty'; empty.textContent = 'Пока нет чатов'; details.append(empty); }
      const settings = document.createElement('details'), heading = document.createElement('summary'), form = document.createElement('form'),
        input = document.createElement('input'), button = document.createElement('button'), label = document.createElement('label');
      settings.className = 'chat-folder-settings'; heading.textContent = 'Переименовать папку'; input.id = 'folder-name-' + folder.folder_id;
      input.maxLength = 80; input.required = true; input.value = folder.title; label.htmlFor = input.id; label.textContent = 'Название папки';
      button.type = 'submit'; button.textContent = 'Сохранить'; form.append(label, input, button);
      form.addEventListener('submit', event => { event.preventDefault(); const title = input.value.trim(); if (title) this.change({action: 'rename_folder', item_id: folder.folder_id, title}); });
      settings.append(heading, form); details.append(settings); container.append(details); select.append(new Option(folder.title, folder.folder_id));
    }
    this.selection(); this.controls();
  }
};
"""
