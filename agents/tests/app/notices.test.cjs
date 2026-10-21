#!/usr/bin/env node
// Runs the shell's own script against element doubles and sample section files, so the shared
// notice region and the reason beside an unavailable control have a real seam. It proves the
// script's notice behaviour, never layout, paint or contrast; those are disposable browser
// probes. agents/tests/page-behaviour.cjs stays untouched.
'use strict';

const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const AGENTS = path.resolve(__dirname, '../../../agents');
const PYTHON = process.env.PYTHON || 'python3';
const PAGES = ['overview', 'model-files', 'profiles', 'runs', 'runtime', 'settings', 'console'];

/* ── Sample sections, so one page carries a tab row under its heading ────── */
const SECTIONS = { profiles: [['language', 'Language profiles', 1], ['speech-profiles', 'Speech profiles', 2]] };

function render() {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'matuto-notices-'));
    try {
        fs.cpSync(path.join(AGENTS, 'matuto'), path.join(root, 'matuto'), { recursive: true });
        for (const [page, list] of Object.entries(SECTIONS))
            for (const [name, title, order] of list)
                fs.writeFileSync(path.join(root, 'matuto', 'page', `${name}.py`),
                    `PAGE = ${JSON.stringify(page)}\nNAME = ${JSON.stringify(name)}\n` +
                    `TITLE = ${JSON.stringify(title)}\nORDER = ${order}\n\n\n` +
                    `def html(status):\n    return '<p id="panel-${name}">${name}</p>'\n`);
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
    get className() { return this.getAttribute('class') || '' }
    set className(value) { this.setAttribute('class', value) }
    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === 'class') this.classes = new Set(String(value).split(/\s+/).filter(Boolean));
        if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
    }
    getAttribute(name) { return name in this.attributes ? this.attributes[name] : null }
    hasAttribute(name) { return name in this.attributes }
    removeAttribute(name) {
        delete this.attributes[name];
        if (name.startsWith('data-')) delete this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())];
    }
    append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node) } }
    after(node) {
        if (!this.parentNode) return;
        const at = this.parentNode.children.indexOf(this) + 1;
        node.parentNode = this.parentNode;
        this.parentNode.children.splice(at, 0, node);
    }
    remove() {
        if (!this.parentNode) return;
        this.parentNode.children = this.parentNode.children.filter(child => child !== this);
        this.parentNode = null;
    }
    querySelector(selector) { const list = this.querySelectorAll(selector); return list.length ? list.item(0) : null }
    querySelectorAll(selector) { return new NodeList(selector.split(',').flatMap(part => find(this, part.trim().split(/\s+/)))) }
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) }
    click() { for (const handler of this.listeners.click || []) handler({}) }
    focus() { this.ownerDocument.activeElement = this }
}
// The selector shapes the shell's script uses: a tag, #id, .class, [attr] and descendant steps.
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

/* A live getElementById finds any node in the tree, including the ones the script just made. */
function elementWithId(node, id) {
    if (node.attributes && node.getAttribute('id') === id) return node;
    for (const child of node.children || []) {
        const found = elementWithId(child, id);
        if (found) return found;
    }
    return null;
}

function build(html) {
    const document = {
        root: new Element('body'),
        activeElement: null,
        createElement(tag) { const node = new Element(tag); node.ownerDocument = this; return node },
        getElementById(id) { return elementWithId(this.root, id) },
        querySelector(selector) { return this.root.querySelector(selector) },
        querySelectorAll(selector) { return this.root.querySelectorAll(selector) },
        addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) },
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
        for (const [, attribute, value] of tag.slice(name.length).matchAll(/([\w:-]+)="([^"]*)"/g)) node.setAttribute(attribute, value);
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
    return document;
}

function load(html) {
    const document = build(html);
    const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
    const window = { location: { hash: '' }, addEventListener() {} };
    const shell = new Function('document', 'window', 'fetch', script +
        '\nreturn {notice,clearNotice,reason};')(document, window, async () => ({ ok: true, json: async () => ({}) }));
    return { document, window, shell };
}

