#!/usr/bin/env node
// Exercise the Runs & jobs page script at its browser-state seam with element doubles.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
    constructor(tag, id = '') {
        this.tagName = tag.toUpperCase(); this.id = id; this.children = []; this.attributes = {};
        this.listeners = {}; this.text = ''; this.value = ''; this.disabled = false;
        this.elements = {}; this.selectedOptions = []; this.hidden = false; this.checked = false; this.parentNode = null;
        this._selectedIndex = -1;
    }
    set textContent(value) {
        this.text = String(value);
        for (const child of this.children) child.parentNode = null;
        this.children = [];
        this.selectedOptions = []; this._selectedIndex = -1;
    }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    set value(value) { this._value = String(value); }
    get value() { return this._value; }
    set selectedIndex(index) {
        this._selectedIndex = index;
        const option = this.children[index];
        this.selectedOptions = option ? [option] : [];
        if (option) this.value = option.getAttribute('value');
    }
    get selectedIndex() { return this._selectedIndex; }
    setAttribute(name, value) { this.attributes[name] = String(value); if (name === 'id') this.id = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    focus() { this.ownerDocument.activeElement = this; }
    scrollIntoView() { this.scrolled = true; }
    append(...children) {
        for (const child of children) {
            if (child.parentNode) child.parentNode.children.splice(child.parentNode.children.indexOf(child), 1);
            child.parentNode = this;
            this.children.push(child);
        }
    }
    remove() {
        if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
        this.parentNode = null;
    }
    contains(element) { return this === element || this.children.some(child => child.contains(element)); }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    querySelector(selector) {
        if (selector === 'button[type="submit"]') return this.submitButton;
        return null;
    }
    querySelectorAll(selector) {
        const found = [];
        const visit = element => {
            if (selector === '[data-exception]' && element.getAttribute('data-exception') !== null) found.push(element);
            for (const child of element.children) visit(child);
        };
        visit(this);
        return found;
    }
    fire(name, event = {}) { return Promise.all((this.listeners[name] || []).map(fn => fn(event))); }
}

