'use strict';

const fs = require('fs');
const path = require('path');
const script = fs.readFileSync(path.join(__dirname, '../../../agents/matuto/static/maintenance.js'), 'utf8');

class Element {
  constructor(name = '') { this.name = name; this.children = []; this.listeners = {}; this.dataset = {}; this.text = ''; }
  get textContent() { return this.text + this.children.map(child => child.textContent || '').join(''); }
  set textContent(value) { this.text = String(value); this.children = []; }
  querySelector(selector) { return this.children.find(child => child.selector === selector); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  append(...nodes) { this.children.push(...nodes); }
}
const selectors = ['[data-repair]', '[data-repair-message]', '[data-remove]'];
const root = new Element();
for (const selector of selectors) { const element = new Element(selector); element.selector = selector; root.append(element); }
const body = new Element('body');
const document = {
  body,
  querySelector: selector => selector === '[data-maintenance]' ? root : null,
  createElement: tag => new Element(tag),
};
let popup;
const notices = [];
const reasons = [];
const window = {
  location: {hash: '#/settings/maintenance'},
  crypto: {randomUUID: () => '01234567-89ab-cdef-0123-456789abcdef'},
  listeners: {},
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); },
  matutoCsrf: 'csrf',
  matutoReason: (element, text) => reasons.push({element, text}),
  notice: (...args) => notices.push(args),
  clearNotice: () => {},
  matutoPopup: {preview: value => { popup = value; }},
};
let finalStepFailed = false;
let unconfirmedRemoval = false;
let unconfirmedStatusReads = 0;
let removalState = null;
let plan = {remove: [{item: 'Release files', path: '/release', size_bytes: 42}],
  keep: [{item: 'Store', path: '/store', size_bytes: 128}],
  blockers: [], consumer_configuration: 'Never touched.', later_bootstrap: 'Reuses kept data.'};
const requests = [];
const fetch = async (url, options) => {
  requests.push({url, ...options});
  if (options.method === 'GET' && url.includes('removal_id=')) {
    if (unconfirmedRemoval && ++unconfirmedStatusReads > 1) throw new TypeError('Failed to fetch');
    return {ok: true, json: async () => removalState || {stage: 'pending'}};
  }
  if (options.method === 'GET') return {ok: true, json: async () => ({removal: plan, repair: null})};
  const action = JSON.parse(options.body).action;
  if (action === 'remove-preview') return {ok: true, json: async () => plan};
  if (action === 'remove') {
    if (unconfirmedRemoval) {
      removalState = {stage: 'finalizing', kept: [
        {item: 'Store', path: '/after/first-step/store', size_bytes: 127},
      ]};
      return new Promise(() => {});
    }
    const result = finalStepFailed
      ? {finalStepFailed: true, error: 'final removal failed', kept: plan.keep}
      : {removed: true, kept: plan.keep};
    removalState = finalStepFailed
      ? {stage: 'failed', error: result.error, kept: result.kept}
      : {stage: 'complete', kept: result.kept};
    return {ok: true, json: async () => result};
  }
  if (action === 'repair') return {ok: true, json: async () => ({started: true})};
  throw new Error(`unexpected action ${action}`);
};

