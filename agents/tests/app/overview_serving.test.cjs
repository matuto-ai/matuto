'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../matuto/static/overview_serving.js'), 'utf8');

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.hidden = false; this.textContent = ''; this.dataset = {}; }
  append(...nodes) {
    if (this.tagName === 'select' && !this.value) {
      const option = nodes.find(node => node.tagName === 'option');
      if (option) this.value = option.getAttribute('value');
    }
    this.children.push(...nodes);
  }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === 'value') this.value = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  focus() { document.activeElement = this; }
  async click() { await this.listeners.click?.(); }
  querySelectorAll(selector) {
    if (selector !== 'button') return [];
    return this.children.flatMap(child => [child, ...child.querySelectorAll(selector)])
      .filter(child => child.tagName === 'button');
  }
  set href(value) { this.attributes.href = value; }
  get href() { return this.attributes.href; }
}

const currentSection = new Element('section');
const elements = new Map([
  ['heading-overview', new Element('h2')],
  ['serving-headline', new Element('p')], ['serving-error', new Element('p')],
  ['serving-details', new Element('div')], ['serving-controls', new Element('div')],
  ['overview-current-work', new Element('div')],
  ['overview-current-work-detail', new Element('p')],
  ['overview-current-work-link', new Element('a')],
]);
 elements.get('overview-current-work').parentElement = currentSection;
const timers = [];
const listeners = {};
const document = {
  activeElement: null,
  getElementById: id => elements.get(id) || null,
  createElement: tag => new Element(tag),
  addEventListener(name, handler) { (listeners[name] ||= []).push(handler); },
};
const popups = [];
const window = {
  matutoCsrf: 'test-token', matutoReason: (control, reason) => { control.reason = reason; },
  matutoPopup: {preview: options => popups.push(options)},
  location: {hash: '#/overview/serving'},
  addEventListener(name, handler) { (listeners[`window:${name}`] ||= []).push(handler); },
  setTimeout(handler, delay) { timers.push({handler, delay}); return timers.length; },
};
let result = {
  state: 'agentic', healthy: true, headline: 'Serving your published models.', model: 'model-1', bundle: '',
  runtime: {build: 'Runtime A', backend: 'HIP', card: 'Card A'},
  addresses: [{label: 'Serving', address: 'http://127.0.0.1:8888'}], parts: [],
  profiles: [{profile_id: 'candidate-one', name: 'Candidate', published: true,
    qualification: {state: 'candidate', changed_since_qualification: true}}],
  bundles: [{id: 'bundle-one', name: 'Studio voice A', offered: true, reasons: []},
    {id: 'bundle-two', name: 'Studio voice B', offered: true, reasons: []},
    {id: 'bundle-candidate', name: 'Candidate voice', offered: false, reasons: ['Not qualified: a candidate.']}],
  controls: [
    {name: 'stop_serving', label: 'Stop serving…', href: '/api/serving/stop', confirmation: true, reason: ''},
    {name: 'restart', label: 'Restart…', href: '/api/serving/restart', confirmation: true, reason: ''},
    {name: 'serve_voice', label: 'Serve voice…', href: '/api/serving/voice', confirmation: true, reason: ''},
  ],
  currentWork: {label: 'Building runtime', href: '#/runs?work=build-1'},
};
const calls = [];
let refuseConfirm = false;
let responseDelay = 0, activeReads = 0, maximumActiveReads = 0;
const fetch = async (url, options = {}) => {
  calls.push({url, options});
  if (url === '/api/overview/serving') {
    activeReads++;
    maximumActiveReads = Math.max(maximumActiveReads, activeReads);
    if (responseDelay) await new Promise(resolve => setTimeout(resolve, responseDelay));
    activeReads--;
    return {ok: true, json: async () => result};
  }
  if (options.body) {
    const body = JSON.parse(options.body);
    if (body.confirm === false) return {ok: true, json: async () => ({acted: false, preview: {
      stops: ['Agentic serving'], starts: ['Nothing'], stays: ['Downloads'], safe_choice: 'Cancel'}})};
    if (refuseConfirm) return {ok: true, json: async () => ({acted: true, stopped: false,
      reason: 'A speech part is still running.'})};
    return {ok: true, json: async () => ({acted: true, state: 'stopped'})};
  }
  return {ok: true, json: async () => ({})};
};

vm.runInNewContext(source, {document, window, fetch, encodeURIComponent});

