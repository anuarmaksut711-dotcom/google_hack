const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(process.argv[2], 'src/campuspulse_recovery/web/assets/client.py'), 'utf8');
const section = source.slice(source.indexOf('  const views = {'), source.indexOf('  function focusPanel()'));
const nodes = new Map();
function element() {
  return {textContent:'', setAttribute(){}, classList:{toggle(){}}, dataset:{}};
}
const $ = id => {if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id);};
const sandbox = {$, URLSearchParams, location:{hash:'#panel', search:''},
  document:{body:{dataset:{}}, getElementById:()=> null, querySelectorAll:()=>[], title:''},
  chats:{title:()=> 'Ассистент'}, activeChat:'default', chatHash:()=> '#workspace',
  requestAnimationFrame(){}, resizeComposer(){}};
vm.createContext(sandbox);
vm.runInContext(section + '\nupdateNavigation();', sandbox);
assert.equal($('view-title').textContent, 'Предложенное расписание');
assert.equal(sandbox.document.title, 'Google AI · Предложенное расписание');
assert.equal(sandbox.document.body.dataset.view, 'plan');
console.log('Unified schedule navigation passed');
