'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.hidden = false; this.text = ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(item => item.textContent).join(''); }
  append(...items) { this.children.push(...items); }
}

async function render(initialPayload) {
  const list = new Element('div');
  const calls = [];
  const notices = [];
  let payload = initialPayload;
  const window = {location: {hash: '#/runtime/speech-runtimes'}, listeners: {},
    addEventListener(name, handler) { this.listeners[name] = handler; }};
  const document = {
    getElementById(id) { return id === 'speech-runtimes-list' ? list : null; },
    createElement(tag) { return new Element(tag); },
  };
  const fetch = async url => {
    calls.push(url);
    if (payload instanceof Error) throw payload;
    return {ok: true, json: async () => payload};
  };
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,
    '../../../agents/matuto/static/speech_runtimes.js'), 'utf8'), {document, window, fetch,
    notice: (...args) => notices.push(args), clearNotice: (...args) => notices.push(['clear', ...args])});
  await new Promise(resolve => setImmediate(resolve));
  return {list, calls, notices, window, setPayload(value) { payload = value; }};
}

(async () => {
  const entries = [
    {project: 'Breeze-TTS-2.cpp', recipe: 'Breeze synthesis (Vulkan)', backend: 'Vulkan',
      commit: 'a'.repeat(40), ggmlCommit: 'b'.repeat(40), installationPath: '/speech/breeze-a',
      executablePath: '/speech/breeze-a/breeze-server', sha256: 'c'.repeat(64),
      installHref: '#/runtime/install?recipe=Breeze%20synthesis%20%28Vulkan%29'},
    {project: 'whisper.cpp', recipe: 'whisper recognition (CPU)', backend: 'CPU',
      commit: 'd'.repeat(40), ggmlCommit: '', installationPath: '/speech/whisper-d',
      executablePath: '/speech/whisper-d/build/bin/whisper-server', sha256: 'e'.repeat(64),
      installHref: '#/runtime/install?recipe=whisper%20recognition%20%28CPU%29'},
  ];
  const populated = await render({executables: entries});
  assert.deepStrictEqual(populated.calls, ['/api/speech-runtimes']);
  assert.strictEqual(populated.list.children.length, 2);
  assert.match(populated.list.textContent, /third_party\/ggml commit/);
  for (const entry of entries) {
    assert(populated.list.textContent.includes(entry.project));
    assert(populated.list.textContent.includes(entry.recipe));
    assert(populated.list.textContent.includes(entry.commit));
    assert(populated.list.textContent.includes(entry.installationPath));
    assert(populated.list.textContent.includes(entry.executablePath));
    assert(populated.list.textContent.includes(entry.sha256));
  }
  assert.match(populated.list.textContent,
    /Install another version — Runtime › Install a version/);
  assert.match(populated.list.textContent, /Open Profiles › Speech profiles/);
  assert.strictEqual(populated.list.children[0].children.at(-1).children[0].href, entries[0].installHref);
  assert.strictEqual(populated.list.children[0].children.at(-1).children[2].href,
    '#/profiles/speech-profiles');
  assert.doesNotMatch(populated.list.textContent, /Register|Forget|in use|selection|Build/);

  const empty = await render({executables: []});
  assert.match(empty.list.textContent, /No speech runtimes are installed/);
  assert.strictEqual(empty.list.children[0].children[1].href, '#/runtime/install');
  const reopened = await render({executables: []});
  assert.match(reopened.list.textContent, /No speech runtimes are installed/);
  reopened.setPayload({executables: [entries[1]]});
  reopened.window.location.hash = '#/runtime/build-activity';
  await reopened.window.listeners.hashchange();
  reopened.window.location.hash = '#/runtime/speech-runtimes';
  await reopened.window.listeners.hashchange();
  assert.strictEqual(reopened.list.children.length, 1);
  assert.match(reopened.list.textContent, /whisper\.cpp/);
  assert.match(reopened.list.textContent, new RegExp(entries[1].commit));
  assert.doesNotMatch(reopened.list.textContent, /No speech runtimes are installed/);

  const unavailable = await render(new Error('offline'));
  assert.deepStrictEqual(unavailable.notices[0],
    ['runtime', 'speech-runtimes-error', 'blocker', 'Speech runtimes unavailable', 'offline']);
  unavailable.setPayload({executables: [entries[0]]});
  unavailable.window.location.hash = '#/runtime/build-activity';
  await unavailable.window.listeners.hashchange();
  unavailable.window.location.hash = '#/runtime/speech-runtimes';
  await unavailable.window.listeners.hashchange();
  assert.match(unavailable.list.textContent, /Breeze-TTS-2\.cpp/);
  process.stdout.write('PASS: Speech runtimes inventory, empty state, and navigation refresh\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
