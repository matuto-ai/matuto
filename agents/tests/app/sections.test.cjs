#!/usr/bin/env node
// Runs the shell's own script against element doubles with sample section files, so the tab
// row, its routes and its current-section state have a real seam. It proves the script's
// navigation behaviour, never layout, paint or wrapping; those are disposable browser probes.
'use strict';

const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const AGENTS = path.resolve(__dirname, '../../../agents');
const PYTHON = process.env.PYTHON || 'python3';

/* ── Sample sections ─────────────────────────────────────────────────────── */
// File names are the sections' own NAME, deliberately unlike the approved order, so the
// order these assertions read can only come from each section's ORDER.
const SECTIONS = {
    profiles: [['language', 'Language profiles', 1], ['speech-profiles', 'Speech profiles', 2],
               ['bundles', 'Bundles', 3], ['io', 'Import / export', 4]],
    runtime: [['builds', 'llama.cpp builds', 1], ['install', 'Install a version', 2],
              ['speech-runtimes', 'Speech runtimes', 3], ['activity', 'Build activity', 4]],
    settings: [['storage-artifacts', 'Storage & artifacts', 1], ['origins', 'Download origins', 2],
               ['cards', 'Cards', 3], ['catalogue', 'Argument catalogue', 4], ['lab', 'Lab port', 5],
               ['account', 'Account', 6], ['maintenance', 'Maintenance', 7]],
};

function render(sections) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'matuto-sections-'));
    try {
        fs.cpSync(path.join(AGENTS, 'matuto'), path.join(root, 'matuto'), { recursive: true });
        for (const name of ['language_profiles.py', 'speech_profiles.py', 'speech_runtimes.py',
            'import_export.py', 'build_activity.py'])
            fs.rmSync(path.join(root, 'matuto', 'page', name));
        for (const [page, list] of Object.entries(sections))
            for (const [name, title, order] of list) {
                if (name === 'storage-artifacts') continue; // Use the real section file.
                fs.writeFileSync(path.join(root, 'matuto', 'page', `${name}.py`),
                    `PAGE = ${JSON.stringify(page)}\nNAME = ${JSON.stringify(name)}\n` +
                    `TITLE = ${JSON.stringify(title)}\nORDER = ${order}\n\n\n` +
                    `def html(status):\n    return '<p id="panel-${name}">${name}</p>'\n`);
            }
        return execFileSync(PYTHON, ['-B', '-c',
            'import sys; from matuto.shell import render; sys.stdout.write(render({}, "test"))'],
            { cwd: root, encoding: 'utf8' });
    } finally {
        fs.rmSync(root, { recursive: true, force: true });
    }
}

/* A live querySelectorAll returns a NodeList, which has neither some, filter nor map; returning an array here would hide a script that calls one of them. */
class NodeList {
    constructor(items) { this.items = items }
    get length() { return this.items.length }
    item(index) { return this.items[index] || null }
    forEach(callback, thisArg) { this.items.forEach(callback, thisArg) }
    [Symbol.iterator]() { return this.items[Symbol.iterator]() }
}

