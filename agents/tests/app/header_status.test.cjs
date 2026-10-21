#!/usr/bin/env node
// Exercise the status poller against the shell's actual status, page and notice slots.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SCRIPT = fs.readFileSync(path.resolve(__dirname, '../../matuto/static/header_status.js'), 'utf8');
const failures = [];
const check = (condition, label) => { if (!condition) failures.push(label) };

class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.dataset = {}; this.text = ''; this.parentNode = null; this.ownerDocument = null }
    setAttribute(name, value) { this.attributes[name] = String(value) }
    getAttribute(name) { return this.attributes[name] ?? null }
    removeAttribute(name) { delete this.attributes[name] }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return this.text || this.children.map(child => child.textContent).join('') }
    append(node) { this.insertBefore(node, null) }
    insertBefore(node, reference) {
        if (node.parentNode) node.remove();
        const index = reference === null ? this.children.length : this.children.indexOf(reference);
        this.children.splice(index, 0, node); node.parentNode = this; node.ownerDocument = this.ownerDocument;
    }
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(item => item !== this); this.parentNode = null }
    focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this }
    querySelector(selector) {
        const match = /^\[([^=]+)="([^"]+)"\]$/.exec(selector);
        const found = node => match ? node.getAttribute(match[1]) === match[2] :
            selector.startsWith('.') ? (node.getAttribute('class') || '').split(/\s+/).includes(selector.slice(1)) : false;
        const walk = node => { for (const child of node.children) { if (found(child)) return child; const nested = walk(child); if (nested) return nested } return null };
        return walk(this);
    }
}

function fixture(fetchImpl, { realTimers = false } = {}) {
    const slot = new Element('div');
    slot.setAttribute('data-slot', 'header-status');
    const main = new Element('main');
    main.textContent = 'protected content';
    const popup = new Element('div');
    popup.setAttribute('data-slot', 'popup');
    popup.textContent = 'open popup';
    const pages = {};
    for (const name of ['overview', 'settings', 'runs']) {
        const page = pages[name] = new Element('section');
        const notices = new Element('div');
        notices.setAttribute('class', 'notices');
        page.append(notices);
    }
    const timers = [];
    const body = new Element('body');
    const document = {
        body,
        activeElement: null,
        createElement(tag) { const node = new Element(tag); node.ownerDocument = this; return node },
        querySelector: selector => selector === '[data-slot="header-status"]' ? slot :
            selector === '#main' ? main : selector === '[data-slot="popup"]' ? popup : null,
        getElementById: id => pages[id.replace(/^page-/, '')] || null,
    };
    for (const node of [body, slot, main, popup, ...Object.values(pages)]) node.ownerDocument = document;
    for (const page of Object.values(pages)) page.children[0].ownerDocument = document;
    const window = {
        location: { hash: '#/overview', pathname: '/', search: '', replace(value) { this.replaced = value } },
        fetch: fetchImpl,
        addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) },
        listeners: {},
    };
    const context = { document, window, AbortController, Response,
        setTimeout: realTimers ? setTimeout : (callback, delay) => { const timer = { callback, delay }; timers.push(timer); return timer },
        clearTimeout: realTimers ? clearTimeout : timer => { if (timer) timer.cleared = true },
    };
    const status = vm.runInNewContext(SCRIPT.replace('headerStatus.poll();', '') + '\nheaderStatus;', context);
    return { status, slot, pages, timers, window, document, body, main, popup };
}

const ITEMS = [
    { type: 'serving', label: 'Not serving', href: '#/overview/serving' },
    { type: 'current-work', label: 'Nothing running', href: '#/runs' },
    { type: 'repair', label: 'Storage unavailable', href: '#/settings/storage-artifacts' },
];

