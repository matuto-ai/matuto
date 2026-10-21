#!/usr/bin/env node
// Runs the app shell's own script against element doubles built from the rendered document,
// extending the seam of agents/tests/page-behaviour.cjs. It proves the script's navigation and
// API behaviour, never layout, paint or focus rings; those are disposable browser probes.
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
        const classes = this.classList = {
            add: (...names) => names.forEach(name => this.classes.add(name)),
            remove: (...names) => names.forEach(name => this.classes.delete(name)),
            toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name),
            contains: name => this.classes.has(name),
        };
        this.classList = classes;
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
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) }
    click() { for (const handler of this.listeners.click || []) handler({}) }
    focus() { const owner = this.ownerDocument; owner.activeElement = this.hidden ? owner.root : this }
}
// The selector shapes the shell uses: a tag, #id, .class and one descendant step.
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
        byId: new Map(),
        activeElement: null,
        getElementById(id) { return this.byId.get(id) || null },
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

function load(html, fetchImpl) {
    const { document, window } = build(html);
    const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
    const api = new Function('document', 'window', 'fetch', script +
        '\nreturn {api,applyStatus,setPage,setMenu,pageFromHash,menu};')(document, window, fetchImpl);
    return { document, window, shell: api };
}

const failures = [];
const check = (condition, label) => { if (!condition) failures.push(label) };

