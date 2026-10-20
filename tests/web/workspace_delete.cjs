'use strict';
// Exercise the shipped workspace controller; only browser DOM and HTTP are doubles.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.argv[2] || process.cwd();
const source = fs.readFileSync(path.join(root, 'src/campuspulse_recovery/web/assets/workspace.py'), 'utf8');
const script = source.split('SCRIPT = r"""')[1].split('"""')[0];

function harness() {
  let focused = null, requestNumber = 0;
  class Element {
    constructor(tag = 'div', id = '') {
      Object.assign(this, {tagName: tag, id, events: {}, children: [], dataset: {}, attributes: {},
        style: {}, value: '', textContent: '', hidden: false, disabled: false, open: false, isConnected: true});
      this.classList = {add: name => {this.className = (this.className || '') + ' ' + name;}};
    }
    addEventListener(name, callback) {this.events[name] = callback;}
    fire(name, event = {}) {return this.events[name]?.({currentTarget: this, preventDefault() {}, ...event});}
    append(...children) {this.children.push(...children);}
    replaceChildren(...children) {this.children = children;}
    setAttribute(name, value) {this.attributes[name] = value;}
    removeAttribute(name) {delete this.attributes[name];}
    getAttribute(name) {return this.attributes[name];}
    querySelectorAll(selector) {
      return this.children.flatMap(child => [
        ...((selector === 'button' && child.tagName === 'button') ||
          (selector.startsWith('.') && (child.className || '').split(' ').includes(selector.slice(1))) ||
          (selector === '[data-action-key]' && child.dataset.actionKey) ? [child] : []),
        ...child.querySelectorAll(selector),
      ]);
    }
    querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
    matches(selector) {return selector === ':popover-open' && this.open;}
    showPopover() {this.open = true;}
    hidePopover() {this.open = false; this.fire('toggle', {newState: 'closed'});}
    showModal() {this.open = true;}
    close() {this.open = false; this.fire('close');}
    focus() {if (!this.disabled && !this.hidden) focused = this;}
    select() {}
    getBoundingClientRect() {return {right: 200, bottom: 100, width: 228, height: 140};}
  }
  const elements = new Map();
  for (const match of source.slice(0, source.indexOf('WORKSPACE_STYLES')).matchAll(/<(\w+)[^>]*\bid="([^"]+)"/g)) {
    elements.set(match[2], new Element(match[1], match[2]));
  }
  const library = elements.get('chat-library');
  library.append(...[...elements.values()].filter(element => element !== library && element.id !== 'chat-new'));
  const calls = [], removed = [];
  function get(id) {assert(elements.has(id), `Missing shipped element ${id}`); return elements.get(id);}
  const sandbox = {
    window: {addEventListener() {}, innerWidth: 1000, innerHeight: 800},
    document: {getElementById: get, querySelector() {return null;},
      createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
      createTextNode: text => Object.assign(new Element('text'), {textContent: text})},
    Option: class extends Element {constructor(text, value) {super('option'); this.textContent = text; this.value = value;}},
    sessionStorage: {getItem() {return null;}, setItem() {}},
    crypto: {randomUUID: () => 'request-' + ++requestNumber},
  };
  vm.createContext(sandbox); vm.runInContext(script, sandbox);
  const chat = {chat_id: 'a'.repeat(32), title: 'Старый чат', folder_id: null, updated_at: null};
  const folder = {folder_id: 'b'.repeat(32), title: 'Алгоритмы'};
  let next = async () => ({json: async () => ({revision: 2, chats: [], folders: [folder]})});
  const chats = new sandbox.window.CampusPulseChats({
    api: async (url, payload) => {calls.push({url, payload}); return next(url, payload);},
    selected: () => chat.chat_id, session: () => 'test-session', blocked: () => false,
    navigate() {throw new Error('Deletion routing belongs to the removed callback');},
    changed() {}, removed: (change, data) => removed.push({change, data}),
  });
  chats.data = {revision: 1, chats: [chat], folders: [folder]};
  return {chats, chat, folder, calls, removed, get, Element,
    focused: () => focused, respond: fn => {next = fn;}, flush: () => new Promise(resolve => setImmediate(resolve))};
}

async function deleteRequiresConfirmationWithCancelFocused() {
  const h = harness(), trigger = new h.Element('button');
  h.chats.menu(h.chat, false, trigger);
  const button = h.get('chat-action-menu').children.find(child => child.children.some(node => node.textContent === 'Удалить чат'));
  assert(button, 'Chat action menu must offer deletion');
  button.fire('click');
  assert.equal(h.get('chat-edit-dialog').open, true);
  assert.equal(h.focused(), h.get('chat-edit-cancel'), 'Opening a destructive dialog must not focus its confirm button');
  assert.equal(h.get('chat-edit-name').hidden, true);
  assert.equal(h.get('chat-edit-name').required, false, 'Hidden name field must not block delete submission');
  assert.match(h.get('chat-edit-description').textContent, /восстановить/i);
  h.get('chat-edit-cancel').fire('click');
  assert.equal(h.get('chat-edit-dialog').open, false);
  assert.equal(h.calls.length, 0, 'Cancel must not issue a mutation');
}