const tick = () => new Promise(resolve => setImmediate(resolve));
const source = fs.readFileSync(path.resolve(__dirname, '../../../agents/matuto/static/runs.js'), 'utf8');
async function main() {
    const current = new Element('div', 'runs-current');
    const history = new Element('div', 'runs-history');
    const start = new Element('form', 'runs-start');
    start.elements.procedure = new Element('select');
    start.elements.record = new Element('select');
    const submit = new Element('button'); start.submitButton = submit;
    const error = new Element('p', 'runs-error'); error.hidden = true;
    const startReason = new Element('p');
    const acceptanceRecord = new Element('select');
    const acceptanceReview = new Element('button');
    const acceptanceContent = new Element('div');
    const wqNames = ['first_byte_seconds', 'short_total_seconds', 'generation_tokens_per_second',
        'vram_mb', 'rss_kib', 'cancel_recovery_seconds', 'restart_seconds', 'long_execution_seconds',
        'retrieval_samples', 'retrieval_generation_tokens'];
    const elements = {'runs-current': current, 'runs-history': history,
        'runs-start': start, 'runs-error': error, 'runs-start-reason': startReason,
        'acceptance-record': acceptanceRecord, 'acceptance-review': acceptanceReview,
        'acceptance-content': acceptanceContent, 'runs-wq-limits': new Element('fieldset')};
    for (const name of wqNames) {
        elements[`wq-${name}`] = new Element('input');
        elements[`wq-error-${name}`] = new Element('span');
    }
    const document = {
        activeElement: null,
        getElementById(id) { return elements[id]; },
        createElement(tag) { const node = new Element(tag); node.ownerDocument = this; return node; },
    };
    const data = {csrf: 'rotated-token', entries: [
        {id: 'smoke-1', kind: 'smoke', purpose: 'smoke for Studio', record: 'Studio', procedure: 'smoke',
            state: 'running', outcome: '', started_at: '2026-10-13T12:00:00Z',
            progress: {phase: 'Loading', step: 'layer 2', elapsed: 7, log: 'server ready', unknown: ''}, checks: []},
        {id: 'smoke-0', kind: 'smoke', purpose: 'smoke for Studio', record: 'Studio', procedure: 'smoke',
            state: 'completed', outcome: 'completed', started_at: '2026-10-13T11:00:00Z',
            progress: null, checks: [{status: 'PASS', name: 'exact reply', detail: ''}]},
        {id: 'context-search-winner', kind: 'context_search', purpose: 'context_search for Studio', record: 'Studio',
            procedure: 'context_search', state: 'completed', outcome: 'completed', started_at: 'today', progress: null,
            checks: [], context_search: {role: 'search', result: '192K', winner: false}},
        {id: 'context-search-winning-rung', kind: 'wq', purpose: 'wq for Studio', record: 'Studio',
            procedure: 'wq', state: 'completed', outcome: 'completed', started_at: 'today', progress: null,
            checks: [], context_search: {role: 'rung', rung: '192K', result: '192K', winner: true}},
        {id: 'context-search-no-winner', kind: 'context_search', purpose: 'context_search for Studio', record: 'Studio',
            procedure: 'context_search', state: 'completed', outcome: 'completed', started_at: 'today', progress: null,
            checks: [], context_search: {role: 'search', result: 'no winner', winner: false}},
        {id: 'context-search-failed-rung', kind: 'wq', purpose: 'wq for Studio', record: 'Studio',
            procedure: 'wq', state: 'completed', outcome: 'completed', started_at: 'today', progress: null,
            checks: [], context_search: {role: 'rung', rung: '96K', result: 'no winner', winner: false}},
    ], procedures: [{id: 'smoke', label: 'Smoke'}, {id: 'wq', label: 'Wq'}], records: [
        {id: 'profile-1', name: 'Voice bundle', kind: 'bundle'},
        {id: 'profile-1', name: 'Studio', kind: 'language', wq_limits: {
            first_byte_seconds: 35, short_total_seconds: 90, generation_tokens_per_second: 20,
            vram_mb: 32000, rss_kib: 33554432, cancel_recovery_seconds: 5, restart_seconds: 300,
            long_execution_seconds: 1800, retrieval_samples: 3, retrieval_generation_tokens: 8192}},
        {id: 'profile-2', name: 'Workshop', kind: 'language', wq_limits: {
            first_byte_seconds: 35, short_total_seconds: 90, generation_tokens_per_second: 20,
            vram_mb: 16000, rss_kib: 33554432, cancel_recovery_seconds: 5, restart_seconds: 300,
            long_execution_seconds: 1800, retrieval_samples: 3, retrieval_generation_tokens: 8192}}]};
    let servingState = 'stopped';
    const calls = [];
    let downloadData = [];
    let discardChanges = 0, resumeRefusals = 0;
    let nextResponse = null;
    let accepted = false;
    let recordKind = 'language';
    const acceptanceData = () => ({record: {id: 'profile-1',
        kind: recordKind, name: recordKind === 'bundle' ? 'Voice bundle' : 'Studio',
        state: {state: accepted ? 'qualified' : 'candidate', fingerprint: 'fp-current'}},
        runs: [
            {run_id: 'smoke-1', procedure: 'smoke', outcome: 'completed', fingerprint: 'fp-current',
                started_at: 'today', citable: true, checks: [{name: 'reply', status: 'PASS', detail: ''}]},
            {run_id: 'bench-1', procedure: 'bench', outcome: 'completed', fingerprint: 'fp-current',
                started_at: 'today', citable: true, checks: [{name: 'measurement', status: 'FAIL', detail: 'slow'}]},
            {run_id: 'old-1', procedure: 'smoke', outcome: 'interrupted', fingerprint: 'fp-current',
                citable: false, reason: 'Outcome is interrupted, not completed.', checks: []}],
        acceptances: accepted ? [{acceptance_id: 'accept-1', scope: 'scope', residual_risks: 'risk',
            accepted_by: 'operator', accepted_at: 'today', fingerprint: 'fp-current', withdrawn_at: null,
            withdrawable: true, runs: [{run_id: 'smoke-1', procedure: 'smoke', record_label: 'Studio',
                run_fingerprint: 'fp-current', build_id: 'build-1'}],
            exceptions: [{check_name: 'measurement', reason: 'thermal limit'}]}] : []});
    const fetch = async (url, options = {}) => {
        calls.push({url, options});
        if (nextResponse) { const response = nextResponse; nextResponse = null; return response; }
        if (url === '/api/status') return {ok: true, headers: {get: () => 'application/json'},
            json: async () => ({items: [{name: 'serving', state: servingState}]})};
        if (url === '/api/acquisition') {
            if (options.method === 'POST') {
                const body = JSON.parse(options.body);
                if (body.action === 'discard-preview') return {ok: true, headers: {get: () => 'application/json'}, json: async () => downloadData[0].partials};
                if (body.action === 'resume' && resumeRefusals-- > 0) return {ok: false, headers: {get: () => 'application/json'}, json: async () => ({error: 'slot held by another job'})};
                if (body.action === 'discard' && discardChanges-- > 0) return {ok: false, headers: {get: () => 'application/json'},
                    json: async () => ({error: 'staging changed; review the fresh discard preview', preview: {...downloadData[0].partials, files: [{name: 'new.bin', size: 3}], total_size: 3}})};
                return {ok: true, headers: {get: () => 'application/json'}, json: async () => ({job_id: body.job_id})};
            }
            return {ok: true, headers: {get: () => 'application/json'}, json: async () => ({downloads: downloadData})};
        }
        if (url.startsWith('/api/acceptance')) {
            if (options.method === 'POST' && JSON.parse(options.body).action === 'accept') accepted = true;
            if (options.method === 'POST' && JSON.parse(options.body).action === 'withdraw') accepted = false;
            return {ok: true, headers: {get: () => 'application/json'}, json: async () =>
                options.method === 'POST' ? {acceptance: {}} : acceptanceData()};
        }
        return {ok: true, headers: {get: () => 'application/json'}, json: async () => data};
    };
    const popups = [];
    let poll;
    const window = {matutoCsrf: 'old-token', location: {hash: ''}, listeners: {},
        addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); },
        fire(name) { for (const fn of this.listeners[name] || []) fn(); },
        matutoReason: (control, reason) => {
            if (reason) {
                control.setAttribute('aria-disabled', 'true');
                control.setAttribute('aria-describedby', 'start-reason');
            } else {
                control.attributes['aria-disabled'] = undefined;
                control.attributes['aria-describedby'] = undefined;
            }
        },
        matutoPopup: {open: popup => popups.push(popup), preview: popup => popups.push(popup)},
        setInterval(fn) { poll = fn; }, setTimeout(fn) { setImmediate(fn); }};
    vm.runInNewContext(source, {document, window, fetch, setInterval: window.setInterval,
        Date, Object, String, encodeURIComponent, Error, Promise});
    await tick();

    const check = (condition, message) => { if (!condition) throw new Error(message); };
    check(error.hidden, 'available run and record data must render without an error');
    check(elements['runs-wq-limits'].hidden && elements['runs-wq-limits'].disabled,
        'WQ-only required fields must not block other procedure starts');
    check(current.children.length === 1 && current.textContent.includes('Phase: Loading. Current step: layer 2.'),
        'running work must show progress from the shared runner');
    check(current.textContent.includes('Stop…'), 'running work must offer Stop');
    check(history.children.length === 5 && history.textContent.includes('PASS: exact reply'),
        'ended runs must keep checks distinct from the outcome');
    check(history.textContent.includes('completed') && !history.textContent.includes('qualified'),
        'completion must not be presented as qualification');
    const winnerSearch = history.children.find(row => row.id === 'work-context-search-winner');
    const winnerRung = history.children.find(row => row.id === 'work-context-search-winning-rung');
    const noWinnerSearch = history.children.find(row => row.id === 'work-context-search-no-winner');
    const failedRung = history.children.find(row => row.id === 'work-context-search-failed-rung');
    check(winnerSearch.children[0].textContent === 'Context search: Studio'
        && winnerSearch.textContent.includes('Search result: 192K.'),
        'the shared job row must identify the recorded winning rung');
    check(winnerRung.children[0].textContent === 'Context-search rung 192K: Studio'
        && winnerRung.textContent.includes('Search result: 192K. Winning rung.'),
        'the matching rung row must identify the same result as its parent search');
    check(noWinnerSearch.textContent.includes('Search result: no winner.')
        && failedRung.textContent.includes('Search result: no winner.')
        && !noWinnerSearch.textContent.includes('Winner.') && !failedRung.textContent.includes('Winner.'),
        'the parent and rung rows must report no winner without labeling any row as the winner');
    const evidence = history.children[0].children.find(child => child.tagName === 'A');
    check(evidence && evidence.textContent === 'Download evidence for run smoke-0'
        && evidence.getAttribute('href').includes('smoke-0'), 'each ended run must identify its evidence download');

    const stop = current.children[0].children.find(child => child.textContent === 'Stop…');
    const runningRow = current.children[0];
    document.activeElement = stop;
    await poll();
    check(current.children[0] === runningRow && runningRow.children.includes(stop) && document.activeElement === stop,
        'an unchanged poll must preserve the live focused action and its popup opener');
    check(runningRow.getAttribute('id') === 'work-smoke-1',
        'each shared work item must expose a stable destination for in-page links');
    window.location.hash = '#/runs?work=smoke-1';
    window.fire('hashchange');
    check(document.activeElement === runningRow && runningRow.scrolled,
        'a Runs & jobs link with a work ID must focus and scroll its matching entry');
    document.activeElement = stop;
    await poll();
    check(document.activeElement === stop,
        'polling must not steal focus after arriving at a linked work entry');
    await stop.fire('click');
    check(popups.length === 1 && popups[0].title === 'Stop this work?'
        && popups[0].stops[0].includes('smoke-1'), 'Stop must use the shared consequence popup');
    await popups[0].confirmAction();
    const stopCall = calls.find(call => call.options.body && JSON.parse(call.options.body).action === 'stop');
    check(stopCall && stopCall.options.headers['X-CSRF-Token'] === 'rotated-token',
        'mutations after reconnect must use the current session-bound token');

    check(window.matutoCsrf === 'rotated-token', 'the authenticated poll must refresh the mutation token');
    check(start.elements.procedure.children.length === 2
        && start.elements.procedure.children.some(option => option.getAttribute('value') === 'smoke')
        && start.elements.procedure.children.some(option => option.getAttribute('value') === 'wq')
        && start.elements.record.children.length === 2
        && start.elements.record.children[0].getAttribute('value') === 'profile-1'
        && start.elements.record.children[0].getAttribute('data-kind') === 'language',
        'Start must offer language procedures and language profiles only');
    window.location.hash = '#/runs?profile=profile-2'; window.fire('hashchange');
    check(start.elements.record.value === 'profile-2'
        && !calls.some(call => call.url === '/api/runs' && call.options.method === 'POST'
            && JSON.parse(call.options.body).action === 'start'),
        'a language-profile address must preselect that record without starting work');
    window.location.hash = '#/runs?record=profile-1'; window.fire('hashchange');
    start.elements.procedure.value = 'wq'; await start.elements.procedure.fire('change');
    check(elements['runs-wq-limits'].hidden === false && !elements['runs-wq-limits'].disabled
        && elements['wq-vram_mb'].value === '32000'
        && elements['wq-retrieval_samples'].value === '3',
        'WQ shows its defaults with the selected profile card ceiling');
    start.elements.record.value = 'profile-2'; await start.elements.record.fire('change');
    check(elements['wq-vram_mb'].value === '16000',
        'changing profiles refills the WQ VRAM default from the newly picked card');
    start.elements.record.value = 'profile-1'; await start.elements.record.fire('change');
    check(elements['wq-vram_mb'].value === '32000',
        'changing back refills the WQ defaults from that profile card');
    window.location.hash = '#/runs?record=profile-2'; window.fire('hashchange');
    check(start.elements.record.value === 'profile-2' && elements['wq-vram_mb'].value === '16000',
        'an addressed profile change immediately refills its WQ card limit');
    window.location.hash = '#/runs?record=profile-1'; window.fire('hashchange');
    check(elements['wq-vram_mb'].value === '32000',
        'returning to the addressed profile immediately restores its default');
    servingState = 'agentic'; await poll();
    check(submit.textContent === 'Stop serving and run…',
        'a card procedure must name the serving interruption before opening its preview');
    start.elements.procedure.value = 'verify'; await start.elements.procedure.fire('change');
    check(submit.textContent === 'Start', 'Verify must retain Start while serving is up');
    start.elements.procedure.value = 'smoke'; await start.elements.procedure.fire('change');
    servingState = 'voice'; await poll();
    check(submit.textContent === 'Stop serving and run…',
        'voice serving must use the same card-run control label');
    start.elements.record.selectedOptions = [{textContent: 'Studio (language)'}];
    nextResponse = {ok: false, headers: {get: () => 'application/json'},
        json: async () => ({error: 'The admission slot is held by serve-preview.', offer: true})};
    await start.fire('submit', {preventDefault() {}});
    check(calls.some(call => call.options.body && JSON.parse(call.options.body).action === 'start'),
        'Start must send the selected procedure and record to the API');
    check(popups.length === 2 && popups[1].title === 'Stop serving and run?'
        && popups[1].safe === 'Keep serving' && popups[1].stops.length && popups[1].starts.length && popups[1].stays.length
        && popups[1].starts[0] === 'smoke for Studio (language)' && typeof popups[1].confirmAction === 'function',
        'the runner offer must name the selected record without executing out-of-scope serving transitions');
    check(startReason.textContent.includes('serve-preview'), 'the offer must name the current work beside Start');
    start.elements.procedure.value = 'verify';
    start.elements.record.value = 'different-profile';
    await popups[1].confirmAction();
    const confirmedRun = calls.find(call => call.url === '/api/runs/stop-serving');
    check(confirmedRun && JSON.parse(confirmedRun.options.body).confirm === true
        && JSON.parse(confirmedRun.options.body).procedure === 'smoke'
        && JSON.parse(confirmedRun.options.body).record === 'profile-1',
        'confirmation must submit the same selected procedure and record to the stop-serving route');
    await poll();
    check(startReason.textContent.includes('serve-preview'), 'polling must not erase a start refusal');
    check(!calls.some(call => call.url === '/api/serving/stop'), 'the page must not stop serving outside the procedure workflow');

    nextResponse = {ok: false, headers: {get: () => 'application/json'},
        json: async () => ({error: 'The admission slot is held by smoke-1.', offer: false})};
    await start.fire('submit', {preventDefault() {}});
    await poll();
    check(startReason.textContent.includes('smoke-1'), 'ordinary admission refusals must survive the next poll');

    start.elements.procedure.value = 'wq';
    start.elements.record.value = 'profile-1';
    start.elements.record.selectedOptions = [{textContent: 'Studio (language)'}];
    await start.elements.procedure.fire('change');
    await start.elements.record.fire('change');
    elements['wq-retrieval_samples'].value = '1.5';
    nextResponse = {ok: false, headers: {get: () => 'application/json'},
        json: async () => ({error: 'retrieval-samples must be a whole number', field: 'retrieval_samples'})};
    const startsBeforeInvalidWq = calls.filter(call => call.url === '/api/runs' && call.options.method === 'POST'
        && JSON.parse(call.options.body).action === 'start').length;
    await start.fire('submit', {preventDefault() {}});
    const invalidWqStart = calls.filter(call => call.url === '/api/runs' && call.options.method === 'POST'
        && JSON.parse(call.options.body).action === 'start').at(-1);
    check(elements['wq-error-retrieval_samples'].textContent.includes('whole number')
        && elements['wq-retrieval_samples'].value === '1.5'
        && JSON.parse(invalidWqStart.options.body).limits.retrieval_samples === 1.5
        && calls.filter(call => call.url === '/api/runs' && call.options.method === 'POST'
            && JSON.parse(call.options.body).action === 'start').length === startsBeforeInvalidWq + 1,
        'the procedure-owned validator returns a field error and keeps the refused value');
    elements['wq-first_byte_seconds'].value = '1e999';
    elements['wq-first_byte_seconds'].validationMessage = 'Enter a number.';
    await elements['wq-first_byte_seconds'].fire('invalid');
    check(elements['wq-error-first_byte_seconds'].textContent === 'Enter a number.'
        && elements['wq-first_byte_seconds'].getAttribute('aria-invalid') === 'true'
        && elements['wq-first_byte_seconds'].value === '1e999',
        'native-invalid number input displays its browser message beside the retained field');
    elements['wq-first_byte_seconds'].value = '35';
    elements['wq-retrieval_samples'].value = '4';
    elements['wq-vram_mb'].value = '12000';
    nextResponse = {ok: false, headers: {get: () => 'application/json'},
        json: async () => ({error: 'Stop serving and run: consumers lose the model.', offer: true})};
    const stopServingCallsBeforeWq = calls.filter(call => call.url === '/api/runs/stop-serving').length;
    await start.fire('submit', {preventDefault() {}});
    const wqStart = calls.filter(call => call.url === '/api/runs' && call.options.method === 'POST'
        && JSON.parse(call.options.body).action === 'start').at(-1);
    check(JSON.parse(wqStart.options.body).procedure === 'wq'
        && JSON.parse(wqStart.options.body).limits.vram_mb === 12000
        && JSON.parse(wqStart.options.body).limits.retrieval_samples === 4,
        'WQ Start sends the selected profile, procedure, and edited limits');
    check(popups.at(-1).title === 'Stop serving and run?'
        && typeof popups.at(-1).confirmAction === 'function'
        && calls.filter(call => call.url === '/api/runs/stop-serving').length === stopServingCallsBeforeWq,
        'WQ serving confirmation is opened without starting work');
    await popups.at(-1).confirmAction();
    const wqConfirmed = calls.filter(call => call.url === '/api/runs/stop-serving').at(-1);
    const confirmedWqBody = JSON.parse(wqConfirmed.options.body);
    check(confirmedWqBody.procedure === 'wq' && confirmedWqBody.record === 'profile-1'
        && confirmedWqBody.limits.vram_mb === 12000 && confirmedWqBody.limits.retrieval_samples === 4,
        'the serving confirmation carries the exact entered WQ limits');

    acceptanceRecord.value = 'profile-1';
    acceptanceRecord.selectedOptions = [acceptanceRecord.children.find(item => item.getAttribute('data-kind') === 'language')];
    await acceptanceReview.fire('click');
    check(acceptanceContent.textContent.includes('Cannot cite: Outcome is interrupted'),
        'uncitable runs must remain listed with their citation reason');
    check(acceptanceContent.textContent.includes('Still needs: a passed smoke and a completed bench'),
        'the form must show its non-overridable minimum evidence');
    check(acceptanceContent.textContent.includes('Automated results are read-only')
        && acceptanceContent.textContent.includes('date and login username are filled in automatically'),
        'acceptance must keep automated evidence read-only and identify automatic attribution');
    const all = root => [root, ...root.children.flatMap(all)];
    check(!all(acceptanceContent).some(item => item.tagName === 'INPUT' && item.getAttribute('value') === 'old-1'),
        'an uncitable run must not be selectable');
    const checkbox = all(acceptanceContent).find(item => item.tagName === 'INPUT' && item.getAttribute('value') === 'smoke-1');
    const bench = all(acceptanceContent).find(item => item.tagName === 'INPUT' && item.getAttribute('value') === 'bench-1');
    const controls = all(acceptanceContent).filter(item => item.tagName === 'INPUT' || item.tagName === 'BUTTON');
    check(!controls.some(item => /override/i.test(item.textContent + item.getAttribute('name'))),
        'minimum evidence must not expose an override control');
    check(!controls.some(item => ['username', 'accepted_at', 'outcome', 'status'].includes(item.getAttribute('name'))),
        'acceptance must not edit identity, date, outcomes, or checks');
    checkbox.checked = true; bench.checked = true;
    await checkbox.fire('change'); await bench.fire('change');
    const scope = all(acceptanceContent).find(item => item.tagName === 'TEXTAREA' && item.getAttribute('name') === 'scope');
    const risks = all(acceptanceContent).find(item => item.tagName === 'TEXTAREA' && item.getAttribute('name') === 'residual-risks');
    let exception = all(acceptanceContent).find(item => item.getAttribute('data-exception') === 'measurement');
    check(exception && exception.getAttribute('required') === 'true', 'each selected failed check needs a reason field');
    scope.value = 'one consumer'; risks.value = 'slow bench'; exception.value = 'thermal limit';
    await exception.fire('input');
    bench.checked = false; await bench.fire('change');
    bench.checked = true; await bench.fire('change');
    exception = all(acceptanceContent).find(item => item.getAttribute('data-exception') === 'measurement');
    check(exception.value === 'thermal limit', 'changing citations must preserve an entered exception reason');
    nextResponse = {ok: false, headers: {get: () => 'application/json'}, json: async () => ({error: 'fingerprint changed'})};
    const acceptForm = all(acceptanceContent).find(item => item.tagName === 'FORM');
    await acceptForm.fire('submit', {preventDefault() {}});
    check(scope.value === 'one consumer' && risks.value === 'slow bench' && exception.value === 'thermal limit'
        && acceptanceContent.textContent.includes('fingerprint changed'), 'a refusal must keep every form value');
    await acceptForm.fire('submit', {preventDefault() {}});
    check(acceptanceContent.textContent.includes('Accepted by operator')
        && acceptanceContent.textContent.includes('thermal limit'), 'acceptance history must show submitted evidence and exception');
    check(acceptanceContent.textContent.includes('Cannot cite: Outcome is interrupted'),
        'qualified records must keep their read-only run evidence and citation reasons visible');
    const withdraw = all(acceptanceContent).find(item => item.tagName === 'BUTTON' && item.textContent === 'Withdraw acceptance…');
    await withdraw.fire('click');
    check(popups.at(-1).title === 'Withdraw acceptance?'
        && popups.at(-1).safe === 'Keep acceptance'
        && popups.at(-1).content.textContent.includes('nothing stops'), 'withdrawal must explain its non-destructive consequence and open on the safe choice');
    await popups.at(-1).confirmAction();
    check(calls.some(call => call.url === '/api/acceptance' && JSON.parse(call.options.body).action === 'withdraw'),
        'withdrawal must call the acceptance API');

    recordKind = 'bundle'; accepted = true; acceptanceRecord.value = 'profile-1';
    acceptanceRecord.selectedOptions = [acceptanceRecord.children.find(item => item.getAttribute('data-kind') === 'bundle')];
    data.records.reverse();
    await poll();
    check(acceptanceRecord.selectedOptions[0].getAttribute('data-kind') === 'bundle',
        'refreshing a picker with duplicate IDs must preserve the selected kind');
    await acceptanceReview.fire('click');
    check(calls.some(call => call.url === '/api/acceptance?kind=bundle&id=profile-1'),
        'selecting a bundle with an ID shared by a language profile must preserve its kind');
    const bundleWithdraw = all(acceptanceContent).find(item => item.tagName === 'BUTTON'
        && item.textContent === 'Withdraw acceptance…');
    await bundleWithdraw.fire('click');
    check(popups.at(-1).content.textContent.includes('keeps serving until stopped')
        && popups.at(-1).content.textContent.includes('next Serve voice request will refuse'),
        'bundle withdrawal must explain its serving and next-request consequences');

    await start.elements.procedure.fire('change');
    check(startReason.textContent === '', 'changing a start choice clears its stale refusal');

    const download = {id: 'download-old', kind: 'download', purpose: 'pinned-json', state: 'running',
        outcome: '', started_at: 'today', progress: {phase: 'download', step: '', elapsed: 7, log: '', unknown: ''}, checks: []};
    data.entries.push(download);
    downloadData = [{id: download.id, purpose: 'owner/model@abc', state: 'running', outcome: '', phase: 'download',
        file: 'model.gguf', transferred: 4, total: 8, progress: {elapsed: 7, unknown: ''}}];
    await poll();
    let downloadRow = current.children.find(row => row.getAttribute('id') === 'work-download-old');
    check(downloadRow && downloadRow.textContent.includes('Phase: download') &&
        downloadRow.textContent.includes('Current file: model.gguf') && downloadRow.textContent.includes('4 of 8 bytes') &&
        downloadRow.textContent.includes('Elapsed: 7 seconds') && downloadRow.textContent.includes('download: owner/model@abc'),
        'Runs & jobs shows readable, structured download progress rather than the pinned-request blob');
    const cancelDownload = downloadRow.children.find(child => child.textContent === 'Cancel download…');
    await cancelDownload.fire('click');
    check(popups.at(-1).title === 'Cancel download?' && popups.at(-1).stays[0].includes('partial files'),
        'Runs & jobs Cancel previews keeping partials for Resume');
    await popups.at(-1).confirmAction();
    download.state = 'launching'; download.progress = {phase: '', step: '', elapsed: 8, log: '', unknown: ''};
    downloadData = [{id: download.id, purpose: 'owner/model@abc', state: 'launching', outcome: '', progress: {elapsed: 8}}];
    await poll();
    downloadRow = current.children.find(row => row.getAttribute('id') === 'work-download-old');
    check(downloadRow.children.find(child => child.textContent === 'Cancel download…').hidden,
        'Cancel is offered only for a running download');
    download.state = 'interrupted'; download.outcome = 'interrupted';
    download.progress = {phase: '', step: '', elapsed: 12, log: '', unknown: 'progress is unknown'};
    downloadData = [{id: download.id, purpose: 'owner/model@abc', state: 'interrupted', outcome: 'interrupted',
        reason: 'Connection interrupted', progress: {elapsed: 12, unknown: 'progress is unknown'}, partials: {
            job_id: download.id, files: [{name: 'model.gguf', size: 4}], total_size: 4,
            consequence: 'The next attempt starts again from nothing.'}}];
    await poll();
    downloadRow = history.children.find(row => row.getAttribute('id') === 'work-download-old');
    check(downloadRow && downloadRow.textContent.includes('progress is unknown')
        && downloadRow.textContent.includes('Resume') && downloadRow.textContent.includes('Discard partial download…'),
        'Runs & jobs shows unknown progress and only offers recovery for retained partials');
    resumeRefusals = 1;
    await downloadRow.children.find(child => child.textContent === 'Resume').fire('click');
    check(downloadRow.textContent.includes('Resume refused: slot held'),
        'Runs & jobs shows a Resume refusal beside its control');
    await downloadRow.children.find(child => child.textContent === 'Resume').fire('click');
    check(calls.some(call => call.url === '/api/acquisition' && call.options.body && JSON.parse(call.options.body).action === 'resume'),
        'Runs & jobs Resume calls the pinned acquisition route');
    await downloadRow.children.find(child => child.textContent === 'Discard partial download…').fire('click');
    check(popups.at(-1).content.textContent.includes('model.gguf') && popups.at(-1).content.textContent.includes('4 bytes'),
        'Runs & jobs discard opens the same safe-file preview');
    discardChanges = 1;
    let discardError;
    try { await popups.at(-1).confirmAction(); } catch (cause) { discardError = cause; }
    check(discardError && popups.at(-1).content.textContent.includes('new.bin'),
        'a stale Runs & jobs confirmation retains the popup and refreshes its preview');
    await popups.at(-1).confirmAction();
    check(calls.some(call => call.url === '/api/acquisition' && call.options.body && JSON.parse(call.options.body).action === 'discard'),
        'Runs & jobs confirms discard through the acquisition route');
    process.stdout.write('PASS: Runs & jobs page state and actions\n');
}

main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
