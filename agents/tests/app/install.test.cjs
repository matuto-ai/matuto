#!/usr/bin/env node
// Exercise Install a version at the page-script seam with element doubles.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
  constructor(tag, id = '') {
    this.tagName = tag.toUpperCase(); this.id = id; this.children = []; this.attributes = {};
    this.listeners = {}; this.text = ''; this.value = ''; this.hidden = false; this.parentNode = null;
    this.elements = {};
  }
  set textContent(value) {
    this.text = String(value);
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    if (this.tagName === 'SELECT') this.value = '';
  }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'value') this.value = String(value);
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  append(...children) {
    for (const child of children) { child.parentNode = this; this.children.push(child); }
    if (this.tagName === 'SELECT' && !this.value && this.children.length) this.value = this.children[0].value;
  }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  fire(name, event = {}) { return Promise.all((this.listeners[name] || []).map(fn => fn(event))); }
}

const tick = () => new Promise(resolve => setImmediate(resolve));
const source = fs.readFileSync(path.resolve(__dirname, '../../../agents/matuto/static/install.js'), 'utf8');
async function main() {
  const form = new Element('form', 'runtime-install-form');
  for (const name of ['recipe', 'ref', 'target']) form.elements[name] = new Element(name === 'ref' ? 'input' : 'select');
  const elements = {};
  for (const id of ['runtime-target-label', 'runtime-check', 'runtime-resolve', 'runtime-build',
    'runtime-pin', 'runtime-refs', 'runtime-error']) elements[id] = new Element('div', id);
  elements['runtime-error'].hidden = true;
  elements['runtime-install-form'] = form;
  const pageNotices = new Element('div');
  const runtimePage = new Element('section', 'page-runtime');
  const installPanel = new Element('section');
  const document = {
    getElementById(id) { return elements[id] || (id === 'page-runtime' ? runtimePage : null); },
    createElement(tag) { return new Element(tag); },
    querySelector(selector) {
      if (selector === '#page-runtime .notices [data-notice="runtime-install"]') return pageNotices;
      if (selector === '#page-runtime section[data-section="install"]') return installPanel;
      return null;
    },
    addEventListener(name, fn) { (documentListeners[name] ||= []).push(fn); },
    hidden: false,
  };
  const response = value => ({ok: true, json: async () => value});
  const calls = [];
  const windowListeners = {};
  const documentListeners = {};
  let postResult = null;
  let failBuild = false;
  let servingState = 'stopped';
  let holdCheck = false;
  let completeCheck = null;
  let holdResolve = false;
  let completeResolve = null;
  const fetch = async (_url, options) => {
    calls.push(options);
    if (!options.method || options.method === 'GET') return response({csrf: 'csrf-now', state: 'completed',
      servingState, cards: [{pciAddress: '0000:03:00.0', displayName: 'Fixture GPU', gfxTarget: 'gfx1201',
        canBuildHIP: true, default: false, targetReason: null}], recipes: [
        {name: 'llama.cpp HIP', takesGfxTarget: true, missingPrerequisites: []},
        {name: 'llama.cpp Vulkan', takesGfxTarget: false, missingPrerequisites: []},
        {name: 'Breeze synthesis (Vulkan)', takesGfxTarget: false, missingPrerequisites: []},
        {name: 'whisper recognition (CPU)', takesGfxTarget: false, missingPrerequisites: ['cmake']},
      ]});
    const payload = JSON.parse(options.body);
    if (payload.action === 'serving-state') return response({servingState});
    if (payload.action === 'check') {
      const result = {refs: [{name: 'refs/heads/main', commit: 'a'.repeat(40)}]};
      if (holdCheck) return new Promise(resolve => { completeCheck = () => resolve(response(result)); });
      postResult = result;
    }
    if (payload.action === 'resolve') {
      const result = {recipe: payload.recipe, ref: payload.ref, target: payload.target,
        commit: 'a'.repeat(40), pinId: 'resolved-pin'};
      if (holdResolve) return new Promise(resolve => { completeResolve = () => resolve(response(result)); });
      postResult = result;
    }
    if (payload.action === 'build' && failBuild) return {ok: false,
      json: async () => ({error: 'The slot is held by another build.'})};
    if (payload.action === 'build') postResult = {work: 'build-1', warning: '', href: '#/runtime/activity'};
    return response(postResult);
  };
  const notices = [];
  const window = {matutoCsrf: 'csrf-old', location: {hash: '#/runtime/install'},
    addEventListener(name, fn) { (windowListeners[name] ||= []).push(fn); },
    matutoReason: (control, reason) => {
      control.reason = reason;
      if (reason) control.setAttribute('aria-disabled', 'true');
      else delete control.attributes['aria-disabled'];
    }, clearNotice: (_page, key) => notices.push(['clear', key])};
  vm.runInNewContext(source, {document, window, fetch,
    notice: (...args) => notices.push(args), Promise, Object, String, Error, URLSearchParams, encodeURIComponent});
  await tick();

  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const recipes = form.elements.recipe;
  check(recipes.children.map(option => option.textContent).join('|') ===
    'llama.cpp HIP|llama.cpp Vulkan|Breeze synthesis (Vulkan)|whisper recognition (CPU)',
    'the form must expose exactly the four fixed recipes');
  check(!form.elements.ref.attributes.type && !source.includes('name="origin"')
    && !source.includes('name="command"') && !source.includes('name="patch"'),
    'the form must not offer arbitrary source or build inputs');
  check(elements['runtime-build'].getAttribute('aria-disabled') === 'true',
    'Build must be unavailable until the current request is resolved');
  check(form.elements.target.value === '' && form.elements.target.children[0].textContent === 'Choose a physical card'
    && form.elements.target.children[1].textContent.includes('Fixture GPU (gfx1201)'),
    'HIP asks for a physical target when no active selection supplies a default');

  holdCheck = true;
  const oldCheck = elements['runtime-check'].fire('click');
  await tick();
  recipes.value = 'llama.cpp Vulkan';
  await recipes.fire('change');
  completeCheck();
  await oldCheck;
  check(elements['runtime-refs'].textContent === '',
    'refs from a check superseded by a recipe change are discarded');
  holdCheck = false;
  await elements['runtime-check'].fire('click');
  check(calls.some(call => call?.method === 'POST' && JSON.parse(call.body).action === 'check'),
    'version discovery runs only after activation');
  check(elements['runtime-refs'].textContent.includes('refs/heads/main')
    && elements['runtime-refs'].textContent.includes('a'.repeat(40)),
    'the page lists only returned refs with their full commits');

  form.elements.ref.value = 'old';
  holdResolve = true;
  const oldResolve = elements['runtime-resolve'].fire('click');
  await tick();
  form.elements.ref.value = 'main';
  await form.elements.ref.fire('input');
  completeResolve();
  await oldResolve;
  check(elements['runtime-pin'].textContent === ''
    && elements['runtime-build'].getAttribute('aria-disabled') === 'true',
    'a resolution superseded by a ref change cannot restore a stale pin');
  holdResolve = false;
  await elements['runtime-resolve'].fire('click');
  check(elements['runtime-pin'].textContent.includes('a'.repeat(40))
    && elements['runtime-pin'].textContent.includes('llama.cpp Vulkan'),
    'resolution displays the exact full commit and fixed recipe before Build');
  check(elements['runtime-build'].getAttribute('aria-disabled') === null,
    'a resolved current pin enables Build');
  failBuild = true;
  await form.fire('submit', {preventDefault() {}});
  check(elements['runtime-pin'].textContent === ''
    && elements['runtime-build'].reason.includes('slot is held'),
    'a refused Build clears its pin and names the refusal beside Build');
  failBuild = false;
  form.elements.ref.value = 'other';
  await form.elements.ref.fire('input');
  check(elements['runtime-pin'].textContent === ''
    && elements['runtime-build'].getAttribute('aria-disabled') === 'true',
    'changing the ref clears the shown pin and disables Build again');
  recipes.value = 'llama.cpp HIP';
  await recipes.fire('change');
  form.elements.target.value = '0000:03:00.0';
  await form.elements.target.fire('change');
  form.elements.ref.value = 'main';
  await elements['runtime-resolve'].fire('click');
  check(elements['runtime-pin'].textContent.includes('gfx1201'),
    'the resolved HIP pin displays the selected physical target');
  form.elements.target.value = '';
  await form.elements.target.fire('change');
  check(elements['runtime-pin'].textContent === ''
    && elements['runtime-build'].getAttribute('aria-disabled') === 'true',
    'changing the physical target clears the shown pin');

  recipes.value = 'whisper recognition (CPU)';
  await recipes.fire('change');
  form.elements.ref.value = 'main';
  await elements['runtime-resolve'].fire('click');
  check(elements['runtime-build'].reason.includes('cmake'),
    'missing prerequisites remain a named Build blocker after resolution');
  const buildsBefore = calls.filter(call => call?.method === 'POST'
    && JSON.parse(call.body).action === 'build').length;
  await form.fire('submit', {preventDefault() {}});
  check(calls.filter(call => call?.method === 'POST'
    && JSON.parse(call.body).action === 'build').length === buildsBefore,
    'submitting while prerequisites are missing does not call Build');
  check(notices.some(item => item[0] === 'runtime' && item[1] === 'runtime-prerequisites'
    && item[3].includes('cannot build yet')),
    'the missing-prerequisite notice uses its own stable key');
  recipes.value = 'llama.cpp Vulkan';
  await recipes.fire('change');
  check(notices.some(item => item[0] === 'clear' && item[1] === 'runtime-prerequisites'),
    'changing to a prerequisite-free recipe removes the prerequisite notice');

  servingState = 'agentic';
  runtimePage.hidden = true;
  window.location.hash = '#overview';
  windowListeners.hashchange[0]();
  runtimePage.hidden = false;
  installPanel.hidden = true;
  window.location.hash = '#runtime';
  windowListeners.hashchange[0]();
  const statusesBeforeInstallTab = calls.filter(call => call?.method === 'POST'
    && JSON.parse(call.body).action === 'serving-state').length;
  installPanel.hidden = false;
  window.location.hash = '#/runtime/install';
  windowListeners.hashchange[0]();
  await tick();
  check(calls.filter(call => call?.method === 'POST'
    && JSON.parse(call.body).action === 'serving-state').length === statusesBeforeInstallTab + 1,
    'the Install section refreshes after selecting its tab from the sidebar default section');
  check(notices.some(item => item[0] === 'runtime' && item[1] === 'runtime-contention'
    && item[3] === 'Serving remains up during a build'),
    'returning to Install refreshes serving state and warns before Build');
  check(calls.some(call => call?.method === 'POST'
    && JSON.parse(call.body).action === 'serving-state'),
    'entering Install refreshes the current serving state');
  await elements['runtime-resolve'].fire('click');
  check(elements['runtime-pin'].textContent.includes('a'.repeat(40))
    && elements['runtime-build'].getAttribute('aria-disabled') === null,
    'the sidebar default route enables Build after its serving refresh and resolution');
  const buildsBeforeRoute = calls.filter(call => call?.method === 'POST'
    && JSON.parse(call.body).action === 'build').length;
  await form.fire('submit', {preventDefault() {}});
  check(calls.filter(call => call?.method === 'POST'
    && JSON.parse(call.body).action === 'build').length === buildsBeforeRoute + 1,
    'Build submits after the Runtime sidebar route refreshes serving state');
  const progressLink = pageNotices.children.find(child => child.tagName === 'A');
  check(progressLink?.getAttribute('href') === '#/runtime/activity',
    'an admitted build links to the discovered Build activity section');
  window.location.hash = '#/runtime/install?recipe=whisper%20recognition%20%28CPU%29';
  windowListeners.hashchange[0]();
  await tick();
  check(recipes.value === 'whisper recognition (CPU)',
    'Install another version selects the exact recipe named by its section link');
  check(elements['runtime-pin'].textContent === '',
    'changing recipe through a section link clears any old commit pin');
  process.stdout.write('PASS: Install a version page state and actions\n');
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