async function projectDeletionSendsNoTitleAndRendersPreservedChats() {
  const h = harness();
  h.chat.folder_id = h.folder.folder_id;
  h.respond(async () => ({json: async () => ({revision: 2, chats: [{...h.chat, folder_id: null}], folders: []})}));
  h.chats.edit('delete_folder', h.folder, new h.Element('button'));
  assert.match(h.get('chat-edit-description').textContent, /сохранятся/i);
  h.get('chat-edit-form').fire('submit');
  await h.flush();
  assert.equal(h.calls.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0].payload)), {
    action: 'delete_folder', item_id: 'b'.repeat(32), request_key: 'request-1', revision: 1,
  });
  assert.equal(h.removed.length, 1);
  assert.equal(h.removed[0].data.chats[0].folder_id, null);
  assert.equal(h.get('chat-library-list').querySelectorAll('.chat-link').length, 1, 'Preserved project chat must appear in the common list');
  assert.equal(h.get('chat-edit-dialog').open, false);
}

async function inflightDeleteBlocksDuplicateSubmitAndCancel() {
  const h = harness();
  let resolve;
  h.respond(() => new Promise(done => {resolve = done;}));
  h.chats.edit('delete_chat', h.chat, new h.Element('button'));
  h.get('chat-edit-form').fire('submit');
  assert.equal(h.get('chat-edit-cancel').disabled, true);
  h.get('chat-edit-cancel').fire('click');
  assert.equal(h.get('chat-edit-dialog').open, true, 'A click must not dismiss a request in progress');
  let prevented = false;
  h.get('chat-edit-dialog').fire('cancel', {preventDefault() {prevented = true;}});
  assert(prevented, 'Escape must not dismiss a deletion in progress');
  h.get('chat-edit-form').fire('submit');
  assert.equal(h.calls.length, 1);
  resolve({json: async () => ({revision: 2, chats: [], folders: [h.folder]})});
  await h.flush();
  assert.equal(h.removed.length, 1);
}

async function lostResponseCanRetryAfterClosingDialogWithSameKey() {
  const h = harness();
  h.respond(async () => {throw new Error('Connection lost after server accepted deletion');});
  h.chats.edit('delete_chat', h.chat, new h.Element('button'));
  h.get('chat-edit-form').fire('submit');
  await h.flush();
  assert.equal(h.removed.length, 0, 'An ambiguous response must not remove local history');
  assert.equal(h.get('chat-edit-dialog').open, true);
  h.get('chat-edit-cancel').fire('click');
  assert.equal(h.get('chat-library-retry').hidden, false, 'Deletion recovery must remain available after dismissing its dialog');
  let resolve;
  h.respond(() => new Promise(done => {resolve = done;}));
  h.get('chat-library-retry').fire('click');
  assert.equal(h.get('chat-edit-dialog').open, true, 'Retry must keep the composer behind the modal while deletion is in progress');
  resolve({json: async () => ({revision: 2, chats: [], folders: [h.folder]})});
  await h.flush();
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[0].payload.request_key, h.calls[1].payload.request_key, 'Lost responses must reuse the original idempotency key');
  assert.equal(h.removed.length, 1);
  assert.equal(h.get('chat-library-retry').hidden, true);
  assert.equal(h.focused(), h.get('chat-new'), 'Successful retry must restore focus to a visible navigation control');
}

async function conflictReloadsBeforeExplicitRetry() {
  const h = harness();
  h.respond(async (url, payload) => {
    if (payload) {const error = new Error('Catalog changed'); error.status = 409; throw error;}
    return {json: async () => ({revision: 5, chats: [h.chat], folders: [h.folder]})};
  });
  h.chats.edit('delete_chat', h.chat, new h.Element('button'));
  h.get('chat-edit-form').fire('submit');
  await h.flush();
  assert.equal(h.calls.length, 2, 'Conflict may refresh the catalog but must not delete automatically');
  assert.equal(h.removed.length, 0);
  assert.equal(h.chats.pending, null);
  assert.equal(h.get('chat-edit-dialog').open, true);
  h.respond(async () => ({json: async () => ({revision: 6, chats: [], folders: [h.folder]})}));
  h.get('chat-edit-form').fire('submit');
  await h.flush();
  assert.equal(h.calls[2].payload.revision, 5);
  assert.notEqual(h.calls[0].payload.request_key, h.calls[2].payload.request_key);
  assert.equal(h.removed.length, 1);
}

async function deletionIsBlockedWhileVoiceOrMessageIsBusy() {
  const h = harness();
  h.chats.blocked = () => true;
  h.chats.edit('delete_chat', h.chat, new h.Element('button'));
  assert.equal(h.get('chat-edit-dialog').open, false);
  await h.chats.change({action: 'delete_chat', item_id: h.chat.chat_id});
  assert.equal(h.calls.length, 0);
}

async function resetRejectsLateDeleteResponse() {
  const h = harness();
  let resolve;
  h.respond(() => new Promise(done => {resolve = done;}));
  const pending = h.chats.change({action: 'delete_chat', item_id: 'default'});
  h.chats.reset();
  resolve({json: async () => ({revision: 2, chats: [], folders: [h.folder]})});
  await pending;
  assert.equal(h.removed.length, 0, 'A previous account response must not alter the current account');
  assert.equal(h.chats.data, null);
}

(async () => {
  for (const test of [deleteRequiresConfirmationWithCancelFocused, projectDeletionSendsNoTitleAndRendersPreservedChats,
    inflightDeleteBlocksDuplicateSubmitAndCancel, lostResponseCanRetryAfterClosingDialogWithSameKey,
    conflictReloadsBeforeExplicitRetry, deletionIsBlockedWhileVoiceOrMessageIsBusy, resetRejectsLateDeleteResponse]) {
    await test(); process.stdout.write('PASS ' + test.name + '\n');
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
