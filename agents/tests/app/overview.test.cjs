'use strict';

const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../../..');
const html = execFileSync('python3', ['-B', '-c',
    "import sys; sys.path.insert(0, 'agents'); from matuto.shell import render; print(render({}, ''))"],
    { cwd: root, encoding: 'utf8' });
if (!html.includes('<script src="/overview.js" defer></script>'))
    throw new Error('Overview must load its same-origin setup script');
const script = fs.readFileSync(path.join(root, 'agents/matuto/static/overview.js'), 'utf8');

class Element {
    constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.hidden = false; this.textContent = ''; }
    set href(value) { this.attributes.href = value }
    get href() { return this.attributes.href }
    append(...nodes) { this.children.push(...nodes) }
    replaceChildren(...nodes) { this.children = nodes }
}
const elements = new Map([
    ['overview-setup-tasks', new Element('ul')],
    ['overview-configured', new Element('section')],
    ['overview-configured-list', new Element('ul')],
]);
let onReady;
const notices = [];
const document = {
    addEventListener(name, handler) { if (name === 'DOMContentLoaded') onReady = handler },
    getElementById(id) { return elements.get(id) || null },
    createElement(tag) { return new Element(tag) },
};
const window = {
    location: { hash: '#/overview/setup' },
    listeners: {},
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) },
    async fire(name) { await Promise.all((this.listeners[name] || []).map(handler => handler())) },
};
let response = { tasks: [
    { label: 'Set model storage', href: '#/settings/storage-artifacts', destination: 'Settings › Storage & artifacts' },
    { label: 'Get model files', href: '#/model-files/acquisition', destination: 'Model files wizard' },
    { label: 'Prepare the runtime', href: '#/runtime/install', destination: 'Runtime › Install a version' },
    { label: 'Create or import a language profile', href: '#/profiles/language', destination: 'Profiles › Language profiles' },
], configured: [], storageRepair: null };
let requests = 0;
let failRequest = false;
const api = { request: async path => {
    if (path !== '/api/overview') throw new Error('unexpected route');
    requests++;
    if (failRequest) throw new Error('temporary failure');
    return response;
} };
const notice = (...args) => notices.push(args);
const clearNotice = (...args) => notices.push(['clear', ...args]);
new Function('document', 'window', 'api', 'notice', 'clearNotice', script)(document, window, api, notice, clearNotice);

(async () => {
    window.location.hash = '#/settings/storage-artifacts';
    await onReady();
    if (requests !== 0) throw new Error('a deep link outside Overview must not read setup early');
    window.location.hash = '#/overview/setup';
    await window.fire('hashchange');
    const taskList = elements.get('overview-setup-tasks');
    if (taskList.children.length !== 4 || taskList.hidden)
        throw new Error('an empty store must render the four setup tasks');
    if (taskList.children.map(row => row.children[0].textContent).join('|') !==
        'Set model storage — Settings › Storage & artifacts|Get model files — Model files wizard|' +
        'Prepare the runtime — Runtime › Install a version|Create or import a language profile — Profiles › Language profiles')
        throw new Error('every setup task must name its destination');

    window.location.hash = '#/settings/storage-artifacts';
    await window.fire('hashchange');
    response = { tasks: [
        { label: 'Set model storage', href: '#/settings/storage-artifacts', destination: 'Settings › Storage & artifacts' },
        { label: 'Prepare the runtime', href: '#/runtime/builds', destination: 'Runtime › llama.cpp builds' },
    ], configured: [
        { label: 'Get model files', value: 'Imported models' },
        { label: 'Create or import a language profile', value: 'Imported profile' },
    ], storageRepair: null };
    window.location.hash = '#/overview/setup';
    await window.fire('hashchange');
    if (requests !== 2) throw new Error('returning to Overview must read current store data again');
    if (taskList.children.length !== 2 || taskList.children[1].children[0].href !== '#/runtime/builds')
        throw new Error('cutover state must keep only storage/runtime and link the found build list');
    const configured = elements.get('overview-configured-list').children;
    if (elements.get('overview-configured').hidden || configured.length !== 2
        || configured[0].textContent !== 'Get model files: Imported models'
        || configured[1].textContent !== 'Create or import a language profile: Imported profile')
        throw new Error('cutover records must appear under Already configured');

    failRequest = true;
    window.location.hash = '#/settings/storage-artifacts';
    await window.fire('hashchange');
    window.location.hash = '#/overview/setup';
    await window.fire('hashchange');
    if (!notices.some(args => args[0] === 'overview' && args[1] === 'setup-data'))
        throw new Error('a failed request must show a keyed setup warning');
    failRequest = false;
    window.location.hash = '#/settings/storage-artifacts';
    await window.fire('hashchange');
    window.location.hash = '#/overview/setup';
    await window.fire('hashchange');
    if (!notices.some(args => args[0] === 'clear' && args[1] === 'overview' && args[2] === 'setup-data'))
        throw new Error('a successful refresh must clear the recovered setup warning');

    window.location.hash = '#/settings/storage-artifacts';
    await window.fire('hashchange');
    response = { tasks: [{ label: 'Prepare the runtime', href: '#/runtime/install', destination: 'Runtime › Install a version' }],
        configured: [{ label: 'Set model storage', value: '/models' }],
        storageRepair: { title: 'Storage unavailable', detail: '/models does not exist. Affected records: Imported models.',
            link: '#/settings/storage-artifacts' } };
    window.location.hash = '#/overview/setup';
    await window.fire('hashchange');
    const repairNotice = notices.find(args => args[3] === 'Storage unavailable');
    if (!repairNotice || repairNotice[0] !== 'overview'
        || repairNotice[5].href !== '#/settings/storage-artifacts')
        throw new Error('storage repair must render in Overview notices with its repair link');
    window.location.hash = '#/settings/storage-artifacts';
    await window.fire('hashchange');
    response = { tasks: [], configured: [], storageRepair: null };
    window.location.hash = '#/overview/setup';
    await window.fire('hashchange');
    if (!taskList.hidden || !elements.get('overview-configured').hidden)
        throw new Error('all configured records must hide both Overview lists');
    if (notices.at(-1)[0] !== 'clear') throw new Error('cleared storage must leave no repair notice');
    console.log('PASS: Overview setup links, completed records, and repair notice');
})().catch(error => { console.error(error.stack || error); process.exitCode = 1 });