/* ── Behaviour ───────────────────────────────────────────────────────────── */
async function main() {
const html = render('{}', "'shell-token'");
const { document, window, shell } = load(html, async () => ({ ok: true, json: async () => ({}) }));
const byId = id => document.getElementById(id);

/* The page list is the seven pages, in the approved order, each one a link the menu shares. */
const links = document.querySelectorAll('#pages a');
const labels = links.map(link => link.textContent.replace('needs agentic serving', ''));
check(labels.join(' | ') === 'Overview | Model files | Profiles | Runs & jobs | Runtime | Settings | Model console',
    'the page list must carry the seven pages in the approved order');
check(links.every(link => link.dataset.page) && links.filter(link => link.getAttribute('aria-current') === 'page').length === 1,
    'exactly the current page may claim to be the current page');
check(byId('page-overview').hidden === false && links.slice(1).every(link => byId('page-' + link.dataset.page).hidden),
    'only the current page may be shown');

/* The first Tab stop is the skip link, and it points at the content it skips past. */
check(document.root.children[0].tagName === 'A' && document.root.children[0].classes.has('skip')
    && document.root.children[0].getAttribute('href') === '#main' && byId('main') !== null,
    'the skip link must come first and reach the page content');

/* "needs agentic serving" follows the status the shell is given, not the page it shows. */
const hint = document.querySelector('#pages .hint');
check(hint.hidden === true, 'the console hint must stay hidden while the console is usable');
shell.applyStatus({ console: { usable: true } });
check(hint.hidden === true, 'a usable console must carry no hint');
shell.applyStatus({ console: { usable: false } });
check(hint.hidden === false && hint.textContent === 'needs agentic serving' && hint.parentNode.dataset.page === 'console',
    'an unusable console must show "needs agentic serving" under Model console');
shell.applyStatus({});
check(hint.hidden === true, 'losing the status must take the hint away again');
shell.applyStatus({ console: {} });
check(hint.hidden === false, 'a console record without a usable flag must read as unusable');

/* The narrow menu is one button that is ☰ closed and ✕ open, named Pages, expanded state exposed. */
const toggle = byId('pagesToggle');
check(toggle.getAttribute('aria-label') === 'Pages' && toggle.getAttribute('aria-expanded') === 'false' && toggle.textContent === '☰',
    'the closed menu button must be named Pages, collapsed and show ☰');
check(toggle.getAttribute('aria-controls') === 'pages', 'the menu button must name the list it opens');
toggle.click();
check(toggle.getAttribute('aria-expanded') === 'true' && toggle.textContent === '✕' && byId('pages').classes.has('open'),
    'an open menu must show ✕, expose its expanded state and show the list');
toggle.click();
check(toggle.getAttribute('aria-expanded') === 'false' && toggle.textContent === '☰' && !byId('pages').classes.has('open'),
    'closing the menu must return it to ☰ and collapse it');

/* Choosing a page closes the list and focuses that page's heading. */
toggle.click();
links[4].click();
check(toggle.getAttribute('aria-expanded') === 'false' && byId('pages').classes.has('open') === false,
    'choosing a page must close the menu');
check(byId('page-runtime').hidden === false && byId('page-overview').hidden === true
    && links[4].getAttribute('aria-current') === 'page' && links[0].getAttribute('aria-current') === null,
    'choosing a page must show that page and mark it current');
check(document.activeElement === byId('heading-runtime'), 'choosing a page must focus that page heading');

/* Escape closes the list and gives focus back to the button that opened it. */
toggle.click();
byId('heading-runtime').focus();
document.fire('keydown', { key: 'Escape' });
check(toggle.getAttribute('aria-expanded') === 'false' && !byId('pages').classes.has('open'),
    'Escape must close the menu');
check(document.activeElement === toggle, 'Escape must return focus to the menu button');
document.fire('keydown', { key: 'Escape' });
check(toggle.getAttribute('aria-expanded') === 'false', 'Escape with the menu closed must change nothing');

/* A page change from the address bar focuses the new heading too. */
window.location.hash = '#/settings';
window.fire('hashchange', {});
check(shell.pageFromHash() === 'settings' && byId('page-settings').hidden === false
    && document.activeElement === byId('heading-settings'), 'a page change must focus the new page heading');
window.location.hash = '#no-such-page';
window.fire('hashchange', {});
check(shell.pageFromHash() === null && byId('page-settings').hidden === false,
    'a fragment that names no page must leave the current page alone');

/* Skip to content reaches the page content without leaving the page the operator is on. */
window.location.hash = '#main';
window.fire('hashchange', {});
check(byId('page-settings').hidden === false && byId('page-overview').hidden === true,
    'activating the skip link must keep the current page');

/* The API client carries the token on mutations only, and never reports a refusal as a success. */
const calls = [];
const client = load(render('{}', "'shell-token'"), async (path_, options) => {
    calls.push([path_, options]);
    return { ok: true, json: async () => ({ ok: true }) };
});
await client.shell.api.request('/api/status');
check(calls[0][1].headers['X-CSRF-Token'] === undefined, 'a read must not carry the token');
await client.shell.api.request('/api/action', { method: 'POST', body: { action: 'start' } });
check(calls[1][1].headers['X-CSRF-Token'] === 'shell-token' && calls[1][1].body === '{"action":"start"}',
    'a mutation must carry the CSRF token and its body');

const refused = load(html, async () => ({ ok: false, status: 400, json: async () => ({ error: 'Stop serving first.' }) }));
const failure = await refused.shell.api.request('/api/action', { method: 'POST', body: {} }).then(() => null, error => error);
check(failure !== null && failure.message === 'Stop serving first.' && failure.detail === 'Stop serving first.',
    'a JSON error body must reach the caller as the error it can show');

const silent = load(html, async () => ({ ok: false, status: 500, json: async () => { throw new Error('not json') } }));
check(await silent.shell.api.request('/api/action', { method: 'POST', body: {} }).then(() => null, error => error) !== null,
    'a failed request with no readable body must still fail');

const offline = load(html, async () => { throw new TypeError('offline') });
const unreachable = await offline.shell.api.request('/api/status').then(() => null, error => error);
check(unreachable !== null && /not answering/.test(unreachable.message), 'an unreachable manager must fail, never resolve');
const unconfirmed = await offline.shell.api.request('/api/action', { method: 'POST', body: {} }).then(() => null, error => error);
check(unconfirmed !== null && /not answering/.test(unconfirmed.message),
    'an action whose answer is lost must reject instead of resolving as success');
check((await offline.shell.api.request('/api/status', { method: 'HEAD' }).then(() => null, error => error)) !== null,
    'a refused HEAD must fail like any other refusal');

const noBody = load(html, async () => ({ ok: true, json: async () => { throw new Error('not json') } }));
check(await noBody.shell.api.request('/api/status').then(value => value === null, () => false),
    'a successful response with no readable body must resolve, not fail');

/* The header status, theme switch, section tabs, notice region and popup are empty slots. */
for (const slot of ['header-status', 'theme-switch', 'section-tabs', 'notices', 'popup'])
    check(document.querySelectorAll(`[data-slot="${slot}"]`).length > 0, `the shell must keep an empty ${slot} slot`);
check(document.querySelectorAll('[data-slot="section-tabs"]').length === 7
    && document.querySelectorAll('[data-slot="notices"]').length === 7,
    'every page must carry its own tab row and notice region for later tickets to fill');
}
main().then(() => {
    if (failures.length) {
        console.error('FAIL app shell:\n- ' + failures.join('\n- '));
        process.exit(1);
    }
    console.log('PASS: app shell navigation and API client');
}).catch(error => { console.error('FAIL app shell: ' + (error.stack || error)); process.exit(1) });