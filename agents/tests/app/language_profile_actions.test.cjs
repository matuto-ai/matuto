'use strict';

const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {};
    this.attributes = {}; this.value = ''; this.text = ''; this.parentNode = null;
  }
  set className(value) { this.setAttribute('class', value); }
  get className() { return this.getAttribute('class') || ''; }
  set textContent(value) { this.replaceChildren(); this.text = String(value); }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } }
  replaceChildren(...children) { for (const child of this.children) child.parentNode = null; this.text = ''; this.children = []; this.append(...children); }
  after(node) {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children, index = siblings.indexOf(this) + 1;
    node.parentNode = this.parentNode; siblings.splice(index, 0, node);
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this);
    this.parentNode = null;
  }
  querySelector(selector) { return this.children.find(child => child.className.split(/\s+/).includes(selector.slice(1))) || null; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  async click() { for (const callback of this.listeners.click || []) await callback({}); }
}

const page = new Element('div'), profileList = new Element('div');
const elements = {languageProfiles: page, languageProfileList: profileList};
page.append(profileList);
const document = {
  getElementById(id) { return elements[id] || null; },
  createElement(tag) { return new Element(tag); },
  querySelector() { return {content: 'csrf-fixture'}; },
};
function profileRow(profile) {
  const row = new Element('article');
  row.setAttribute('data-profile-id', profile.profile_id);
  profileList.append(row);
  return row;
}
const candidate = {profile_id: 'candidate', name: 'Candidate', alias: 'candidate-alias', published: false,
  qualification: {state: 'candidate'}, available: true, remove_reason: '', unpublish_reason: ''};
let profiles = [candidate, {profile_id: 'live', name: 'Live', alias: 'live-alias', published: true,
  qualification: {state: 'qualified'}, available: true, remove_reason: 'Published profile; unpublish before removing.',
  unpublish_reason: 'Stop serving on Overview before changing it.'},
  {profile_id: 'stale', name: 'Stale qualified', alias: 'stale-alias', published: false,
    qualification: {state: 'qualified'}, available: true, remove_reason: '', unpublish_reason: ''}];
for (const profile of profiles) profileRow(profile);
const calls = [];
let popup;
const fetch = async (_url, options = {}) => {
  if (!options.body) return {ok: true, json: async () => ({profiles})};
  const body = JSON.parse(options.body); calls.push(body);
  if (body.action === 'publish' && body.profile_id === 'stale' && !body.confirmation)
    return {ok: false, json: async () => ({error: 'Stale qualified is not qualified.'})};
  if (body.action === 'duplicate' && body.alias === 'occupied')
    return {ok: false, json: async () => ({error: 'The alias is already published.'})};
  return {ok: true, json: async () => ({profile_id: body.profile_id || body.new_id})};
};
const listeners = {};
const window = {
  matutoPopup: {open(options) { popup = options; }},
  addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
  dispatchEvent(event) { for (const callback of listeners[event.type] || []) callback(event); },
};
const reasonTexts = new WeakMap();
let reasonId = 0;
function reason(control, text) {
  if (!text) { control.setAttribute('aria-disabled', 'false'); reasonTexts.delete(control); return; }
  assert.ok(control.parentNode, 'reason() requires the control to be attached first');
  const node = document.createElement('span');
  node.setAttribute('id', `test-reason-${++reasonId}`); node.textContent = text;
  control.after(node);
  control.setAttribute('aria-describedby', node.getAttribute('id'));
  control.setAttribute('aria-disabled', 'true'); reasonTexts.set(control, node);
}
class BrowserEvent { constructor(type) { this.type = type; } }
const source = readFileSync(path.join(__dirname, '../../../agents/matuto/static/language-profile-actions.js'), 'utf8');
vm.runInNewContext(source, {document, window, fetch, reason, Event: BrowserEvent});

const tick = () => new Promise(resolve => setImmediate(resolve));
const row = id => profileList.children.find(item => item.getAttribute('data-profile-id') === id);
const actions = id => row(id).querySelector('.language-profile-record-actions');
(async () => {
  await tick();
  const candidateActions = actions('candidate');
  await candidateActions.children.find(item => item.textContent === 'Publish…').click();
  assert.equal(popup.title, 'Publish a candidate?');
  assert.match(popup.content.textContent, /not qualified.*candidate-alias.*starts and loads nothing/);
  await popup.confirmAction();
  assert.equal(calls.at(-1).confirmation, 'Publish Candidate as not qualified');

  const liveActions = actions('live');
  const unpublish = liveActions.children[1], remove = liveActions.children[3];
  for (const [control, expected] of [[unpublish, /Stop serving on Overview/], [remove, /unpublish before removing/]]) {
    const description = liveActions.children[liveActions.children.indexOf(control) + 1];
    assert.equal(control.getAttribute('aria-disabled'), 'true');
    assert.equal(control.getAttribute('aria-describedby'), description.getAttribute('id'));
    assert.match(description.textContent, expected);
    assert.equal(reasonTexts.get(control), description);
  }
  const beforeRefusedClick = calls.length;
  await unpublish.click();
  assert.equal(calls.length, beforeRefusedClick);

  await candidateActions.children[0].click();
  assert.equal(popup.title, 'Duplicate Candidate');
  assert.deepEqual(popup.content.children.map(field => field.textContent.split('')[0]), ['N', 'N', 'A']);
  const [id, name, alias] = popup.content.children.map(field => field.children[0]);
  id.value = 'candidate-copy'; name.value = 'Candidate copy'; alias.value = 'occupied';
  await assert.rejects(popup.confirmAction, /alias is already published/);
  assert.deepEqual([id.value, name.value, alias.value], ['candidate-copy', 'Candidate copy', 'occupied']);
  alias.value = 'candidate-copy-alias';
  await popup.confirmAction();
  assert.deepEqual(calls.at(-1), {action: 'duplicate', profile_id: 'candidate', new_id: 'candidate-copy',
    name: 'Candidate copy', alias: 'candidate-copy-alias'});
  assert.match(page.children[1].textContent, /Created unpublished copy/);

  await actions('stale').children[1].click();
  await tick();
  assert.equal(popup.title, 'Publish a candidate?');
  await popup.confirmAction();
  assert.equal(calls.at(-1).confirmation, 'Publish Stale qualified as not qualified');
  console.log('PASS: language profile actions attach to the existing list and explain refused controls');
})().catch(error => { console.error(error); process.exitCode = 1; });
