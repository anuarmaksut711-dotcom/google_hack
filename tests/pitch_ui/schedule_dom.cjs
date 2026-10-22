'use strict';

// A deliberately small DOM for the shipped timetable script. Appending an
// existing node moves it, and focus belongs to one document element, as in a browser.
function createDocument() {
  const nodes = new Map();
  const focusListeners = new Set();
  const document = {activeElement: null, cookie: '',
    addEventListener(type, listener) { if (type === 'focusin') focusListeners.add(listener); },
    removeEventListener(type, listener) { if (type === 'focusin') focusListeners.delete(listener); }};
  const focused = target => {
    document.activeElement = target;
    for (const listener of [...focusListeners]) listener({type: 'focusin', target});
  };
  const dataKey = name => name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
  function matches(node, selector) {
    if (selector.startsWith('.')) return node.className.split(/\s+/).includes(selector.slice(1));
    if (selector.startsWith('#')) return node.id === selector.slice(1);
    if (selector === '[hidden]') return node.hidden;
    const data = selector.match(/^\[data-([a-z-]+)(?:=["']([^"']*)["'])?\]$/);
    if (data) return Object.hasOwn(node.dataset, dataKey(data[1])) &&
      (data[2] === undefined || String(node.dataset[dataKey(data[1])]) === data[2]);
    return node.tagName.toLowerCase() === selector.toLowerCase();
  }
  class Element {
    constructor(tag = 'div') {
      Object.assign(this, {tagName: tag.toUpperCase(), children: [], listeners: {}, dataset: {},
        value: '', disabled: false, hidden: false, checked: false, files: [], className: '', attrs: {}});
      this._text = '';
    }
    get parentElement() { return this.parent || null; }
    get isConnected() { return this === document.body || Boolean(this.parent?.isConnected); }
    get disabled() { return this._disabled; }
    set disabled(value) {
      this._disabled = Boolean(value);
      if (value && document.body && this.contains(document.activeElement)) focused(document.body);
    }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this.replaceChildren(); this._text = String(value); }
    get classList() {
      const element = this;
      return {contains: value => element.className.split(/\s+/).includes(value),
        add(...values) { element.className = [...new Set([...element.className.split(/\s+/).filter(Boolean), ...values])].join(' '); },
        remove(...values) { element.className = element.className.split(/\s+/).filter(value => !values.includes(value)).join(' '); },
        toggle(value, force) { const enabled = force ?? !this.contains(value); enabled ? this.add(value) : this.remove(value); return enabled; }};
    }
    append(...items) {
      for (const item of items) {
        if (item.parent) item.remove();
        item.parent = this; this.children.push(item);
      }
    }
    appendChild(item) { this.append(item); return item; }
    replaceChildren(...items) {
      this.children.forEach(child => { child.parent = null; }); this.children = []; this._text = ''; this.append(...items);
    }
    addEventListener(type, fn) { this.listeners[type] = fn; }
    setAttribute(name, value) {
      this.attrs[name] = String(value);
      if (name.startsWith('data-')) this.dataset[dataKey(name.slice(5))] = String(value);
      if (name === 'class') this.className = String(value);
      if (name === 'id') this.id = String(value);
    }
    getAttribute(name) { return this.attrs[name] ?? null; }
    removeAttribute(name) { delete this.attrs[name]; }
    focus() {
      for (let parent = this; parent; parent = parent.parent) {
        if (parent.hidden || parent.disabled) return;
      }
      if (this.isConnected) focused(this);
    }
    remove() {
      if (document.body && this.contains(document.activeElement)) focused(document.body);
      if (this.parent) this.parent.children = this.parent.children.filter(item => item !== this);
      this.parent = null;
    }
    querySelectorAll(selector) {
      const all = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
      return selector === '*' ? all : all.filter(child => matches(child, selector));
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return matches(this, selector) ? this : this.parent?.closest(selector) || null; }
    contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  }
  document.body = new Element('body');
  document.activeElement = document.body;
  const $ = id => {
    if (!nodes.has(id)) {
      const tag = id === 'class-schedule' ? 'form' : id === 'classes-fields' ? 'fieldset' :
        ['classes-save', 'classes-import', 'class-add', 'classes-calendar', 'classes-reload'].includes(id) ? 'button' : 'div';
      const node = new Element(tag); node.id = id; nodes.set(id, node);
    }
    return nodes.get(id);
  };
  document.body.append($('class-schedule'), $('classes-status'), $('classes-calendar-review'), $('classes-reload'), $('sidebar-link'));
  $('class-schedule').append($('classes-fields'));
  $('classes-fields').append(...['classes-file', 'classes-import', 'classes-from', 'classes-until',
    'classes-empty', 'classes-rows', 'class-add', 'classes-save', 'classes-calendar'].map($));
  Object.assign(document, {getElementById: $, createElement: tag => new Element(tag)});
  const fire = (node, event, target = node) => {
    if (typeof node.listeners[event] !== 'function') throw Error(`${node.id || node.className || node.tagName}: missing ${event} listener`);
    return node.listeners[event]({preventDefault() {}, target, type: event});
  };
  const rows = () => $('classes-rows').querySelectorAll('[data-class-row]');
  const field = (row, name) => row.querySelectorAll('[data-field]').find(input => input.dataset.field === name);
  const values = () => rows().map(row => Object.fromEntries(row.querySelectorAll('[data-field]').map(input =>
    [input.dataset.field, input.dataset.field === 'weekday' ? Number(input.value) : input.value])));
  return {document, $, fire, rows, field, values};
}

module.exports = {createDocument};