(async () => {
  listeners.DOMContentLoaded[0]();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get('serving-headline').textContent, 'Serving your published models.',
    `headline ${elements.get('serving-headline').textContent}; error ${elements.get('serving-error').textContent}`);
  assert.equal(elements.get('overview-current-work').hidden, false);
  assert.equal(currentSection.hidden, false, 'active Current work shows its whole section');
  assert.equal(elements.get('overview-current-work-link').href, '#/runs?work=build-1');
  const all = element => element.children.flatMap(child => [child, ...all(child)]);
  const links = all(elements.get('serving-details')).filter(child => child.tagName === 'a');
  assert.ok(links.some(link => link.textContent === 'Qualify…'
    && link.href === '#/runs?profile=candidate-one'));
  const candidateRow = all(elements.get('serving-details')).find(item => item.tagName === 'li'
    && item.children.some(child => child.textContent === 'Candidate — Candidate (changed since qualification)'));
  assert.ok(candidateRow?.children.some(child => child.textContent === 'Qualify…'),
    'each candidate has its quiet Qualify link beside the published profile label');
  assert.equal(all(elements.get('serving-details')).filter(item => item.tagName === 'span'
    && item.textContent === 'Candidate — Candidate (changed since qualification)').length, 1,
    'published candidates appear once in the profile list');
  assert.ok(elements.get('serving-details').children.some(section => section.textContent.includes('Loaded model: model-1')));

  result = {...result, reason: 'The router health check failed.'};
  await listeners['window:hashchange'][0]();
  assert.ok(all(elements.get('serving-details')).some(item => item.textContent.includes('The router health check failed')),
    'a new serving reason is rendered when the other details stay unchanged');
  result = {...result, reason: ''};
  await listeners['window:hashchange'][0]();
  assert.ok(!all(elements.get('serving-details')).some(item => item.textContent.includes('The router health check failed')),
    'a cleared serving reason disappears on the next refresh');

  const stop = elements.get('serving-controls').querySelectorAll('button')[0];
  await stop.click();
  assert.equal(popups.length, 1);
  assert.deepEqual(popups[0].stops, ['Agentic serving']);
  assert.deepEqual(popups[0].starts, ['Nothing']);
  assert.equal(calls.find(call => call.url === '/api/serving/stop').options.headers['X-CSRF-Token'], 'test-token');
  await popups[0].confirmAction();
  assert.equal(JSON.parse(calls.filter(call => call.url === '/api/serving/stop').at(-1).options.body).confirm, true);

  await elements.get('serving-controls').querySelectorAll('button')[1].click();
  assert.match(popups[1].content.textContent, /unloads until the next request loads it/);
  assert.equal(JSON.parse(calls.filter(call => call.url === '/api/serving/restart').at(-1).options.body).confirm, false);
  await popups[1].confirmAction();
  assert.equal(JSON.parse(calls.filter(call => call.url === '/api/serving/restart').at(-1).options.body).confirm, true);

  const picker = all(elements.get('serving-controls')).find(child => child.tagName === 'select');
  assert.equal(picker.children.length, 2);
  assert.equal(picker.children[0].textContent, 'Studio voice A');
  assert.equal(picker.children[1].textContent, 'Studio voice B');
  picker.value = 'bundle-two';
  picker.focus();
  const unchangedPoll = timers.shift();
  assert.equal(unchangedPoll.delay, 2000);
  await unchangedPoll.handler();
  assert.equal(all(elements.get('serving-controls')).find(child => child.tagName === 'select'), picker,
    'an unchanged poll keeps the live bundle picker node');
  assert.equal(picker.value, 'bundle-two', 'an unchanged poll preserves the chosen bundle');
  assert.equal(document.activeElement, picker, 'an unchanged poll preserves focus in the picker');
  assert.ok(all(elements.get('serving-controls')).some(child =>
    child.tagName === 'li' && child.textContent.includes('Candidate voice: Not qualified')));
  await elements.get('serving-controls').querySelectorAll('button')[2].click();
  assert.equal(JSON.parse(calls.filter(call => call.url === '/api/serving/voice').at(-1).options.body).confirm,
    false);
  assert.equal(JSON.parse(calls.filter(call => call.url === '/api/serving/voice').at(-1).options.body).bundle,
    'bundle-two');
  assert.match(all(popups[2].content).map(item => item.textContent).join(' '), /Selected bundle: Studio voice B/);
  await popups[2].confirmAction();
  assert.equal(JSON.parse(calls.filter(call => call.url === '/api/serving/voice').at(-1).options.body).confirm, true);
  result = {...result, currentWork: null, state: 'stopped', headline: 'Ready. Not serving.',
    publishLink: false,
    controls: [{name: 'start_agentic', label: 'Start agentic serving', href: '/api/serving/start',
      confirmation: false, reason: ''}]};
  listeners['window:hashchange'][0]();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get('overview-current-work').hidden, true);
  assert.equal(currentSection.hidden, true, 'idle Current work hides its whole section');
  const start = elements.get('serving-controls').querySelectorAll('button')[0];
  await start.click();
  const startRequest = calls.filter(call => call.url === '/api/serving/start').at(-1);
  assert.equal(startRequest.options.method, 'POST');
  assert.equal(startRequest.options.body, '{}', 'Start agentic serving acts directly without a confirmation');
  assert.equal(popups.length, 3, 'Start agentic serving does not open a preview');

  result = {...result, profiles: [], publishLink: true,
    controls: [{name: 'start_agentic', label: 'Start agentic serving', href: '/api/serving/start',
      confirmation: false, reason: 'Nothing is published.'}]};
  listeners['window:hashchange'][0]();
  await new Promise(resolve => setImmediate(resolve));
  const refusedStart = elements.get('serving-controls').querySelectorAll('button')[0];
  assert.ok(all(elements.get('serving-controls')).some(child =>
    child.tagName === 'a' && child.href === '#/profiles/language'));
  const startsBeforeRefusal = calls.filter(call => call.url === '/api/serving/start').length;
  await refusedStart.click();
  assert.equal(calls.filter(call => call.url === '/api/serving/start').length, startsBeforeRefusal,
    'known Start blockers are refused before the mutation request');
  assert.match(elements.get('serving-error').textContent, /Nothing is published/);
  assert.ok(all(elements.get('serving-controls')).some(child =>
    child.tagName === 'a' && child.href === '#/profiles/language'));

  result = {...result, state: 'voice', bundle: 'Studio voice', headline: 'Voice is on.',
    controls: [
      {name: 'stop_serving', label: 'Stop serving…', href: '/api/serving/voice/stop', confirmation: true, reason: ''},
      {name: 'switch_to_agentic', label: 'Switch to agentic serving…', href: '/api/serving/voice/switch',
        confirmation: true, reason: ''},
    ]};
  listeners['window:hashchange'][0]();
  await new Promise(resolve => setImmediate(resolve));
  const voiceButtons = elements.get('serving-controls').querySelectorAll('button');
  assert.equal(voiceButtons.length, 2);
  await voiceButtons[1].click();
  assert.equal(calls.filter(call => call.url === '/api/serving/voice/switch').length, 1);
  assert.equal(JSON.parse(calls.filter(call => call.url === '/api/serving/voice/switch').at(-1).options.body).confirm,
    false);
  assert.equal(popups[3].safe, 'Cancel');
  await popups[3].confirmAction();
  assert.equal(JSON.parse(calls.filter(call => call.url === '/api/serving/voice/switch').at(-1).options.body).confirm,
    true);

  result = {...result, controls: result.controls.map(entry => entry.name === 'switch_to_agentic'
    ? {...entry, reason: 'A speech part is still running (speech-one). Stop every voice part before switching.'}
    : entry)};
  listeners['window:hashchange'][0]();
  await new Promise(resolve => setImmediate(resolve));
  const partialButtons = elements.get('serving-controls').querySelectorAll('button');
  await partialButtons[1].click();
  assert.match(elements.get('serving-error').textContent, /speech part is still running/,
    'activating a refused switch explains the blocker');
  assert.match(partialButtons[1].reason, /speech part is still running/,
    'the refused switch names the blocker beside it');
  refuseConfirm = true;
  await partialButtons[0].click();
  assert.equal(popups.length, 5);
  assert.equal(popups[4].safe, 'Cancel');
  await assert.rejects(popups[4].confirmAction, /speech part is still running/,
    'a route refusal propagates into the existing popup error path');
  const retryButtons = elements.get('serving-controls').querySelectorAll('button');
  assert.equal(popups[4].returnFocus(), retryButtons[0],
    'closing a refused popup restores focus to the refreshed Stop control');
  assert.equal(retryButtons[0].textContent, 'Stop serving…', 'a remaining voice part leaves Stop retryable');
  assert.match(retryButtons[1].reason, /speech part is still running/,
    'a remaining voice part keeps Switch refused with its blocker');
  assert.equal(elements.get('serving-headline').textContent, 'Voice is on.');

  result = {...result, state: 'stopped', headline: 'Ready. Not serving.', currentWork: null,
    profiles: [{profile_id: 'profile-live', name: 'Live profile', published: true,
      qualification: {state: 'qualified'}}], publishLink: false,
    controls: [{name: 'start_agentic', label: 'Start agentic serving', href: '/api/serving/start',
      confirmation: false, reason: ''}]};
  responseDelay = 30;
  const poll = timers.shift();
  const pollRequest = poll.handler();
  const overlappingTrigger = listeners['window:hashchange'][0]();
  await Promise.all([pollRequest, overlappingTrigger]);
  assert.equal(elements.get('serving-headline').textContent, 'Ready. Not serving.',
    'the delayed read still renders the live serving state');
  assert.equal(maximumActiveReads, 1, 'polls and navigation refreshes never overlap reads');
  assert.equal(timers.at(-1).delay, 2000);
  console.log('PASS: Overview serving states, previews, refusal recovery, bundle selection and live polling');
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
