'use strict';
const { execFileSync } = require('child_process');
const vm = require('vm');
const path = require('path');
const root = path.resolve(__dirname, '../..');
const source = execFileSync('python3', ['-B', '-c', 'from matuto.page.console import html; print(html({}))'], { cwd: root, encoding: 'utf8' });
const script = source.match(/<script[^>]*>([\s\S]*)<\/script>/)[1];

class Element {
  constructor(tag) { this.tagName = tag; this.attributes = {}; this.children = []; this.listeners = {}; this.hidden = false; this.text = ''; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  hasAttribute(name) { return name in this.attributes; }
  removeAttribute(name) { delete this.attributes[name]; }
  append(...nodes) { this.children.push(...nodes); }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.text = String(value); this.children = []; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  click() { for (const callback of this.listeners.click || []) callback(); }
}
const elements = new Map();
for (const id of ['console-status', 'console-available', 'console-unavailable', 'model-console-frame', 'console-separate', 'console-refresh', 'page-console'])
  elements.set(id, new Element(id === 'model-console-frame' ? 'iframe' : 'div'));
const documentListeners = {};
const document = {
  getElementById: id => elements.get(id),
  createElement: tag => new Element(tag),
  addEventListener: (name, callback) => { documentListeners[name] = callback; },
};
const windowListeners = {};
const window = { addEventListener: (name, callback) => { windowListeners[name] = callback; } };
let answer = { state: 'stopped', reason: '', console: { usable: false } };
const context = {
  document, window, fetch: async () => ({ ok: true, json: async () => answer }),
  setTimeout: () => 0, clearTimeout: () => {},
};
vm.runInNewContext(script, context);
const tick = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  documentListeners.DOMContentLoaded();
  await tick();
  const frame = elements.get('model-console-frame');
  const unavailable = elements.get('console-unavailable');
  if (frame.hasAttribute('src') || elements.get('console-separate').hasAttribute('href')) throw new Error('unavailable serving must not load or link to the console');
  if (!unavailable.textContent.includes('Start agentic serving on Overview')) throw new Error('stopped state must name its recovery action and destination');

  answer = { state: 'agentic', healthy: true, reason: '', console: { usable: true } };
  await elements.get('console-refresh').click();
  await tick();
  if (frame.getAttribute('src') !== 'http://matuto.localhost:8889/' || elements.get('console-separate').getAttribute('href') !== 'http://matuto.localhost:8889/') throw new Error('healthy agentic serving must load and link only to the console origin');
  if (!source.includes('sandbox="allow-downloads allow-forms allow-same-origin allow-scripts"') || !source.includes('referrerpolicy="no-referrer"')) throw new Error('frame must keep its sandbox and no-referrer policy');
  if (!source.includes('One model is loaded at a time') || !source.includes('consumer request loaded')) throw new Error('available page must explain the shared model slot');
  if (!source.includes('<a href="#/model-files">Model files</a>') || !source.includes('your external file manager to delete them')) throw new Error('download and deletion guidance must name its destinations');

  answer = { state: 'agentic', healthy: false, reason: 'the router health did not answer.', console: { usable: false } };
  elements.get('console-refresh').click();
  await tick();
  if (frame.hasAttribute('src') || !unavailable.textContent.includes('router health did not answer')
      || !unavailable.textContent.includes('Restart on Overview')) throw new Error('unhealthy agentic state must give its reason and recovery destination');

  answer = { state: 'voice', healthy: true, reason: '', console: { usable: false } };
  elements.get('console-refresh').click();
  await tick();
  if (frame.hasAttribute('src')) throw new Error('leaving healthy agentic serving must remove the frame');
  if (!unavailable.textContent.includes('would unload the voice model') || !unavailable.textContent.includes('Stop serving or Switch to agentic serving on Overview')) throw new Error('voice state must explain the risk and recovery destination');
  answer = { state: 'agentic', healthy: true, reason: '', console: { usable: true } };
  elements.get('console-refresh').click();
  await tick();
  if (frame.getAttribute('src') !== 'http://matuto.localhost:8889/') throw new Error('returning to healthy serving must restore the frame without a page reload');

  let pending = [];
  context.fetch = () => new Promise(resolve => pending.push(resolve));
  elements.get('console-refresh').click();
  await tick();
  elements.get('console-refresh').click();
  await tick();
  const voice = { state: 'voice', healthy: true, reason: '', console: { usable: false, transition: false } };
  pending[1]({ ok: true, json: async () => voice });
  await tick(); await tick();
  pending[0]({ ok: true, json: async () => ({ state: 'agentic', healthy: true, reason: '', console: { usable: true } }) });
  await tick(); await tick();
  if (frame.hasAttribute('src') || !unavailable.textContent.includes('Voice serving is active')) throw new Error('an older healthy response must not overwrite a newer voice response');

  pending = [];
  elements.get('console-refresh').click();
  await tick();
  elements.get('console-refresh').click();
  await tick();
  pending[1]({ ok: true, json: async () => ({ state: 'stopped', reason: '', console: { usable: false } }) });
  await tick(); await tick();
  pending[0]({ ok: false });
  await tick(); await tick();
  if (frame.hasAttribute('src') || !unavailable.textContent.includes('Agentic serving is not running')
      || unavailable.textContent.includes('Matuto is not answering')) throw new Error('an older request failure must not replace a newer state');

  let finishFetch;
  context.fetch = () => new Promise(resolve => { finishFetch = resolve; });
  elements.get('console-refresh').click();
  await tick();
  elements.get('page-console').hidden = true;
  finishFetch({ ok: true, json: async () => ({ state: 'agentic', healthy: true, reason: '', console: { usable: true } }) });
  await tick();
  await tick();
  if (frame.hasAttribute('src')) throw new Error('a late healthy response must not reload the frame after leaving the page');
  context.fetch = async () => ({ ok: true, json: async () => answer });
  elements.get('page-console').hidden = false;

  answer = { state: 'voice', healthy: false, reason: 'matuto-app-voice-part-voice.service is activating, so serving is in transition', console: { usable: false, transition: true } };
  elements.get('console-refresh').click();
  await tick();
  if (!unavailable.textContent.includes('starting or stopping') || !unavailable.textContent.includes('Wait until it settles')
      || unavailable.textContent.includes('Stop serving')) throw new Error('voice transition must tell operators to wait instead of offering voice recovery actions');

  answer = { state: 'unknown', healthy: false, reason: 'router did not answer', console: { usable: false } };
  elements.get('console-refresh').click();
  await tick();
  if (!unavailable.textContent.includes('router did not answer')) throw new Error('unknown state must preserve its reason');
  console.log('PASS: Model console page state and frame gating');
})().catch(error => { console.error(error); process.exitCode = 1; });
