#!/usr/bin/env node
// Runs the app shell's own script against element doubles and a storage double, so the
// light/dark switch's browser-side state has a real seam. It proves the script's state and
// storage behaviour, never layout, paint, contrast or focus rings; those are disposable
// headless-browser probes.
'use strict';

const { execFileSync } = require('child_process');
const path = require('path');

const AGENTS = path.resolve(__dirname, '../../../agents');
const PYTHON = process.env.PYTHON || 'python3';

const render = (status, csrf) => execFileSync(PYTHON, ['-B', '-c',
    'import sys; from matuto.shell import render; sys.stdout.write(render(' + status + ', ' + csrf + '))'],
    { cwd: AGENTS, encoding: 'utf8' });

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
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null }
    querySelectorAll(selector) { return selector.split(',').flatMap(part => find(this, part.trim().split(/\s+/))) }
    closest(selector) { for (let node = this; node; node = node.parentNode) if (matches(node, selector)) return node; return null }
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) }
    click() { for (const handler of this.listeners.click || []) handler({}) }
    focus() { const owner = this.ownerDocument; owner.activeElement = this.hidden ? owner.root : this }
}
// The selector shapes the shell uses: a tag, #id, .class, [attr] and one descendant step.
function matches(node, selector) {
    if (!node.classes) return false;
    if (selector.startsWith('#')) return node.id === selector.slice(1);
    if (selector.startsWith('.')) return node.classes.has(selector.slice(1));
    const attribute = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(selector);
    if (attribute) {
        const value = node.getAttribute(attribute[1]);
        return value !== null && (attribute[2] === undefined || value === attribute[2]);
    }
    return node.tagName === selector.toUpperCase();
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

/* ── The rendered document, as far as the script reads it ────────────────── */
function build(html) {
    const document = {
        root: new Element('body'),
        documentElement: null,
        byId: new Map(),
        activeElement: null,
        getElementById(id) { return this.byId.get(id) || null },
        createElement(tag) { return new Element(tag) },
        querySelector(selector) { return this.root.querySelector(selector) },
        querySelectorAll(selector) { return this.root.querySelectorAll(selector) },
        addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) },
        fire(name, event) { for (const handler of this.listeners[name] || []) handler(event) },
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
        if (closing) {
            if (!['style', 'script'].includes(closing[1].toLowerCase())) stack.pop();
            index += closing[0].length;
            continue;
        }
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
        if (name === 'html') document.documentElement = node;
        if (name === 'body') document.root = node;
        top().append(node);
        index = end + 1;
        if (name === 'style' || name === 'script') {
            const closingTag = html.indexOf('</' + name, index);
            node.text = html.slice(index, closingTag);
            index = closingTag;
        } else if (!/^(meta|link|br|hr|img|input|source)$/.test(name)) stack.push(node);
    }
    return { document, window: { location: { hash: '' }, addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) }, fire(name, event) { for (const handler of this.listeners[name] || []) handler(event) }, listeners: {} } };
}

function makeStorage(initial = {}) {
    const data = { ...initial };
    return {
        data,
        getItem: name => (name in data ? data[name] : null),
        setItem: (name, value) => { data[name] = String(value) },
        removeItem: name => { delete data[name] },
    };
}
const refusingStorage = () => ({
    getItem() { throw new Error('storage denied') },
    setItem() { throw new Error('storage denied') },
});

function load(html, storage, fetchImpl = async () => { throw new Error('the switch must not call fetch') }) {
    const { document, window } = build(html);
    const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
    new Function('document', 'window', 'fetch', 'localStorage', script)(document, window, fetchImpl, storage);
    return { document, window };
}

const failures = [];
const check = (condition, label) => { if (!condition) failures.push(label) };
const state = document_ => ({
    checked: document_.getElementById('themeToggle').getAttribute('aria-checked'),
    theme: document_.documentElement.dataset.theme,
});

/* ── Behaviour ───────────────────────────────────────────────────────────── */
function main() {
const html = render('{}', "'shell-token'");

/* A first visit starts light; the switch sits in the header slot, named Dark mode. */
const first = makeStorage();
let { document } = load(html, first);
const toggle = document.getElementById('themeToggle');
check(toggle !== null && toggle.closest('[data-slot="theme-switch"]') !== null,
    'the switch must fill the shell\'s header slot');
check(toggle.tagName === 'BUTTON' && toggle.getAttribute('role') === 'switch'
    && toggle.getAttribute('aria-label') === 'Dark mode',
    'the switch must be a button with role switch and the accessible name Dark mode');
check(toggle.querySelector('.knob') !== null, 'the switch must show a knob');
check(state(document).checked === 'false' && state(document).theme === 'light',
    'a first visit with empty storage must start light');

/* Toggling changes the mode and keeps the choice in this browser's storage only. */
toggle.click();
check(state(document).checked === 'true' && state(document).theme === 'dark',
    'toggling must switch the page to dark and mark the switch checked');
check(first.data['matuto-theme'] === 'dark', 'toggling must keep the choice in this browser\'s storage');
toggle.click();
check(state(document).checked === 'false' && state(document).theme === 'light' && first.data['matuto-theme'] === 'light',
    'toggling back must return to light and update the kept choice');

/* A kept dark choice is restored; junk or a missing choice is light. */
({ document } = load(html, makeStorage({ 'matuto-theme': 'dark' })));
check(state(document).checked === 'true' && state(document).theme === 'dark',
    'a kept dark choice must be restored on the next visit');
({ document } = load(html, makeStorage({ 'matuto-theme': 'blue' })));
check(state(document).checked === 'false' && state(document).theme === 'light',
    'an unknown kept value must read as light');

/* A browser that refuses storage starts light and the switch still works for the page. */
({ document } = load(html, refusingStorage()));
check(state(document).checked === 'false' && state(document).theme === 'light',
    'a browser that refuses storage must start light');
document.getElementById('themeToggle').click();
check(state(document).checked === 'true' && state(document).theme === 'dark',
    'the switch must still change the page when storage is refused');

/* Switching is appearance only: it sends no request and leaves unfinished input and popups. */
const calls = [];
({ document } = load(html, makeStorage(), async (...args) => { calls.push(args); return { ok: true, json: async () => ({}) } }));
const input = document.createElement('input');
input.value = 'half-written note';
document.getElementById('page-overview').append(input);
const popup = document.querySelector('[data-slot="popup"]');
popup.hidden = false;
document.getElementById('themeToggle').click();
check(calls.length === 0, 'switching must send no request to the manager');
check(input.value === 'half-written note', 'switching must keep unfinished input');
check(popup.hidden === false, 'switching must keep an open popup open');
check(document.getElementById('page-overview').hidden === false, 'switching must not rebuild the page');
}
main();
if (failures.length) {
    console.error('FAIL app theme switch:\n- ' + failures.join('\n- '));
    process.exit(1);
}
console.log('PASS: app light/dark switch');
