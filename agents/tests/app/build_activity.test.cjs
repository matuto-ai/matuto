#!/usr/bin/env node
// Exercise Build activity's rendering and actions through element doubles.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
    constructor(tag, id = '') {
        this.tagName = tag.toUpperCase(); this.id = id; this.children = []; this.attributes = {};
        this.listeners = {}; this.text = ''; this.hidden = false; this.parentNode = null;
    }
    set textContent(value) {
        this.text = String(value);
        for (const child of this.children) child.parentNode = null;
        this.children = [];
    }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    append(...children) {
        for (const child of children) {
            if (child.parentNode) child.parentNode.children.splice(child.parentNode.children.indexOf(child), 1);
            child.parentNode = this;
            this.children.push(child);
        }
    }
    insertBefore(child, before) {
        if (child.parentNode) child.parentNode.children.splice(child.parentNode.children.indexOf(child), 1);
        const index = before ? this.children.indexOf(before) : this.children.length;
        child.parentNode = this;
        this.children.splice(index, 0, child);
    }
    remove() {
        if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
        this.parentNode = null;
    }
    contains(element) { return this === element || this.children.some(child => child.contains(element)); }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    focus() { document.activeElement = this; }
    fire(name, event = {}) { return Promise.all((this.listeners[name] || []).map(fn => fn(event))); }
}

