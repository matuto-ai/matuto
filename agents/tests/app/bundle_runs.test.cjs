#!/usr/bin/env node
// Exercise the bundle-run section at its browser-state seam with element doubles.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
    constructor(tag, id = '') {
        this.tagName = tag.toUpperCase(); this.id = id; this.children = []; this.attributes = {};
        this.listeners = {}; this.text = ''; this.value = ''; this.hidden = false; this.selectedOptions = [];
        this.elements = {}; this.parentNode = null; this._selectedIndex = -1;
    }
    set textContent(value) {
        this.text = String(value);
        for (const child of this.children) child.parentNode = null;
        this.children = []; this.selectedOptions = []; this._selectedIndex = -1;
    }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    set selectedIndex(index) {
        this._selectedIndex = index;
        const option = this.children[index];
        this.selectedOptions = option ? [option] : [];
        if (option) this.value = option.getAttribute('value');
    }
    get selectedIndex() { return this._selectedIndex; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    append(...children) {
        for (const child of children) {
            if (child.parentNode) child.parentNode.children.splice(child.parentNode.children.indexOf(child), 1);
            child.parentNode = this; this.children.push(child);
        }
    }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    querySelector(selector) { return selector === 'button[type="submit"]' ? this.submitButton : null; }
    fire(name, event = {}) { return Promise.all((this.listeners[name] || []).map(fn => fn(event))); }
}

