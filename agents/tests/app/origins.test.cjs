'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
  constructor() { this.children = []; this.listeners = {}; this.values = {}; this.hidden = false; this.text = ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent || child).join(''); }
  append(...children) { this.children.push(...children); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute(name, value) { (this.attributes ||= {})[name] = value; }
  focus() { this.focused = true; }
  reset() { this.values.name = ''; this.values.kind = 'organisation'; }
  async fire(name, event = {}) { return this.listeners[name]?.({...event, preventDefault() { this.defaultPrevented = true; }}); }
}

class FormDataDouble {
  constructor(form) { this.form = form; }
  get(name) { return this.form.values[name]; }
}

async function main() {
  const ids = Object.fromEntries(['downloadOrigins', 'origin-form', 'origins-list',
    'origins-empty', 'origins-message'].map(id => [id, new Element()]));
  ids['origin-form'].values = {kind: 'organisation', name: ''};
  const document = {
    listeners: {},
    getElementById(id) { return ids[id] || null; },
    createElement() { return new Element(); },
    addEventListener(name, handler) { this.listeners[name] = handler; },
  };
  let origins = [];
  let failNextList = false;
  const calls = [];
  const runningDownload = {state: 'running'};
  const api = {async request(_path, options = {}) {
    calls.push(options);
    if (!options.method || options.method === 'GET') {
      if (failNextList) { failNextList = false; throw new Error('list refresh failed'); }
      return {origins: origins.map(item => ({...item}))};
    }
    const body = options.body;
    if (body.action === 'add') {
      if (!body.name || body.name.includes('://')) throw new Error('origin name is malformed');
      if (origins.some(item => item.kind === body.kind && item.name.toLowerCase() === body.name.toLowerCase()))
        throw new Error(`download origin ${body.name} is already configured`);
      const origin = {origin_id: `origin-${origins.length + 1}`, ...body};
      origins.push(origin);
      if (body.name === 'team') failNextList = true;
      return {origin};
    }
    origins = origins.filter(item => item.origin_id !== body.origin_id);
    return {removed: body.origin_id};
  }};
  const source = fs.readFileSync(path.join(__dirname, '../../matuto/static/origins.js'), 'utf8');
  vm.runInNewContext(source, {document, api, FormData: FormDataDouble});
  await document.listeners.DOMContentLoaded();
  if (ids['origins-empty'].hidden) throw new Error('empty origin list must say downloads are disabled');

  const form = ids['origin-form'];
  form.values.name = 'team';
  await form.fire('submit');
  if (origins.length !== 1 || origins[0].name !== 'team' || ids['origins-empty'].hidden !== true
      || ids['origins-list'].children.length !== 1
      || !ids['origins-message'].text.includes('list refresh failed')
      || ids['origins-message'].text.includes('Added team'))
    throw new Error('successful Add must remain visible and report a failed refresh honestly');
  if (calls[1].body.action !== 'add') throw new Error('Add origin must call the add operation without a preview');

  form.values.name = 'team';
  await form.fire('submit');
  if (origins.length !== 1 || form.values.name !== 'team'
      || !ids['origins-message'].text.includes('already configured'))
    throw new Error('a duplicate refusal must preserve input and explain why it was not saved');

  form.values.name = 'https://other.test/model';
  await form.fire('submit');
  if (origins.length !== 1 || form.values.name !== 'https://other.test/model'
      || !ids['origins-message'].text.includes('malformed'))
    throw new Error('a refused origin must preserve the input and display the reason without saving');

  form.values.kind = 'repository';
  form.values.name = 'team/model';
  await form.fire('submit');
  const remove = ids['origins-list'].children[1].children[2];
  await remove.fire('click');
  if (origins.length !== 1 || origins[0].name !== 'team'
      || runningDownload.state !== 'running'
      || !ids['origins-message'].text.includes('not cancelled'))
    throw new Error('Remove origin must update the list without cancelling running downloads');
  console.log('PASS: Download origins direct add, refusal preservation and non-cancelling remove');
}

main().catch(error => { console.error(error.stack || error); process.exit(1); });
