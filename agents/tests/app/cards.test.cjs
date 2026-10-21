#!/usr/bin/env node
// Exercise Cards form routing, no-op/save/review behavior at its browser boundary.
'use strict';

const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert/strict');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {};
    this.attributes = {}; this.dataset = {}; this.hidden = false; this.value = '';
    this.text = ''; this.id = '';
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent || '').join(''); }
  append(...items) {
    for (const item of items) {
      if (item.parentNode?.children) item.parentNode.children = item.parentNode.children.filter(child => child !== item);
      this.children.push(item); item.parentNode = this;
    }
  }
  insertBefore(item, before) {
    if (item.parentNode?.children) item.parentNode.children = item.parentNode.children.filter(child => child !== item);
    const index = before ? this.children.indexOf(before) : -1;
    if (index < 0) this.children.push(item); else this.children.splice(index, 0, item);
    item.parentNode = this;
  }
  replaceChildren(...items) { this.children = items; this.text = ''; for (const item of items) item.parentNode = this; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  async fire(name, event = {}) { for (const handler of this.listeners[name] || []) await handler(event); }
  setAttribute(name, value) { this.attributes[name] = value; }
  get lastChild() { return this.children.at(-1); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(item => item !== this); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { this.focused = true; }
  close() { this.open = false; return this.fire('close'); }
  scrollIntoView() {}
  dispatchEvent(event) { return this.fire(event.type, event); }
}

async function main() {
  const fieldNames = ['card_id', 'name', 'gpu_identity', 'vram_ceiling_bytes', 'default_gpu_layers',
    'layer_count', 'smoke_output_tokens', 'bench_prompt_tokens', 'bench_generation_tokens',
    'bench_repetitions', 'notes'];
  const controls = Object.fromEntries(fieldNames.map(name => [name, new Element('input')]));
  const formHome = new Element('div');
  const form = new Element('form'); form.parentNode = formHome; formHome.children.push(form);
  form.elements = {namedItem: name => controls[name]};
  const ids = Object.fromEntries(['cards-page', 'cards-form', 'cards-error', 'cards-form-error',
    'cards-save', 'cards-add', 'cards-list', 'cards-detection', 'cards-form-title',
    'cards-layer-count-label', 'cards-cancel'].map(id => [id, new Element()]));
  ids['cards-form'] = form;
  const popupDialog = new Element('dialog');
  const document = {getElementById: id => ids[id] || null,
    querySelector: selector => selector === 'dialog.popup' ? popupDialog : {content: 'csrf-fixture'},
    createElement: tag => new Element(tag)};
  const card = {card_id: 'card-r9', name: 'Radeon R9700', gpu_identity: '0000:03:00.0',
    gpu_name: 'Radeon R9700', detected: false, vram_ceiling_bytes: 32000,
    default_gpu_layers: 99, smoke_output_tokens: 16, bench_prompt_tokens: 4096,
    bench_generation_tokens: 512, bench_repetitions: 5, notes: ''};
  const inventory = {state: 'completed', cards: [card], devices: [
    {pciAddress: '0000:04:00.0', displayName: 'Radeon Other', vramBytes: 16000000000, offered: true}], holders: {}};
  const calls = []; const popups = []; let refuseDirectSave = false; let refuseConfirmedSave = false;
  global.document = document;
  global.Event = class { constructor(type) { this.type = type; } };
  global.window = {matutoReason: (control, message) => {
    if (message) control.setAttribute('aria-disabled', 'true');
    else control.removeAttribute('aria-disabled');
  }, matutoPopup: {open: options => { popups.push(options); popupDialog.open = true; }}};
  global.fetch = async (_url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    if (body) calls.push(body);
    if (body?.action === 'save' && !body.confirm && refuseDirectSave)
      return {ok: false, json: async () => ({error: 'Stop serving before editing this card.'})};
    if (body?.action === 'save' && body.confirm && refuseConfirmedSave)
      return {ok: false, json: async () => ({error: 'Stop serving before editing this card.'})};
    const result = !body ? inventory : body.action === 'preview' ? {
      preview: {kind: 'served-change', changed_values: [
        {field: 'default_gpu_layers', label: 'Default GPU layers', old: 99, new: 128}],
        affected: [{kind: 'language', id: 'profile-1', name: 'Writer',
          state: {state: 'candidate', changed_since_qualification: true}},
        {kind: 'bundle', id: 'bundle-1', name: 'Studio voice',
          state: {state: 'candidate', changed_since_qualification: false}}]},
      review_token: 'review-1'} : {card: {...card, default_gpu_layers: 128}};
    return {ok: true, json: async () => result};
  };
  vm.runInThisContext(fs.readFileSync(path.resolve(__dirname, '../../matuto/static/cards.js'), 'utf8'),
    {filename: 'cards.js'});
  await new Promise(resolve => setImmediate(resolve));
  let actions = ids['cards-list'].children[0].children.at(-1).children;
  assert.equal(actions[0].getAttribute('aria-disabled'), null,
    'an undetected card remains editable for measurement and notes');
  await actions[0].fire('click');
  controls.name.value = 'Radeon draft'; await form.fire('input'); refuseDirectSave = true;
  await form.fire('submit', {preventDefault() {}});
  assert.match(popups[0].content.textContent, /Stop serving before editing this card/);
  assert.equal(popups[0].content.children.includes(form), true,
    'a refused direct edit keeps the form values and reason in a popup');
  await popupDialog.close();
  assert.equal(ids['cards-save'].focused, true, 'closing the refusal returns focus to the restored Save opener');
  refuseDirectSave = false; calls.length = 0;
  assert.equal(form.parentNode, formHome, 'a cancelled save returns the form to the Cards page');
  controls.name.value = card.name; await form.fire('input');
  actions = ids['cards-list'].children[0].children.at(-1).children;
  assert.equal(controls.gpu_identity.value, card.gpu_identity,
    'an edit preserves the stored but currently unavailable GPU identity');
  assert.equal(ids['cards-save'].hidden, true, 'unchanged edit offers no Save');
  controls.default_gpu_layers.value = 'number';
  controls.layer_count.value = '128';
  await form.fire('change');
  assert.equal(ids['cards-save'].textContent, 'Review…');
  assert.equal(ids['cards-save'].hidden, false);
  await form.fire('submit', {preventDefault() {}});
  assert.equal(calls[0].action, 'preview');
  assert.equal(popups.length, 2);
  assert.match(popups[1].content.textContent, /Default GPU layers: 99 → 128/);
  assert.match(popups[1].content.textContent, /language: Writer \(profile-1\) — candidate — changed since qualification/);
  assert.match(popups[1].content.textContent, /bundle: Studio voice \(bundle-1\) — candidate/);
  controls.layer_count.value = '99'; await form.fire('input');
  assert.equal(ids['cards-save'].hidden, true,
    'restoring the original values closes the review instead of offering Save');
  assert.equal(form.dataset.inPopup, undefined, 'a dismissed review returns the form to the Cards page');
  assert.equal(form.parentNode, formHome, 'review captures the original parent before moving the form');
  controls.layer_count.value = '128'; await form.fire('input');
  await form.fire('submit', {preventDefault() {}});
  assert.equal(popups.length, 3);
  inventory.cards = [];
  const formValues = popups[2].content.children.at(-1);
  assert.equal(formValues.children.includes(form), true,
    'the review popup keeps the edit form values with its consequence summary');
  refuseConfirmedSave = true;
  await assert.rejects(popups[2].confirmAction(), /Stop serving before editing this card/);
  assert.equal(popups.length, 3, 'a refused confirmation does not replace the review popup');
  assert.equal(formValues.open, true, 'a refused confirmation opens the preserved form values');
  refuseConfirmedSave = false;
  await popups[2].confirmAction();
  await popupDialog.close();
  assert.equal(calls.at(-1).action, 'save');
  assert.equal(calls.at(-1).confirm, true);
  assert.equal(calls.at(-1).review_token, 'review-1');
  assert.equal(form.dataset.mode, 'add', 'an empty Cards section opens the Add card form');
  assert.equal(controls.vram_ceiling_bytes.value, 16000000000, 'new cards default to detected VRAM');

  console.log('PASS: Cards offers no no-op Save, reviews served edits, and opens Add for an empty list');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
