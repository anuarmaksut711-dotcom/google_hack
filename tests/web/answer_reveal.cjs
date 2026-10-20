// Execute the shipped reveal controller with a clock and a minimal DOM boundary.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.argv[2];
const source = fs.readFileSync(path.join(root, 'src/campuspulse_recovery/web/assets/answer_reveal.py'), 'utf8');
const script = source.match(/SCRIPT = r"""([\s\S]*?)"""/)[1];

function harness({reduced = false, value = 'Первый абзац. Екінші абзац 👩‍💻 без потери текста.'} = {}) {
  const frames = new Map(), listeners = new Map(), media = {matches: reduced};
  let nextFrame = 0;
  const element = parent => ({parentElement: parent, hidden: false, isConnected: true,
    attributes: {}, classList: {add() {}, remove() {}},
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; }});
  const answer = element(null), paragraph = element(answer), list = element(answer), entry = element(list);
  const bold = element(entry), first = {data: value, parentElement: paragraph};
  const second = {data: 'Список с выделением.', parentElement: bold};
  answer.querySelectorAll = () => [paragraph, list, entry];
  const log = element(null);
  const scrollport = {scrollHeight: 600, clientHeight: 200, scrollTop: 400, calls: [],
    scrollTo(options) { this.scrollTop = options.top; this.calls.push(options); }};
  const document = {hidden: false,
    addEventListener(name, listener) { listeners.set(name, listener); },
    removeEventListener(name) { listeners.delete(name); },
    createTreeWalker() { let index = -1; const nodes = [first, second]; return {
      nextNode() { this.currentNode = nodes[++index]; return !!this.currentNode; }}; }};
  const sandbox = {window: {matchMedia: () => media}, document, NodeFilter: {SHOW_TEXT: 4},
    requestAnimationFrame(callback) { const id = ++nextFrame; frames.set(id, callback); return id; },
    cancelAnimationFrame(id) { frames.delete(id); }};
  vm.runInNewContext(script, sandbox);
  const controller = new sandbox.window.CampusPulseAnswerReveal();
  return {controller, answer, first, second, paragraph, list, entry, log, scrollport, frames, document, media, listeners,
    start() { controller.start(answer, scrollport, log); },
    frame(time) { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(callback => callback(time)); }};
}

{
  const h = harness(), original = h.first.data + h.second.data;
  h.start(); assert.equal(h.first.data, ''); assert(h.paragraph.hidden && h.entry.hidden);
  h.frame(0); assert(h.first.data.length > 0 && h.first.data.length < original.length);
  assert.equal(h.paragraph.hidden, false); assert.equal(h.entry.hidden, true);
  assert.equal(h.log.attributes['aria-busy'], 'true');
  h.frame(500); const partial = h.first.data + h.second.data;
  assert.equal(/\p{Surrogate}/u.test(partial), false, 'Unicode must remain valid');
  h.frame(7000); assert.equal(h.first.data + h.second.data, original);
  assert.equal(h.entry.hidden, false); assert.equal(h.log.attributes['aria-busy'], undefined);
  assert.equal(h.frames.size, 0); assert.equal(h.listeners.size, 0);
  assert.equal(h.second.parentElement.parentElement, h.entry, 'Existing formatting nodes must be preserved');
}
{
  const h = harness({reduced: true}), original = h.first.data;
  h.start(); assert.equal(h.first.data, original); assert.equal(h.frames.size, 0);
}
{
  const h = harness(); h.scrollport.scrollTop = 0; h.start(); h.frame(0); h.frame(200);
  assert.equal(h.scrollport.calls.length, 0, 'Reading earlier messages must not be interrupted');
  h.controller.finish(); assert.equal(h.scrollport.calls.length, 0);
}
{
  const h = harness(), original = h.first.data;
  h.start(); h.frame(0); h.document.hidden = true; h.listeners.get('visibilitychange')();
  assert.equal(h.first.data, original); assert.equal(h.frames.size, 0);
}
{
  const h = harness(), original = h.first.data;
  h.start(); h.frame(0); h.answer.isConnected = false; h.frame(20);
  assert.equal(h.first.data, original); assert.equal(h.frames.size, 0);
  h.answer.isConnected = true; h.start(); h.frame(0); h.media.matches = true; h.frame(20);
  assert.equal(h.first.data, original); assert.equal(h.frames.size, 0);
}
{
  const h = harness({value: 'Слово '.repeat(2000)}), original = h.first.data;
  h.start(); h.frame(0); h.frame(6500); assert.equal(h.first.data, original);
  h.start(); h.frame(0); h.start(); h.frame(0); h.controller.finish();
  assert.equal(h.first.data, original); assert.equal(h.frames.size, 0);
}
console.log('PASS: gradual text, formatting, Unicode, reduced motion, scrolling, cancellation and bounded duration');