/* ── Element doubles ─────────────────────────────────────────────────────── */
class Element {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.attributes = {};
        this.dataset = {};
        this.classes = new Set();
        this.listeners = {};
        this.text = '';
        this.classList = {
            add: (...names) => names.forEach(name => this.classes.add(name)),
            remove: (...names) => names.forEach(name => this.classes.delete(name)),
            toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name),
            contains: name => this.classes.has(name),
        };
    }
    get hidden() { return 'hidden' in this.attributes }
    set hidden(value) { value ? this.attributes.hidden = '' : delete this.attributes.hidden }
    get textContent() { return this.text || this.children.map(child => child.textContent).join('') }
    set textContent(value) { this.text = String(value); this.children = [] }
    get id() { return this.getAttribute('id') || '' }
    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
    }
    getAttribute(name) { return name in this.attributes ? this.attributes[name] : null }
    removeAttribute(name) {
        delete this.attributes[name];
        if (name.startsWith('data-')) delete this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())];
    }
    append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node) } }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes) }
    querySelector(selector) { const list = this.querySelectorAll(selector); return list.length ? list.item(0) : null }
    querySelectorAll(selector) { return new NodeList(selector.split(',').flatMap(part => find(this, part.trim().split(/\s+/)))) }
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) }
    click() { for (const handler of this.listeners.click || []) handler({}) }
    focus() { this.ownerDocument.activeElement = this }
}
// The selector shapes the shell's script uses: a tag, #id, .class, [attr] and one descendant step.
function matches(node, selector) {
    if (!node.classes) return false;
    let rest = selector;
    const tag = /^([a-zA-Z][a-zA-Z0-9]*)/.exec(rest);
    if (tag) { if (node.tagName !== tag[1].toUpperCase()) return false; rest = rest.slice(tag[1].length) }
    if (rest.startsWith('#')) return node.id === rest.slice(1);
    if (rest.startsWith('.')) return node.classes.has(rest.slice(1));
    if (rest.startsWith('[')) {
        const attribute = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(rest);
        if (!attribute) return false;
        const value = node.getAttribute(attribute[1]);
        return value !== null && (attribute[2] === undefined || value === attribute[2]);
    }
    return rest === '';
}
function find(node, parts) {
    const found = [];
    const walk = (element, depth) => {
        for (const child of element.children) {
            if (matches(child, parts[depth])) {
                if (depth === parts.length - 1) found.push(child);
                else walk(child, depth + 1);
            }
            walk(child, depth);
        }
    };
    walk(node, 0);
    return found;
}

function build(html) {
    const document = {
        root: new Element('body'),
        byId: new Map(),
        activeElement: null,
        getElementById(id) { return this.byId.get(id) || null },
        createElement(tag) { const node = new Element(tag); node.ownerDocument = this; return node },
        querySelector(selector) { return this.root.querySelector(selector) },
        querySelectorAll(selector) { return this.root.querySelectorAll(selector) },
        addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) },
        async fire(name, event) { await Promise.all((this.listeners[name] || []).map(handler => handler(event))) },
        listeners: {},
    };
    const stack = [document.root];
    let index = 0;
    const top = () => stack[stack.length - 1];
    const text = value => ({ textContent: value, parentNode: null, text: undefined, children: [] });
    while (index < html.length) {
        if (html[index] !== '<') {
            const next = html.indexOf('<', index);
            const end = next < 0 ? html.length : next;
            if (html.slice(index, end).trim()) { const node = text(html.slice(index, end).trim()); node.parentNode = top(); top().children.push(node) }
            index = end;
            continue;
        }
        if (html.startsWith('<!--', index)) { index = html.indexOf('-->', index) + 3; continue }
        if (html.startsWith('<!', index)) { index = html.indexOf('>', index) + 1; continue }
        const closing = /^<\/([a-zA-Z0-9]+)>/.exec(html.slice(index));
        if (closing) { stack.pop(); index += closing[0].length; continue }
        const end = html.indexOf('>', index);
        const tag = html.slice(index + 1, end);
        const name = /^([a-zA-Z0-9]+)/.exec(tag)[1].toLowerCase();
        const node = new Element(name);
        for (const [, attribute, value] of tag.slice(name.length).matchAll(/([\w:-]+)="([^"]*)"/g)) {
            node.setAttribute(attribute, value);
            if (attribute === 'class') node.classes = new Set(value.split(/\s+/).filter(Boolean));
        }
        if (node.id) document.byId.set(node.id, node);
        node.ownerDocument = document;
        if (name === 'body') document.root = node;
        top().append(node);
        index = end + 1;
        if (name === 'style' || name === 'script') {
            stack.push(node);
            const closingTag = html.indexOf('</' + name, index);
            node.text = html.slice(index, closingTag);
            index = closingTag;
        } else if (!/^(meta|link|br|hr|img|input|source)$/.test(name)) stack.push(node);
    }
    return {
        document,
        window: {
            location: { hash: '' },
            addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) },
            async fire(name, event) { await Promise.all((this.listeners[name] || []).map(handler => handler(event))) },
            listeners: {},
        },
    };
}