async function main() {
    const online = fixture(async () => ({ ok: true, status: 200, json: async () => ({ items: ITEMS }) }));
    await online.status.refresh();
    const focusedLink = online.slot.children[0];
    focusedLink.focus();
    await online.status.refresh();
    check(online.slot.children[0] === focusedLink && online.document.activeElement === focusedLink,
        'a successful unchanged poll keeps the focused status link connected');
    check(online.slot.children.length === 3 && online.slot.children.map(item => item.textContent).join('|') ===
        'Not serving|Nothing running|Storage unavailable', 'the header shows ordered serving, current-work and repair items');
    check(online.slot.children.every(item => item.tagName === 'a' && item.getAttribute('href')),
        'current status items link to their handling pages');

    const results = [
        async () => ({ ok: true, status: 200, json: async () => ({ items: ITEMS }) }),
        async () => { throw new TypeError('offline') },
        async () => ({ ok: true, status: 200, json: async () => ({ items: ITEMS }) }),
    ];
    const recovering = fixture(() => results.shift()());
    await recovering.status.refresh();
    const onlineFocused = recovering.slot.children[0];
    onlineFocused.focus();
    await recovering.status.refresh();
    check(recovering.status.lost && recovering.slot.children.length === 4 &&
        recovering.slot.children.slice(0, 3).every(item => item.tagName === 'span' &&
            item.textContent.endsWith('(last known)')),
        'a failed check keeps each prior value marked last known and removes its link');
    check(recovering.document.activeElement === recovering.slot.children[0],
        'the focused status item remains focused when its online link becomes stale text');
    check(recovering.slot.children[3].textContent === 'Matuto is not answering',
        'lost contact adds the header marker');
    const overviewNotice = recovering.pages.overview.querySelector('[data-notice="lost-contact"]');
    check(overviewNotice && /last known/.test(overviewNotice.textContent) &&
        /serving and jobs may still be running/.test(overviewNotice.textContent) &&
        /app-menu entry/.test(overviewNotice.textContent), 'the notice explains retained workload state and app-menu recovery');
    recovering.window.location.hash = '#/settings/storage-artifacts';
    for (const listener of recovering.window.listeners.hashchange) listener();
    check(recovering.pages.overview.querySelector('[data-notice="lost-contact"]') === null &&
        recovering.pages.settings.querySelector('[data-notice="lost-contact"]') !== null,
        'the one lost-contact notice follows the currently shown page');
    await recovering.status.refresh();
    check(!recovering.status.lost && recovering.slot.children.every(item => item.tagName === 'a') &&
        recovering.pages.settings.querySelector('[data-notice="lost-contact"]') === null &&
        recovering.document.activeElement === recovering.slot.children[0],
        'contact returning restores current links and keeps focus without reloading');

    const timedOut = fixture(() => new Promise(() => {}));
    const unanswered = timedOut.status.refresh();
    await Promise.resolve();
    const deadline = timedOut.timers.find(timer => timer.delay === 1500);
    deadline.callback();
    await unanswered;
    check(timedOut.status.lost, 'an unanswered check is marked lost within the 1.5-second deadline');

    const interrupted = fixture(async (_path, options = {}) => options.method === 'POST'
        ? { ok: true, status: 200, clone() { return { text: async () => { throw new Error('truncated body') } } } }
        : { ok: true, status: 200, json: async () => ({ items: ITEMS }) });
    await interrupted.status.refresh();
    const failedMutation = await interrupted.window.fetch('/api/language-profiles', { method: 'POST' })
        .then(() => null, error => error);
    const unconfirmed = interrupted.pages.overview.querySelector('[data-notice="action-unconfirmed"]');
    check(failedMutation && unconfirmed && /not confirmed/i.test(unconfirmed.textContent),
        'an unreadable mutation reply is rejected and explicitly marked unconfirmed');

    const unansweredMutation = fixture(() => new Promise(() => {}));
    unansweredMutation.window.fetch('/api/action', { method: 'POST' });
    await Promise.resolve();
    const pendingNotice = unansweredMutation.pages.overview.querySelector('[data-notice="action-unconfirmed"]');
    check(pendingNotice && /pending/i.test(pendingNotice.textContent) && /not confirmed/i.test(pendingNotice.textContent),
        'an unanswered mutation request remains visibly pending rather than done');

    const unansweredBody = fixture(async () => ({clone() { return {text: () => new Promise(() => {})}; }}));
    unansweredBody.window.fetch('/api/action', { method: 'POST' });
    await new Promise(resolve => setImmediate(resolve));
    const pendingBodyNotice = unansweredBody.pages.overview.querySelector('[data-notice="action-unconfirmed"]');
    check(pendingBodyNotice && /pending/i.test(pendingBodyNotice.textContent) && /not confirmed/i.test(pendingBodyNotice.textContent),
        'an unreadable pending mutation body remains visibly unconfirmed');

    const lateReply = {ok: true, clone() { return {text: async () => '<!doctype html>'}; }};
    let completeLateReply;
    const lateMutation = fixture((_path, options = {}) => new Promise((resolve, reject) => {
        completeLateReply = resolve;
        options.signal?.addEventListener('abort', () => reject(new Error('aborted')));
    }));
    const lateRequest = lateMutation.window.fetch('/api/runtime-install', {method: 'POST'})
        .then(response => response, error => error);
    await Promise.resolve();
    for (const timer of lateMutation.timers) if (!timer.cleared) timer.callback();
    completeLateReply(lateReply);
    check(await lateRequest === lateReply &&
        lateMutation.pages.overview.querySelector('[data-notice="action-unconfirmed"]') === null,
        'a successful multi-step operation can return after the old blanket cutoff would have fired');

    const slowReply = {ok: true, status: 200, clone() { return {text: async () => '<!doctype html>'}; }};
    const slowMutation = fixture((_path, options) => new Promise(resolve => setTimeout(() => resolve(slowReply), 2000)),
        {realTimers: true});
    const delayedReply = slowMutation.window.fetch('/api/runtime-install', {method: 'POST'})
        .then(response => response, error => error);
    await new Promise(resolve => setTimeout(resolve, 1700));
    const stillPending = slowMutation.pages.overview.querySelector('[data-notice="action-unconfirmed"]');
    check(stillPending && /pending/i.test(stillPending.textContent) && /not confirmed/i.test(stillPending.textContent),
        'a valid operation that takes longer than the status deadline stays pending and unconfirmed');
    const slowResponse = await delayedReply;
    check(slowResponse === slowReply &&
        slowMutation.pages.overview.querySelector('[data-notice="action-unconfirmed"]') === null,
        'a slower complete non-JSON mutation reply remains usable and clears the pending notice');

    let resolveExpired;
    let markStarted;
    const checkStarted = new Promise(resolve => { markStarted = resolve; });
    let statusCalls = 0;
    const resultOnly = fixture(() => {
        statusCalls++;
        markStarted();
        return new Promise(resolve => { resolveExpired = resolve; });
    });
    const pendingStatus = resultOnly.status.refresh();
    await checkStarted;
    resultOnly.body.dataset.matutoResultOnly = 'true';
    resolveExpired({ ok: false, status: 401,
        json: async () => ({ error: 'sign-in is needed' }) });
    await pendingStatus;
    await resultOnly.status.refresh();
    await resultOnly.status.poll();
    const backgroundPoll = await resultOnly.window.fetch('/api/build-activity');
    check(statusCalls === 1 && backgroundPoll.status === 409 &&
        resultOnly.window.location.replaced === undefined &&
        resultOnly.main.textContent === 'protected content' &&
        resultOnly.timers.every(timer => timer.cleared || timer.delay !== 500),
        'a result-only removal view ignores in-flight redirects and blocks later shell polls before fetch');

    const hangingResult = fixture(() => new Promise(() => {}));
    const hangingCheck = hangingResult.status.refresh();
    await Promise.resolve();
    hangingResult.body.dataset.matutoResultOnly = 'true';
    hangingResult.timers.find(timer => timer.delay === 1500).callback();
    await hangingCheck;
    check(!hangingResult.status.lost && hangingResult.slot.children.length === 0,
        'a timed-out in-flight status check cannot alter a result-only removal page');

    const expired = fixture(async () => ({ ok: false, status: 401,
        json: async () => ({ error: 'sign-in is needed' }) }));
    expired.window.location.hash = '#/settings/account';
    await expired.status.refresh();
    check(expired.window.location.replaced === '/sign-in?next=%2F%23%2Fsettings%2Faccount&ended=1' &&
        expired.main.textContent === '' && expired.popup.textContent === '' && expired.slot.textContent === '',
        'a confirmed sign-in refusal clears protected content and opens the session-ended form for this page');
}

main().then(() => {
    if (failures.length) { console.error('FAIL header status:\n- ' + failures.join('\n- ')); process.exit(1) }
    console.log('PASS: header status and lost-contact behavior');
}).catch(error => { console.error('FAIL header status: ' + (error.stack || error)); process.exit(1) });