const tick = () => new Promise(resolve => setImmediate(resolve));
const source = fs.readFileSync(path.resolve(__dirname, '../../../agents/matuto/static/bundle_runs.js'), 'utf8');
async function main() {
    const start = new Element('form', 'bundle-runs-start');
    start.elements.bundle = new Element('select');
    const submit = new Element('button'); start.submitButton = submit;
    const reason = new Element('p');
    const error = new Element('p'); error.hidden = true;
    const current = new Element('div'); const history = new Element('div');
    const elements = {'bundle-runs-start': start, 'bundle-runs-reason': reason,
        'bundle-runs-error': error, 'bundle-runs-current': current, 'bundle-runs-history': history};
    const document = {getElementById(id) { return elements[id]; },
        createElement(tag) { return new Element(tag); }};
    const data = {csrf: 'token', records: [
        {id: 'lang-1', name: 'Language', kind: 'language'},
        {id: 'bundle-1', name: 'Voice', kind: 'bundle'},
        {id: 'bundle-2', name: 'Workshop voice', kind: 'bundle'}], entries: [
        {id: 'active-1', kind: 'bundle', procedure: 'bundle', record: 'Voice', state: 'running', outcome: '',
            progress: {phase: 'loading bundle parts', step: 'starting recognition', elapsed: 1}},
        {id: 'failed-1', kind: 'bundle', procedure: 'bundle', record: 'Voice', state: 'completed', outcome: 'completed',
            checks: [
                {name: 'fit', status: 'PASS', detail: 'combined occupancy 100 bytes; card capacity 200 bytes'},
                {name: 'real-time', status: 'FAIL', detail: 'audio duration 900 ns; synthesis wall time 1000 ns'},
                {name: 'recognition', status: 'PASS', detail: 'text non-empty; matching words 0; recognition time 400 ns; clip duration 900 ns'}]},
        {id: 'error-1', kind: 'bundle', procedure: 'bundle', record: 'Voice', state: 'completed', outcome: 'completed',
            checks: [
                {name: 'real-time', status: 'ERROR', detail: 'audio duration unmeasured ns; synthesis wall time 500 ns'},
                {name: 'recognition', status: 'ERROR',
                    detail: 'text unavailable; matching words unavailable; recognition time 400 ns; clip duration unmeasured ns'}]},
        {id: 'empty-1', kind: 'bundle', procedure: 'bundle', record: 'Voice', state: 'completed', outcome: 'completed',
            checks: [{name: 'recognition', status: 'FAIL',
                detail: 'text empty; matching words 0; recognition time 1000 ns; clip duration 900 ns'}]},
        {id: 'pass-1', kind: 'bundle', procedure: 'bundle', record: 'Voice', state: 'completed', outcome: 'completed',
            checks: [{name: 'real-time', status: 'PASS',
                detail: 'audio duration 1250 ns; synthesis wall time 1000 ns'}]},
        {id: 'invalid-1', kind: 'bundle', procedure: 'bundle', record: 'Voice', state: 'completed', outcome: 'completed',
            checks: [{name: 'real-time', status: 'ERROR',
                detail: 'audio duration 0 ns; synthesis wall time 0 ns'}]},
        {id: 'previous-1', kind: 'bundle', procedure: 'bundle', record: 'Voice', state: 'completed', outcome: 'completed',
            checks: [{name: 'recognition', status: 'PASS',
                detail: 'returned 3 matching words in 400 ns; clip duration 900 ns'}]},
        {id: 'previous-unmeasured-1', kind: 'bundle', procedure: 'bundle', record: 'Voice', state: 'completed', outcome: 'completed',
            checks: [{name: 'recognition', status: 'ERROR',
                detail: 'returned 0 matching words in unmeasured ns; clip duration unmeasured ns'}]},
        {id: 'language-1', kind: 'smoke', procedure: 'smoke', record: 'Language', state: 'running', outcome: '', checks: []}],
        procedures: [{id: 'bundle', label: 'Bundle'}]};
    let serving = 'stopped'; const calls = []; const popups = []; let poll;
    let nextResponse = null;
    const fetch = async (url, options = {}) => {
        calls.push({url, options});
        if (nextResponse) { const response = nextResponse; nextResponse = null; return response; }
        if (url === '/api/status') return {ok: true, headers: {get: () => 'application/json'},
            json: async () => ({items: [{name: 'serving', state: serving}]})};
        return {ok: true, headers: {get: () => 'application/json'}, json: async () => data};
    };
    const window = {matutoCsrf: '', location: {hash: '#/runs/bundle-runs?bundle=bundle-1'}, listeners: {},
        addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); },
        matutoReason(control, message) { control.setAttribute('aria-disabled', message ? 'true' : 'false'); },
        matutoPopup: {preview(value) { popups.push(value); }},
        setInterval(fn) { poll = fn; }, setTimeout(fn) { setImmediate(fn); }};
    vm.runInNewContext(source, {document, window, fetch, URLSearchParams, setInterval: window.setInterval,
        setTimeout: window.setTimeout, Promise, Object, String, Error});
    await tick();

    const check = (condition, message) => { if (!condition) throw new Error(message); };
    check(error.hidden, 'bundle run page data must load');
    check(start.elements.bundle.children.length === 2 && start.elements.bundle.value === 'bundle-1'
        && start.elements.bundle.children.every(option => option.getAttribute('value') !== 'lang-1'),
        'the address must preselect the bundle while excluding language profiles');
    start.elements.bundle.selectedIndex = 1;
    await poll();
    check(start.elements.bundle.value === 'bundle-2',
        'refreshing the form must preserve a bundle choice changed after address preselection');
    start.elements.bundle.selectedIndex = 0;
    check(current.textContent.includes('Phase: loading bundle parts. Current step: starting recognition.'),
        'an active bundle run must show its runner phase and step');
    check(history.textContent.includes('Outcome: completed.') && history.textContent.includes('FAIL: real-time'),
        'a completed run must retain its outcome separately from a failed check');
    check(history.textContent.includes('combined occupancy 100 bytes; card capacity 200 bytes')
        && history.textContent.includes('real-time factor 0.9; pass requires > 1.0 (audio duration 900 ns; synthesis wall time 1000 ns)')
        && history.textContent.includes('real-time factor 1.25; pass requires > 1.0'),
        'fit and real-time checks must retain their measurements and show the factor against its threshold');
    check(history.textContent.includes('non-empty text returned; recognition time 400 ns; clip duration 900 ns')
        && history.textContent.includes('Observation — 0 recognised words.')
        && !history.textContent.includes('PASS: recognition — 0 matching words'),
        'non-empty recognition text must not be inferred from overlap, and word count stays an observation');
    check(history.textContent.includes('FAIL: recognition — empty text returned')
        && history.textContent.includes('Observation — 0 recognised words.'),
        'an empty transcript and its word-count observation must remain distinct');
    check(history.textContent.includes('PASS: recognition — text result unavailable; recognition time 400 ns; clip duration 900 ns')
        && history.textContent.includes('Observation — 3 recognised words.')
        && !history.textContent.includes('PASS: recognition — returned 3 matching words'),
        'stored pre-update checks must retain their measured count as an observation without guessing text presence');
    const previousUnmeasured = history.children.find(entry => entry.getAttribute('id') === 'bundle-run-previous-unmeasured-1');
    check(previousUnmeasured && previousUnmeasured.textContent.includes('ERROR: recognition — text result unavailable; recognition time unavailable; clip duration unavailable')
        && !previousUnmeasured.textContent.includes('Observation — 0 recognised words.'),
        'historical recognition defaults must not turn an unmeasured word count into zero');
    check(history.textContent.includes('ERROR: real-time — real-time factor unavailable; pass requires > 1.0')
        && history.textContent.includes('real-time factor unavailable; pass requires > 1.0 (audio duration 0 ns; synthesis wall time 0 ns)')
        && history.textContent.includes('ERROR: recognition — text result unavailable; recognition time 400 ns; clip duration unavailable')
        && !history.textContent.includes('Observation — unavailable recognised words'),
        'unavailable and invalid zero measurements stay unavailable while partial data remains visible');
    check(!history.textContent.includes('qualification') && !history.textContent.includes('qualified'),
        'run completion must never be presented as qualification');

    serving = 'voice'; await poll();
    check(submit.textContent === 'Stop serving and run…', 'serving must use the explicit preview label');
    await start.fire('submit', {preventDefault() {}});
    const startCall = calls.find(call => call.url === '/api/runs' && call.options.method === 'POST');
    check(startCall && JSON.stringify(JSON.parse(startCall.options.body)) === JSON.stringify({
        action: 'start', procedure: 'bundle', record: 'bundle-1'}),
        'Start must send only the selected bundle and bundle procedure through the shared start route');
    nextResponse = {ok: false, headers: {get: () => 'application/json'},
        json: async () => ({error: 'serving is active', offer: true})};
    await start.fire('submit', {preventDefault() {}});
    check(popups.length === 1 && popups[0].safe === 'Keep serving'
        && popups[0].starts[0] === 'bundle for Voice',
        'the runner offer must ask for explicit confirmation with the safe choice first');
    await popups[0].confirmAction();
    const confirm = calls.find(call => call.url === '/api/runs/stop-serving');
    check(confirm && JSON.stringify(JSON.parse(confirm.options.body)) === JSON.stringify({
        confirm: true, procedure: 'bundle', record: 'bundle-1'}),
        'confirmation must pass the same bundle request through the shared confirmed path');
    process.stdout.write('PASS: bundle-run page state and actions\n');
}

main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