const failures = [];
const check = (condition, label) => { if (!condition) failures.push(label) };

/* ── Behaviour ───────────────────────────────────────────────────────────── */
async function main() {
const html = render();
const { document, window, shell } = load(html);
check(window.matutoReason === shell.reason, 'page scripts can use the shared reason helper');
const byId = id => document.getElementById(id);
const region = page => byId('page-' + page).querySelector('.notices');
const text = node => node.textContent;

/* Every page carries exactly one region, and it sits under the heading and the tab row. */
for (const page of PAGES) {
    const sections = byId('page-' + page);
    const regions = [...sections.querySelectorAll('.notices')];
    check(regions.length === 1, `${page} must carry exactly one notice region`);
    if (regions.length !== 1) continue;
    check(regions[0].parentNode === sections && region(page) === regions[0], `${page}'s region belongs to the page`);
    const order = sections.children.map(child => child.id || child.getAttribute('class'));
    check(order.indexOf('heading-' + page) < order.indexOf('notices') && order.indexOf('section-nav') < order.indexOf('notices'),
        `${page}'s region sits under its heading and section tabs`);
}
check(document.querySelectorAll('[data-slot="notices"]').length === PAGES.length,
    'the shell reserves one notice region per page and nothing else');

/* A blocker, a warning and a repair notice land in that one region, told apart in their words. */
check(region('profiles').children.length === 0, 'a page opens with an empty notice region');
shell.notice('profiles', 'storage', 'repair', 'Model storage is unavailable', '/mnt/models cannot be read.',
    { label: 'Repair model storage', href: '#/settings/storage-artifacts' });
shell.notice('profiles', 'slot', 'warning', 'Another job holds the shared slot', 'qualify-demo is running.');
shell.notice('profiles', 'runtime', 'blocker', 'No runtime is selected', 'Pick a build and a card first.');
const shown = [...region('profiles').children];
check(shown.length === 3, 'several notices stack in the one region');
check(shown.every(node => /^Blocker: |^Warning: |^Repair needed: /.test(text(node))),
    'each notice names its kind in text, not by colour alone');
check(text(shown[0]).startsWith('Repair needed: Model storage is unavailable')
    && text(shown[0]).includes('/mnt/models cannot be read.')
    && shown[0].getAttribute('data-kind') === 'repair', 'a repair notice names its kind, its detail and where it is handled');
check(shown[0].querySelector('a').getAttribute('href') === '#/settings/storage-artifacts', 'a notice link points where it is handled');
const prerequisiteNotice = shell.notice('profiles', 'prerequisites', 'blocker',
    'This profile needs a selected runtime and a card first.', 'Complete each missing prerequisite.', [
        {label: 'Runtime › Install a version', href: '#/runtime/install'},
        {label: 'Settings › Cards', href: '#/settings/cards'},
    ]);
check(prerequisiteNotice.querySelectorAll('a').length === 2
    && prerequisiteNotice.querySelectorAll('a').item(0).getAttribute('href') === '#/runtime/install'
    && prerequisiteNotice.querySelectorAll('a').item(1).textContent === 'Settings › Cards'
    && prerequisiteNotice.textContent.includes('Runtime › Install a version · Settings › Cards'),
    'one notice can name each missing prerequisite with a separated destination link');
shell.clearNotice('profiles', 'prerequisites');
check(text(shown[1]).startsWith('Warning: ') && text(shown[2]).startsWith('Blocker: '),
    'a warning and a blocker read as their own kinds');
check([...document.querySelectorAll('.notice')].length === 3, 'a page-wide notice appears nowhere but the region');
check(PAGES.filter(page => page !== 'profiles').every(page => region(page).children.length === 0),
    'a page-wide notice never reaches another page');

/* One key per condition: the same notice is replaced, and a cleared one leaves. */
shell.notice('profiles', 'slot', 'warning', 'Another job holds the shared slot', 'qualify-demo2 is running.');
check(region('profiles').children.length === 3 && text(region('profiles').children[1]).includes('qualify-demo2'),
    'the same condition replaces its notice instead of stacking a second');
shell.clearNotice('profiles', 'slot');
check(region('profiles').children.length === 2, 'a notice whose condition cleared leaves the region');
shell.clearNotice('profiles', 'slot');
check(region('profiles').children.length === 2 && document.querySelectorAll('.notice').length === 2,
    'clearing a notice that already left changes nothing');
shell.clearNotice('profiles', 'storage');
check(region('profiles').children.length === 1 && text(region('profiles').children[0]).startsWith('Blocker: '),
    'each key clears only its own notice');

/* A single unavailable control states its reason beside it, as its accessible description,
   and stays reachable by keyboard so a page can explain the refusal on activation. */
const page = byId('page-profiles');
const serve = (id, label) => {
    const control = document.createElement('button');
    control.setAttribute('id', id);
    control.textContent = label;
    page.append(control);
    return control;
};
const start = serve('start', 'Start agentic serving');
const stop = serve('stop', 'Stop serving');
check(start.getAttribute('aria-disabled') === null && !start.hasAttribute('disabled'),
    'a control starts available');
shell.reason(start, 'Stop serving first, or finish the run that holds the card.');
start.focus();
check(document.activeElement === start, 'an unavailable control stays focusable');
const described = start.getAttribute('aria-describedby');
check(Boolean(described) && byId(described) && byId(described).textContent === 'Stop serving first, or finish the run that holds the card.',
    'the reason is the control\'s accessible description');
const beside = start.parentNode.children[start.parentNode.children.indexOf(start) + 1];
check(beside === byId(described) && text(beside) === byId(described).textContent, 'the reason is shown next to its control');
check(start.getAttribute('aria-disabled') === 'true', 'a control with a reason is marked unavailable, not disabled');

/* Every reason keeps its own id and its own text, and clears only itself. */
shell.reason(stop, 'Nothing is serving.');
check(byId(described).textContent === 'Stop serving first, or finish the run that holds the card.'
    && byId(stop.getAttribute('aria-describedby')).textContent === 'Nothing is serving.'
    && stop.getAttribute('aria-describedby') !== start.getAttribute('aria-describedby'),
    'two unavailable controls each state their own reason beside themselves');
shell.reason(start, 'Select a card first.');
check(byId(stop.getAttribute('aria-describedby')).textContent === 'Nothing is serving.',
    'a re-stated reason leaves another control\'s reason alone');

/* A control that no longer refuses is usable again, with no reason left describing it. */
shell.reason(start, '');
check(!byId(described) && start.getAttribute('aria-describedby') === null && start.getAttribute('aria-disabled') === null,
    'a cleared reason leaves and its control is available again');
start.focus();
check(document.activeElement === start, 'a control whose reason cleared is focusable and not disabled');
shell.reason(start, '');
check(page.querySelectorAll('.reason').length === 1, 'clearing a reason twice leaves one reason beside the other control');

/* Reasons are the control's own: an id-less control still gets one, and it stays beside it. */
const anonymous = document.createElement('button');
anonymous.textContent = 'Unnamed control';
page.append(anonymous);
const given = shell.reason(anonymous, 'Choose a recipe first.');
check(Boolean(given.getAttribute('id')) && given.parentNode === page
    && page.children[page.children.indexOf(anonymous) + 1] === given
    && anonymous.getAttribute('aria-describedby') === given.getAttribute('id'),
    'a control without an id still states its reason beside it and describes itself with it');

/* A reason never takes over or erases a description the control already had. */
const help = document.createElement('p');
help.setAttribute('id', 'publish-why');
help.textContent = 'Publication takes a few seconds.';
page.append(help);
const describedControl = serve('describe', 'Publish');
describedControl.setAttribute('aria-describedby', 'publish-why');
shell.reason(describedControl, 'Qualify the profile first.');
const owned = describedControl.getAttribute('aria-describedby').split(' ');
check(owned.length === 2 && owned.includes('publish-why') && byId(owned[1] === 'publish-why' ? owned[0] : owned[1]).textContent === 'Qualify the profile first.',
    'a stated reason is added beside the control\'s own description, not over it');
const ownedId = describedControl.getAttribute('aria-describedby').replace('publish-why ', '');
shell.reason(describedControl, '');
check(describedControl.getAttribute('aria-describedby') === 'publish-why' && !byId(ownedId),
    'clearing a reason leaves the description the control already had');
shell.reason(describedControl, '');
check(describedControl.getAttribute('aria-describedby') === 'publish-why',
    'clearing a control that never had a reason leaves its own description alone');

/* An id the page already uses cannot become the reason, or the control would describe the wrong text. */
const taken = document.createElement('p');
taken.setAttribute('id', 'serve-reason');
taken.textContent = 'Existing page help.';
page.append(taken);
const collisionControl = serve('serve', 'Serve voice');
const collision = shell.reason(collisionControl, 'Choose a card first.');
check(collision.getAttribute('id') !== 'serve-reason'
    && collisionControl.getAttribute('aria-describedby') === collision.getAttribute('id'),
    'a reason takes an id no other element already uses');

/* The notice and reason text a page cannot wrap itself must not widen the page either. */
const styles = html.slice(html.indexOf('<style>') + 7, html.indexOf('</style>'));
check(styles.includes('.notice,.reason{overflow-wrap:anywhere}'),
    'notice and reason text wrap a long unbroken run instead of widening the page');

/* Exercise a refused action through the actual action script and shared reason helper. */
const languagePage = byId('languageProfiles');
const profileList = byId('languageProfileList');
const profileRow = document.createElement('article');
profileRow.setAttribute('data-profile-id', 'live-language');
profileList.append(profileRow);
window.matutoPopup = {open() {}};
const actionScript = fs.readFileSync(path.join(AGENTS, 'matuto/static/language-profile-actions.js'), 'utf8');
new Function('document', 'window', 'fetch', 'reason', 'Event', actionScript)(document, window,
    async () => ({ok:true, json:async () => ({profiles:[{profile_id:'live-language',name:'Live profile',
        alias:'live',published:true,qualification:{state:'qualified'},
        unpublish_reason:'Stop serving on Overview first.',remove_reason:'Published profile; unpublish before removing.'}]})}),
    shell.reason, Event);
await new Promise(resolve => setImmediate(resolve));
const actionButtons = profileRow.querySelector('.language-profile-record-actions');
for (const [label, reasonText] of [['Unpublish…','Stop serving on Overview first.'],
    ['Remove','Published profile; unpublish before removing.']]) {
    const index = actionButtons.children.findIndex(child => child.textContent === label);
    const control = actionButtons.children[index], description = actionButtons.children[index + 1];
    check(control.getAttribute('aria-disabled') === 'true' && control.getAttribute('aria-describedby') === description.getAttribute('id')
        && description.textContent === reasonText && description.parentNode === actionButtons,
        `${label} refusal is visibly and accessibly described through the shared helper`);
}

/* A control the page rendered disabled still becomes activatable, or it could never explain itself. */
const disabled = document.createElement('button');
disabled.setAttribute('id', 'publish');
disabled.setAttribute('disabled', '');
page.append(disabled);
shell.reason(disabled, 'Qualify the profile first.');
disabled.focus();
check(document.activeElement === disabled && !disabled.hasAttribute('disabled'),
    'a stated reason must leave the control activatable, whatever the page rendered');
}
main().then(() => {
if (failures.length) {
    console.error('FAIL app page notices:\n- ' + failures.join('\n- '));
    process.exit(1);
}
console.log('PASS: app page notices and control reasons');
}).catch(error => {
    console.error('FAIL app page notices: ' + (error.stack || error));
    process.exit(1);
});