const tick = () => new Promise(resolve => setImmediate(resolve));
const script = fs.readFileSync(path.resolve(__dirname, '../../../agents/matuto/static/build_activity.js'), 'utf8');
async function main() {
    const list = new Element('div', 'build-activity-list');
    const elements = {'build-activity-list': list};
    const document = {activeElement: null,
        getElementById(id) { return elements[id]; },
        createElement(tag) { return new Element(tag); }};
    const data = {csrf: 'fresh-token', contention: true, builds: [
        {id: 'build-live', recipe: 'llama.cpp HIP', ref: 'main', commit: 'a'.repeat(40), target: 'gfx1201',
            startedAt: '2026-10-13T10:00:00Z', startedBy: 'operator', state: 'running', outcome: '',
            phase: 'building', step: '2 of 3', elapsed: 17, log: 'redacted tail', logTruncated: false,
            failureReason: '', provenance: {}, outputDigests: {}, canStop: true, canRetry: false,
            canDeleteHistory: false},
        {id: 'build-done', recipe: 'Breeze synthesis (Vulkan)', ref: 'release', commit: 'b'.repeat(40), target: '',
            startedAt: '2026-10-12T10:00:00Z', startedBy: 'operator', state: 'completed', outcome: 'completed',
            phase: '', step: '', elapsed: 0, log: '', failureReason: '',
            provenance: {origin: 'official'}, outputDigests: {server: {sha256: 'digest'}},
            canStop: false, canRetry: false, canDeleteHistory: true},
        {id: 'build-error', recipe: 'whisper recognition (CPU)', ref: 'stable', commit: 'c'.repeat(40), target: '',
            startedAt: '2026-10-11T10:00:00Z', startedBy: 'operator', state: 'errored', outcome: 'errored',
            phase: '', step: '', elapsed: 0, log: '', failureReason: 'cmake exited 1',
            provenance: {recipe: 'whisper'}, outputDigests: {},
            canStop: false, canRetry: true, canDeleteHistory: true},
    ]};
    const calls = [];
    let nextResponse = null;
    const fetch = async (url, options = {}) => {
        calls.push({url, options});
        if (nextResponse) { const response = nextResponse; nextResponse = null; return response; }
        return {ok: true, headers: {get: () => 'application/json'}, json: async () => data};
    };
    const popups = [];
    const notices = [];
    const cleared = [];
    let poll;
    const window = {matutoCsrf: 'old-token', setInterval(fn) { poll = fn; },
        setTimeout(fn) { setImmediate(fn); },
        matutoPopup: {open: popup => popups.push(popup), preview: popup => popups.push(popup)},
        matutoReason: (control, reason) => {
            control.reason = reason;
            if (reason) control.setAttribute('aria-describedby', 'retry-reason');
            else delete control.attributes['aria-describedby'];
        }};
    vm.runInNewContext(script, {document, window, fetch, setInterval: window.setInterval,
        notice: (...args) => notices.push(args), clearNotice: (...args) => cleared.push(args),
        Date, Object, String, JSON, Error, Promise, encodeURIComponent});
    await tick();

    const check = (condition, message) => { if (!condition) throw new Error(message); };
    check(list.children.length === 3,
        'all recorded builds must appear');
    const live = list.children.find(row => row.textContent.includes('build-live'));
    check(live && live.textContent.includes('Pinned ref: main; commit: ' + 'a'.repeat(40) + '; target gfx1201.'),
        'the active HIP build must show its full pin and target');
    check(live.textContent.includes('Started by operator') && live.textContent.includes('Phase: building')
        && live.textContent.includes('Current step: 2 of 3') && live.textContent.includes('redacted tail'),
        'a running build must show its owner, progress and redacted log tail');
    const links = live.children.filter(child => child.tagName === 'A');
    check(links.some(link => link.getAttribute('href') === '#/runs?work=build-live' && link.textContent === 'Runs & jobs'),
        'each build must name its Runs & jobs destination');
    const done = list.children.find(row => row.textContent.includes('build-done'));
    check(done.textContent.includes('Outcome: completed')
        && done.textContent.includes('Continue to Runtime › Speech runtimes'),
        'a completed speech build must link to its speech runtime inventory');
    check(done.textContent.includes('Kept build provenance and output digests'),
        'ended build details must keep provenance and digests visible');
    const failed = list.children.find(row => row.textContent.includes('build-error'));
    check(failed.textContent.includes('Outcome: errored') && failed.textContent.includes('cmake exited 1'),
        'an errored build must display its failure reason');
    check(notices.length === 1 && notices[0][1] === 'build-activity-contention'
        && notices[0][4].includes('Matuto does not stop or restart')
        && notices[0][5].label === 'Open Runs & jobs',
        'agentic serving beside a build must get the shared contention warning');

    const button = (row, label) => row.children.find(child => child.tagName === 'DIV')
        .children.find(child => child.textContent === label);
    const stop = button(live, 'Stop…');
    await stop.fire('click');
    check(popups[0].stops[0].includes('staging is never installed')
        && popups[0].starts.length === 0 && popups[0].stays.includes('the runtime selection'),
        'Stop must preview what stops, starts and stays');
    await popups[0].confirmAction();
    check(calls.some(call => call.url === '/api/build-activity'
        && call.options.headers['X-CSRF-Token'] === 'fresh-token'
        && JSON.parse(call.options.body).action === 'stop'),
        'Stop must use the authenticated API and current CSRF token');

    const retry = button(failed, 'Retry');
    nextResponse = {ok: false, headers: {get: () => 'application/json'},
        json: async () => ({error: 'Stop serving on Overview before retrying.'})};
    await retry.fire('click');
    check(retry.reason.includes('Stop serving on Overview')
        && calls.some(call => JSON.parse(call.options.body || '{}').action === 'retry'),
        'Retry must act directly and show a serving refusal beside its control');

    const remove = button(done, 'Delete history…');
    await remove.fire('click');
    check(popups[1].title === 'Delete build history?'
        && popups[1].content.textContent.includes('log will be removed')
        && popups[1].content.textContent.includes('output digests')
        && popups[1].safe === 'Keep history',
        'Delete history must confirm log removal while keeping provenance and digests');
    await popups[1].confirmAction();
    check(calls.some(call => JSON.parse(call.options.body || '{}').action === 'delete-history'),
        'history deletion must use its dedicated API action');
    data.builds[1].historyLogEmpty = true;
    await poll();
    check(done.children.find(child => child.tagName === 'DETAILS').open,
        'deleted history must open the retained provenance and digest details');
    document.activeElement = stop;
    data.builds.unshift({...data.builds[0], id: 'build-retried', commit: 'd'.repeat(40),
        startedAt: '2026-10-13T11:00:00Z'});
    await poll();
    check(list.children[0].textContent.includes('build-retried') && list.children[1] === live
        && document.activeElement === stop,
        'a newly polled build must move to the top without replacing rows or stealing focus');

    data.builds = []; data.contention = false;
    await poll();
    const emptyLink = list.children[0].children.find(child => child.tagName === 'A');
    check(list.textContent.includes('No runtime builds yet') && emptyLink
        && emptyLink.textContent === 'Open Runtime › Install a version'
        && emptyLink.getAttribute('href') === '#/runtime/install',
        'an empty history must link to Runtime › Install a version');
    check(cleared.some(args => args[0] === 'runtime' && args[1] === 'build-activity-contention'),
        'the contention warning must clear when serving or the build ends');
    nextResponse = {ok: false, headers: {get: () => 'application/json'},
        json: async () => ({error: 'The build service is unavailable.'})};
    await poll();
    check(notices.some(args => args[1] === 'build-activity-error' && args[2] === 'blocker'
        && args[4] === 'The build service is unavailable.'),
        'page-wide fetch failures must use the shared Runtime notice region');
    await poll();
    check(cleared.some(args => args[0] === 'runtime' && args[1] === 'build-activity-error'),
        'the page-wide error notice must clear when the API recovers');
    process.stdout.write('PASS: Build activity rendering, progress and actions\n');
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