(async () => {
  new Function('document', 'window', 'fetch', 'setTimeout', script)(document, window, fetch, setTimeout);
  await new Promise(resolve => setImmediate(resolve));
  const repair = root.querySelector('[data-repair]');
  await repair.listeners.click();
  assert(popup.title === 'Review Repair Matuto', 'Repair opens its own consequence review');
  assert(popup.safe === 'Keep current installation', 'Repair opens on the safe choice');
  assert(popup.stops[0].includes('open tabs lose contact'), 'Repair preview explains manager restart');
  assert(popup.stays[0].includes('Serving and every run'), 'Repair preview promises no workload stop');
  assert(!requests.some(item => item.method === 'POST'), 'opening Repair review starts no action');

  plan = {...plan, blockers: ['Stop serving on Overview']};
  window.location.hash = '#/overview';
  for (const handler of window.listeners.hashchange) handler();
  await new Promise(resolve => setImmediate(resolve));
  plan = {...plan, blockers: []};
  window.location.hash = '#/settings/maintenance';
  for (const handler of window.listeners.hashchange) handler();
  await new Promise(resolve => setImmediate(resolve));
  const remove = root.querySelector('[data-remove]');
  assert(remove.dataset.blocked === 'false', 'returning to Maintenance refreshes and clears a blocker after it is resolved');

  await remove.listeners.click();
  assert(requests.at(-1).method === 'POST'
    && JSON.parse(requests.at(-1).body).action === 'remove-preview', 'removal first requests a fresh preview');
  assert(popup.title === 'Review removal of Matuto', 'removal opens its consequence preview');
  assert(popup.safe === 'Keep Matuto', 'removal opens on the safe choice');
  assert(popup.content.children.some(item => item.children?.some(child => child.textContent.includes('/store'))),
    'preview identifies the kept path and size');
  await popup.confirmAction();
  await new Promise(resolve => setTimeout(resolve, 1));
  assert(requests.some(item => item.method === 'POST' && JSON.parse(item.body).action === 'remove'),
    'only confirmation starts removal');
  assert(body.children.length === 1 && body.children[0].children[0].textContent === 'Matuto removed',
    'successful removal replaces Settings with a result-only view');
  assert(body.children[0].children[1].textContent.includes('manager and updater were removed last'),
    'the result records the final removal order');
  assert(body.children[0].children.some(item => item.textContent.includes('/store')),
    'result view shows the kept path');

  finalStepFailed = true;
  new Function('document', 'window', 'fetch', 'setTimeout', script)(document, window, fetch, setTimeout);
  await new Promise(resolve => setImmediate(resolve));
  await root.querySelector('[data-remove]').listeners.click();
  await popup.confirmAction();
  await new Promise(resolve => setTimeout(resolve, 1));
  assert(body.children[0].children[0].textContent === 'Matuto removal needs a terminal step',
    'a failed final removal step shows terminal recovery instead of completion');
  assert(body.children[0].children.some(item => item.textContent.includes('CLI `remove` command')),
    'terminal recovery identifies the remaining action');
  assert(body.children[0].children.some(item => item.textContent.includes('/store')),
    'terminal recovery keeps the preserved data list visible');

  finalStepFailed = false;
  unconfirmedRemoval = true;
  unconfirmedStatusReads = 0;
  delete body.dataset.matutoResultOnly;
  new Function('document', 'window', 'fetch', 'setTimeout', script)(document, window, fetch, setTimeout);
  await new Promise(resolve => setImmediate(resolve));
  await root.querySelector('[data-remove]').listeners.click();
  await popup.confirmAction();
  assert(body.children[0].children[0].textContent === 'Matuto removal status is unconfirmed',
    'a lost final response is not mislabeled as either success or confirmed failure');
  assert(body.children[0].children.some(item => item.textContent.includes('/after/first-step/store')) &&
    !body.children[0].children.some(item => item.textContent.includes(' /store')),
    'an unconfirmed result uses the actual kept list rather than the preview');

  unconfirmedRemoval = false;
  plan = {...plan, blockers: ['Stop serving on Overview', 'job state unknown']};
  delete body.dataset.matutoResultOnly;
  const blockedRoot = new Element();
  for (const selector of selectors) { const element = new Element(selector); element.selector = selector; blockedRoot.append(element); }
  document.querySelector = selector => selector === '[data-maintenance]' ? blockedRoot : null;
  const before = requests.length;
  new Function('document', 'window', 'fetch', 'setTimeout', script)(document, window, fetch, setTimeout);
  await new Promise(resolve => setImmediate(resolve));
  const blockedRemove = blockedRoot.querySelector('[data-remove]');
  assert(blockedRemove.dataset.blocked === 'true', 'busy removal is exposed as unavailable');
  assert(reasons.at(-1).text.includes('Stop serving'), 'the removal control states its blocker accessibly');
  await blockedRemove.listeners.click();
  assert(requests.length === before + 1, 'a known blocker prevents the preview request');
  const blockerNotice = notices.find(args => args[3] === 'Removal is unavailable');
  assert(blockerNotice, 'the blocker appears in Settings notices');
  assert(blockerNotice[5][1].label.startsWith('\u00a0·\u00a0'), 'several blocker links remain visibly separated');
  process.stdout.write('Maintenance page behavior passed.\n');
})().catch(error => { console.error(error); process.exitCode = 1; });

function assert(condition, message) { if (!condition) throw new Error(message); }
