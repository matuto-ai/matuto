#!/usr/bin/env node
// Runs the app shell's own script against element doubles, extending the seam of
// agents/tests/page-behaviour.cjs, so the one popup component has a real seam. It proves the
// script's popup state, never layout, paint or focus rings; those are disposable browser probes.
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
        this.open = false;
        this.scrolled = 0;
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
        // A class attribute names the classes, so a node built by the script matches a class selector too.
        if (name === 'class') this.classes = new Set(String(value).split(/\s+/).filter(Boolean));
        if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
    }
    getAttribute(name) { return name in this.attributes ? this.attributes[name] : null }
    removeAttribute(name) {
        delete this.attributes[name];
        if (name === 'class') this.classes = new Set();
        if (name.startsWith('data-')) delete this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())];
    }
    append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node) } }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null }
    querySelectorAll(selector) { return selector.split(',').flatMap(part => find(this, part.trim().split(/\s+/))) }
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) }
    click() { for (const handler of this.listeners.click || []) handler({}) }
    get disabled() { return 'disabled' in this.attributes }
    set disabled(value) { value ? this.attributes.disabled = '' : delete this.attributes.disabled }
    focus() { const owner = this.ownerDocument; owner.activeElement = this.hidden ? owner.root : this }
    /* The dialog doubles model showModal/close as the open flag and the close event. */
    showModal() { this.open = true }
    close() {
        if (!this.open) return;
        this.open = false;
        for (const handler of this.listeners.close || []) handler({ target: this });
    }
    scrollIntoView() { this.scrolled += 1 }
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
        createElement(tag) { const node = new Element(tag); node.ownerDocument = this; return node },
        createTextNode(value) { return { textContent: String(value), children: [], parentNode: null } },
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

function load(html, fetchImpl = async () => { throw new Error('the popup must not call fetch on its own') }) {
    const { document, window } = build(html);
    const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
    const shell = new Function('document', 'window', 'fetch', script + '\nreturn {popup,api};')(document, window, fetchImpl);
    return { document, window, ...shell };
}

const failures = [];
const check = (condition, label) => { if (!condition) failures.push(label) };