function load(html, fetch = async () => ({ ok: true, json: async () => ({}) })) {
    const { document, window } = build(html);
    const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
    const shell = new Function('document', 'window', 'fetch', script +
        '\nreturn {api,applyStatus,setPage,setMenu,pageFromHash,routeFromHash,showSection,menu,notice,clearNotice};')(
        document, window, fetch);
    return { document, window, shell };
}

const failures = [];
const check = (condition, label) => { if (!condition) failures.push(label) };
const labelsOf = nav => nav.map(tab => tab.textContent).join(' | ');
const tabsOf = (page, query) => query.querySelectorAll('#page-' + page + ' .section-nav a');
/* ── Behaviour ───────────────────────────────────────────────────────────── */
async function main() {
const APPROVED = {
    profiles: 'Language profiles | Speech profiles | Bundles | Import / export',
    runtime: 'llama.cpp builds | Install a version | Speech runtimes | Build activity',
    settings: 'Storage & artifacts | Download origins | Cards | Argument catalogue | Lab port | Account | Maintenance',
};
let overviewData = { tasks: [
    { label: 'Set model storage', href: '#/settings/storage-artifacts', destination: 'Settings › Storage & artifacts' },
], configured: [], storageRepair: null };
let overviewReads = 0, rawUpload;
const { document, window, shell } = load(render(SECTIONS), async (path, options = {}) => {
    if (options.method === 'POST') rawUpload = options;
    return { ok: true, json: async () => {
        if (path === '/api/overview') { overviewReads++; return overviewData }
        return {};
    } };
});
const byId = id => document.getElementById(id);
const overviewScript = fs.readFileSync(path.join(AGENTS, 'matuto/static/overview.js'), 'utf8');
new Function('document', 'window', 'api', 'notice', 'clearNotice', overviewScript)(
    document, window, shell.api, shell.notice, shell.clearNotice);
await Promise.all((document.listeners.DOMContentLoaded || []).map(handler => handler()));
const uploadedFile = {bytes: 'unchanged'};
await shell.api.request('/api/import-export', {method: 'POST', rawBody: uploadedFile});
check(rawUpload.body === uploadedFile && rawUpload.headers['X-CSRF-Token'] === 'test',
    'the shared API must send raw document bodies unchanged with the CSRF token');
const setupList = byId('overview-setup-tasks');
check(overviewReads === 1 && setupList.children.length === 1,
    'the served Overview script must load data after the initial page view');
const tabs = page => [...tabsOf(page, document)];
const panels = (page, query) => [...query.querySelectorAll('#page-' + page + ' section[data-section]')];
const shown = (page, query) => panels(page, query).filter(panel => !panel.hidden)
    .map(panel => panel.dataset.section).join(' | ');
const current = query => query.filter(tab => tab.getAttribute('aria-current') === 'page')
    .map(tab => tab.dataset.section).join(' | ');
const openHash = hash => { window.location.hash = hash; return window.fire('hashchange', {}) };

/* Each tabbed page carries its sections as a tab row in the approved order. */
for (const [page, order] of Object.entries(APPROVED)) {
    check(labelsOf(tabs(page)) === order, `${page} must show its sections as tabs in the approved order`);
    check(tabs(page).length === SECTIONS[page].length, `${page} must show exactly one tab per section`);
}

/* Overview's parts are concurrent content, not mutually exclusive section tabs. */
check(tabs('overview').length === 0 && shown('overview', document) === 'setup | serving | current-work',
    'Overview must show setup, serving and current work together without section tabs');
for (const page of ['runs', 'console']) {
    check(tabs(page).length === 0, `${page} must show no tabs`);
    const nav = [...byId('page-' + page).querySelectorAll('.section-nav')];
    check(nav.length === 1 && nav[0].children.length === 0, `${page} must keep an empty (hidden) tab row`);
}
for (const [page, sections] of Object.entries(SECTIONS))
    for (const [name] of sections)
        check(tabs(page).some(tab => tab.tagName === 'A' && tab.getAttribute('href') === `#/${page}/${name}`),
            `the ${name} tab must be a link to its own route #/${page}/${name}`);

check(labelsOf(tabs('model-files')) === 'Get model files' &&
    tabs('model-files')[0].getAttribute('href') === '#/model-files/acquisition',
    'Model files must expose its acquisition wizard as a page section');

/* Overview deep links still show all concurrent parts; tabbed pages show only their selected panel. */
await openHash('#/overview/serving');
check(tabs('overview').length === 0 && shown('overview', document) === 'setup | serving | current-work',
    'an Overview deep link must not hide its other content');
await openHash('#/settings/cards');
check(byId('page-settings').hidden === false && byId('page-overview').hidden === true,
    'opening a section route must open its page');
check(current(tabs('settings')) === 'cards', 'the routed section must be exposed as the current tab');
check(shown('settings', document) === 'cards', 'only the routed section must be shown');

/* A page route with no section opens the page at its first section. */
await openHash('#/profiles');
check(byId('page-profiles').hidden === false && current(tabs('profiles')) === 'language',
    'a page route with no section must open the first section');
check(shown('profiles', document) === 'language', 'only the current section must be shown on a page route');

/* The first tab is current on the page the shell opens with, and no earlier section survives. */
await openHash('#/settings');
check(current(tabs('settings')) === 'storage-artifacts' && shown('settings', document) === 'storage-artifacts',
    'a page route must open its first section and no other');

/* Keyboard choice: activating a tab (Enter on a link) selects that section and shows its panel. */
const accountTab = tabs('settings').find(tab => tab.dataset.section === 'account');
accountTab.click();
check(current(tabs('settings')) === 'account' && shown('settings', document) === 'account',
    'choosing a tab must select that section and show only its panel');
check(document.activeElement === byId('heading-settings'), 'choosing a tab must move focus to the page heading');
check(tabs('runtime').every(tab => tab.getAttribute('aria-current') === null),
    'only the current page may mark a tab current');

/* A route from another page opens a section directly, and the shell keeps the page list current. */
await openHash('#/runtime/install');
check(byId('page-runtime').hidden === false && byId('page-settings').hidden === true,
    'a section route from another page must open that page');
check(current(tabs('runtime')) === 'install' && shown('runtime', document) === 'install',
    'the linked section must be current and shown');
check([...document.querySelectorAll('#pages a')].filter(link => link.dataset.page === 'runtime')
    .every(link => link.getAttribute('aria-current') === 'page'),
    'the page list must mark the page the section route opened');
overviewData = { tasks: [], configured: [], storageRepair: null };
const previousReads = overviewReads;
await openHash('#/settings/storage-artifacts');
const readsAway = overviewReads;
await openHash('#/overview/setup');
check(readsAway === previousReads && overviewReads === readsAway + 1 && setupList.hidden,
    `returning through the shell navigation must refresh setup from the store (${previousReads} -> ${readsAway} -> ${overviewReads}, hidden=${setupList.hidden})`);
}
main().then(() => {
    if (failures.length) {
        console.error('FAIL app section tabs:\n- ' + failures.join('\n- '));
        process.exit(1);
    }
    console.log('PASS: app section tabs, routes and current section');
}).catch(error => { console.error('FAIL app section tabs: ' + (error.stack || error)); process.exit(1) });