/* ── Behaviour ───────────────────────────────────────────────────────────── */
async function main() {
const html = render('{}', "'shell-token'");
const { document, window, popup, api } = load(html);
const slot = document.querySelector('[data-slot="popup"]');
check(window.matutoPopup === popup, 'page scripts must use the shell-owned shared popup API');
check(slot !== null && slot.hidden === true && slot.children.length === 0,
    'the popup must fill the shell\'s reserved popup slot and leave it empty until a popup opens');

/* An opener is the control the operator pressed; the popup gives focus back to it. */
const opener = document.createElement('button');
opener.textContent = 'Stop serving';
document.getElementById('page-overview').append(opener);
const draft = document.createElement('input');
draft.value = 'half-written note';
opener.focus();

let confirmed = 0;
popup.preview({
    title: 'Stop serving?',
    stops: ['Agentic serving; consumers lose access'],
    starts: [],
    stays: ['Downloads and builds'],
    confirm: 'Stop serving',
    confirmAction: () => { confirmed += 1 },
});
const dialog = slot.children[0];
check(dialog.tagName === 'DIALOG' && dialog.open === true && slot.hidden === false,
    'a preview must open the shell\'s one popup dialog');
check(document.querySelectorAll('[data-slot="popup"] dialog').length === 1,
    'a second popup must reuse the one dialog, never add another');

/* The preview shows what the caller says it Stops, Starts and Stays. */
const groups = dialog.querySelector('.consequence');
check(groups !== null, 'a consequence preview must render its three groups');
const terms = groups.querySelectorAll('dt').map(term => term.textContent);
const details = groups.querySelectorAll('dd').map(detail => detail.textContent);
check(terms.join(',') === 'Stops,Starts,Stays', 'the preview must label Stops, Starts and Stays in that order');
check(details[0] === 'Agentic serving; consumers lose access' && details[1] === 'Nothing'
    && details[2] === 'Downloads and builds',
    'every group must be filled from the caller\'s own data, and an empty group reads as Nothing');

/* The alert sits below the title and is empty while nothing has failed. */
const heading = dialog.children[0];
const alert = dialog.querySelector('[role="alert"]');
check(heading.textContent === 'Stop serving?' && alert.hidden === true
    && alert.parentNode === dialog.querySelector('.popup-body'),
    'a fresh popup must carry its title and place its hidden alert in the scrollable review body');
check(dialog.querySelectorAll('[role="alert"]').length === 1, 'the popup must carry exactly one alert element');

/* Every popup opens on its safe choice, keeping the confirm action separate. */
const [safeChoice, confirmButton] = dialog.querySelectorAll('.popup-actions button');
check(document.activeElement === safeChoice && safeChoice.textContent === 'Cancel'
    && confirmButton.textContent === 'Stop serving',
    'a popup must open with focus on its safe choice, keeping the confirm action separate');
check(confirmed === 0, 'opening a preview must confirm nothing');

/* Escape closes the popup and returns focus to the control that opened it. */
document.fire('keydown', { key: 'Escape' });
check(dialog.open === false && slot.hidden === true && document.activeElement === opener,
    'Escape must close the popup and return focus to its opener');
check(confirmed === 0, 'closing without acting must confirm nothing');

/* Closing with the safe choice does the same. */
opener.focus();
popup.preview({ title: 'Stop serving?', stops: ['Agentic serving'], confirm: 'Stop serving', confirmAction: () => { confirmed += 1 } });
dialog.querySelectorAll('.popup-actions button')[0].click();
check(dialog.open === false && document.activeElement === opener && confirmed === 0,
    'taking the safe choice must close the popup without acting and return focus to its opener');

/* A refused confirmation stays in the same popup, with its context and the operator's draft. */
opener.focus();
let refusals = 0;
popup.preview({
    title: 'Remove Matuto?',
    stops: ['Agentic serving'],
    stays: ['Evidence'],
    content: draft,
    confirm: 'Remove',
    confirmAction: () => { refusals += 1; throw new Error(refusals === 1 ? 'Stop the running build first.' : 'The lab lease still holds the port.') },
});
const removal = dialog.querySelectorAll('.popup-actions button')[1];
removal.click();
await Promise.resolve();
check(dialog.open === true && heading.textContent === 'Remove Matuto?' && dialog.querySelector('.consequence') !== null,
    'a refused confirmation must leave the popup open with its title and review context');
check(alert.textContent === 'Stop the running build first.' && alert.hidden === false,
    'the failure must appear as one alert below the popup title');
check(document.activeElement === alert && alert.scrolled === 1,
    'the alert must take focus and be scrolled into view');
check(draft.value === 'half-written note' && draft.parentNode === dialog.querySelector('.popup-body'),
    'the operator\'s input must stay exactly as entered, inside the popup that still shows it');

/* A later failure replaces the alert instead of adding another. */
removal.click();
await Promise.resolve();
check(alert.textContent === 'The lab lease still holds the port.' && alert.hidden === false
    && dialog.querySelectorAll('[role="alert"]').length === 1,
    'a later failure must replace the one alert with its own message, never stack a second');

/* Closing clears the alert, and the next popup never shows it. */
dialog.querySelectorAll('.popup-actions button')[0].click();
check(dialog.open === false && alert.hidden === true && alert.textContent === '',
    'closing the popup must clear its alert');

/* A page that refreshes its opener can return focus to its replacement control. */
const settings = document.getElementById('page-settings');
const previousReview = document.createElement('button');
settings.append(previousReview);
let currentReview = previousReview;
previousReview.focus();
popup.open({
    title: 'Review storage change',
    confirm: 'Save both settings',
    returnFocus: () => currentReview,
    confirmAction: async () => {
        const parent = currentReview.parentNode;
        parent.children.splice(parent.children.indexOf(currentReview), 1);
        currentReview.parentNode = null;
        currentReview = document.createElement('button');
        settings.append(currentReview);
        throw new Error('Path saved, but a record remains unavailable.');
    },
});
dialog.querySelectorAll('.popup-actions button')[1].click();
await Promise.resolve();
check(dialog.open && alert.textContent.includes('Path saved'),
    'a committed partial outcome stays in the popup alert');
dialog.querySelectorAll('.popup-actions button')[0].click();
check(dialog.open === false && document.activeElement === currentReview,
    'closing after a refresh focuses the replacement Review control instead of a detached opener');

opener.focus();
popup.preview({ title: 'Serve voice?', stops: ['Nothing'], starts: ['Studio voice'], stays: [] });
check(dialog.open === true && alert.hidden === true && alert.textContent === ''
    && dialog.querySelectorAll('[role="alert"]').length === 1,
    'a popup opened after a closed one must show no alert from it');
dialog.querySelectorAll('.popup-actions button')[0].click();

/* The API client's refusal is shown in the popup and never closes it as a success. */
const refused = load(html, async () => ({ ok: false, status: 409, json: async () => ({
    error: 'A build holds the card.', blockers: [{ text: 'Build: HIP build (Stop)',
        href: '#/runs?work=build-1', label: 'Runs & jobs — Stop' }],
}) }));
refused.document.getElementById('heading-overview').focus();
refused.popup.preview({
    title: 'Serve voice?',
    stops: ['Agentic serving'],
    confirm: 'Serve voice',
    confirmAction: () => refused.api.request('/api/serving/voice', { method: 'POST', body: {} }),
});
const refusedConfirm = refused.document.querySelectorAll('[data-slot="popup"] .popup-actions button')[1];
/* The double's click() drops the handler's promise, so this runs the same handler directly. */
await refusedConfirm.listeners.click[0]({});
const refusedDialog = refused.document.querySelector('[data-slot="popup"] dialog');
const refusedAlert = refusedDialog.querySelector('[role="alert"]');
check(refusedDialog.open === true && refusedAlert.hidden === false
    && refusedAlert.textContent.includes('A build holds the card.'),
    'a refused request must leave the popup open showing the error the API client returned');
check(refusedAlert.querySelector('a')?.getAttribute('href') === '#/runs?work=build-1'
    && refusedAlert.querySelector('a')?.textContent === 'Runs & jobs — Stop',
    'a blocker that appears during confirmation must include its handling link in the popup');
check(refused.document.activeElement === refusedAlert, 'the refused request\'s alert must take focus');

/* A request left pending across a dismissal belongs to the popup that started it: the next
   popup keeps its own review and takes neither the old alert nor the old close. */
const tick = () => new Promise(resolve => setImmediate(resolve));
await (async () => {
    const stale = load(html);
    let reject;
    stale.popup.preview({
        title: 'Remove Matuto?',
        stops: ['Agentic serving'],
        confirm: 'Remove',
        confirmAction: () => new Promise((resolve, fail) => { reject = fail }),
    });
    const staleDialog = stale.document.querySelector('[data-slot="popup"] dialog');
    const staleActions = () => staleDialog.querySelectorAll('.popup-actions button');
    staleActions()[1].listeners.click[0]({});
    staleActions()[0].click();
    stale.popup.preview({ title: 'Serve voice?', stops: ['Nothing'], starts: ['Studio voice'] });
    reject(new Error('The old request failed.'));
    await tick();
    check(staleDialog.open === true && staleDialog.children[0].textContent === 'Serve voice?'
        && staleDialog.querySelector('[role="alert"]').hidden === true
        && staleDialog.querySelector('[role="alert"]').textContent === '',
        'a confirmation that fails after its popup was dismissed and replaced must not touch the new popup');
})();
await (async () => {
    const stale = load(html);
    let calls = 0, resolve;
    stale.popup.preview({
        title: 'Remove Matuto?',
        stops: ['Agentic serving'],
        confirm: 'Remove',
        confirmAction: () => { calls += 1; return new Promise(done => { resolve = done }) },
    });
    const staleDialog = stale.document.querySelector('[data-slot="popup"] dialog');
    const staleActions = () => staleDialog.querySelectorAll('.popup-actions button');
    staleActions()[1].listeners.click[0]({});
    staleActions()[0].click();
    stale.popup.preview({ title: 'Serve voice?', stops: ['Nothing'], starts: ['Studio voice'] });
    resolve();
    await tick();
    check(calls === 1 && staleDialog.open === true && staleDialog.children[0].textContent === 'Serve voice?',
        'a confirmation that succeeds after its popup was dismissed and replaced must not close the new popup');
})();

/* One in-flight confirmation per popup: a double-click dispatches the caller once, and a
   refusal re-enables the choice so the operator can retry. */
const double = load(html);
let restarts = 0, finishRestart;
double.popup.preview({
    title: 'Restart?',
    stops: ['Agentic serving'],
    confirm: 'Restart',
    confirmAction: () => { restarts += 1; return new Promise(resolve => { finishRestart = resolve }) },
});
const restart = double.document.querySelectorAll('[data-slot="popup"] .popup-actions button')[1];
restart.click();
restart.click();
await tick();
check(restarts === 1, 'a double-click must dispatch the confirmation once while it is pending');
finishRestart();
await tick();
check(double.document.querySelector('[data-slot="popup"] dialog').open === false,
    'the settled confirmation must still close the popup it belongs to');

const retry = load(html);
let attempts = 0;
retry.popup.preview({
    title: 'Restart?',
    stops: ['Agentic serving'],
    confirm: 'Restart',
    confirmAction: () => { attempts += 1; throw new Error('Stop serving first.') },
});
const retryButton = retry.document.querySelectorAll('[data-slot="popup"] .popup-actions button')[1];
await retryButton.listeners.click[0]({});
const retryDialog = retry.document.querySelector('[data-slot="popup"] dialog');
check(attempts === 1 && retryButton.disabled === false && retryDialog.querySelector('[role="alert"]').textContent === 'Stop serving first.',
    'a refused confirmation must re-enable its choice so the operator can retry in the same popup');
await retryButton.listeners.click[0]({});
check(attempts === 2, 'the retry must run the caller again');
}
main().then(() => {
    if (failures.length) {
        console.error('FAIL app popup:\n- ' + failures.join('\n- '));
        process.exit(1);
    }
    console.log('PASS: app popups, consequence previews and popup errors');
}).catch(error => { console.error('FAIL app popup: ' + (error.stack || error)); process.exit(1) });