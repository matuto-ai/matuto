#!/usr/bin/env node
// Exercise context-search form and result behavior with DOM element doubles.
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
        for (const child of children) { child.parentNode = this; this.children.push(child); }
    }
    remove() {
        if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
        this.parentNode = null;
    }
    replaceWith(element) {
        const index = this.parentNode.children.indexOf(this);
        this.parentNode.children[index] = element;
        element.parentNode = this.parentNode;
        this.parentNode = null;
    }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    querySelector(selector) { return selector === 'button[type="submit"]' ? this.submitButton : null; }
    fire(name, event = {}) { return Promise.all((this.listeners[name] || []).map(fn => fn(event))); }
}

const tick = () => new Promise(resolve => setImmediate(resolve));
const source = fs.readFileSync(path.resolve(__dirname, '../../../agents/matuto/static/context_search.js'), 'utf8');
async function main() {
    const form = new Element('form', 'context-search-start');
    form.elements.record = new Element('select'); form.elements.start_rung = new Element('select');
    const button = new Element('button'); form.submitButton = button;
    const elements = {
        'context-search-app': new Element('div'), 'context-search-start': form,
        'context-search-floor': new Element('p'), 'context-search-reason': new Element('p'),
        'context-search-rung-label': new Element('label'),
        'context-search-error': new Element('p'), 'context-search-current': new Element('div'),
        'context-search-history': new Element('div'),
    };
    const document = {activeElement: null,
        getElementById(id) { return elements[id]; }, createElement(tag) { return new Element(tag); }};
    const profile = {id: 'language-1', name: 'Studio', kind: 'language', language_id: 'language-1',
        rungs: [196608, 131072, 98304]};
    const bundle = {id: 'bundle-1', name: 'Voice', kind: 'bundle', language_id: 'language-1',
        rungs: [196608, 131072, 98304]};
    const belowFloor = {id: 'language-small', name: 'Small context', kind: 'language',
        language_id: 'language-small', trained_context: 65536, rungs: []};
    const unknownContext = {id: 'language-unknown', name: 'Unknown context', kind: 'language',
        language_id: 'language-unknown', trained_context: null, rungs: []};
    const data = {records: [profile, bundle, belowFloor, unknownContext], searches: [
        {run_id: 'winner-search', record_id: 'language-1', record_kind: 'language', record_label: 'Studio',
            outcome: 'completed', summary: {outcome: 'winner', winner: 196608, checked: true}, children: [
                {id: 'winner-rung', record_id: 'language-1', context: 196608, label: '192K',
                    outcome: 'completed', winner: true, citable: true,
                    checks: [{name: 'WQ-06', status: 'PASS', detail: 'passed'}]}]},
        {run_id: 'failed-search', record_id: 'bundle-1', record_kind: 'bundle', record_label: 'Voice',
            outcome: 'completed', summary: {outcome: 'no winner', winner: null}, children: [
                {id: 'failed-rung', record_id: 'bundle-1', context: 98304, label: '96K',
                    outcome: 'completed', winner: false, citable: false, citation_reason: 'fingerprint differs',
                    checks: [{name: 'recognition', status: 'ERROR', detail: 'unmeasured'}]}]},
    ]};
    const runs = {csrf: 'token', entries: []};
    const calls = []; const popups = []; const reasonMessages = []; let poll;
    let nextStart;
    const headers = {get: () => 'application/json'};
    const fetch = async (url, options = {}) => {
        calls.push({url, options});
        if (url === '/api/context-search') return {ok: true, headers, json: async () => data};
        if (url === '/api/runs' && options.method === 'POST' && nextStart) {
            const response = nextStart; nextStart = null; return response;
        }
        if (url === '/api/runs') return {ok: true, headers, json: async () => runs};
        if (url === '/api/status') return {ok: true, headers, json: async () => ({items: [{name: 'serving', state: 'voice'}]})};
        if (url === '/api/runs/stop-serving') return {ok: true, headers, json: async () => ({work: 'search'})};
        throw new Error(`unexpected URL: ${url}`);
    };
    const window = {matutoCsrf: '', location: {hash: '#/runs/context-search?record=bundle-1'}, listeners: {},
        addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); },
        matutoReason(control, message) { reasonMessages.push(message); control.setAttribute('aria-disabled', message ? 'true' : 'false'); },
        matutoPopup: {preview(value) { popups.push(value); }},
        setInterval(fn) { poll = fn; }, setTimeout(fn) { setImmediate(fn); }};
    vm.runInNewContext(source, {document, window, fetch, URLSearchParams, setInterval: window.setInterval,
        setTimeout: window.setTimeout, Promise, Object, String, Error, Number, Math, Map, Set});
    await tick();

    const check = (condition, message) => { if (!condition) throw new Error(message); };
    check(form.elements.record.value === 'bundle-1', 'the address must pick the named stored record');
    check(form.elements.start_rung.children.map(option => option.getAttribute('value')).join(',')
        === '196608,131072,98304', 'the form must offer only the record’s offered rungs, highest first');
    check(button.textContent === 'Stop serving and run…', 'serving must require the confirmed path');
    check(elements['context-search-history'].textContent.includes('Result: 192K.')
        && elements['context-search-history'].textContent.includes('PASS: WQ-06 — passed')
        && elements['context-search-history'].textContent.includes('Citable for acceptance at the current fingerprint.'),
        'a winner and its recorded check must remain distinct');
    check(elements['context-search-history'].textContent.includes('Result: no winner.')
        && elements['context-search-history'].textContent.includes('No winner.')
        && !elements['context-search-history'].textContent.includes('Winner. Rung 96K'),
        'a failed search must say no winner and mark no rung as the winner');
    const setContext = elements['context-search-history'].children[0].children[2].children[4];
    check(setContext.getAttribute('href') === '#/profiles/language?edit=language-1&context=196608',
        'Set context must open the ordinary editor for the language profile and winning context');
    document.activeElement = setContext;
    await poll();
    check(document.activeElement === setContext && setContext.parentNode,
        'an unchanged result poll must preserve the connected focused action');

    nextStart = {ok: false, headers, json: async () => ({error: 'slot held by active search', offer: true})};
    await form.fire('submit', {preventDefault() {}});
    const start = calls.find(call => call.url === '/api/runs' && call.options.method === 'POST');
    check(JSON.stringify(JSON.parse(start.options.body)) === JSON.stringify({action: 'start',
        procedure: 'context_search', record: 'bundle-1', start_rung: 196608}),
        'Start must send only the selected record, procedure and offered rung');
    check(elements['context-search-reason'].textContent.includes('slot held by active search'),
        'a runner refusal must be shown beside Start');
    check(popups.length === 1 && popups[0].starts[0] === 'context search for Voice',
        'serving must offer the shared confirmation before starting');
    await popups[0].confirmAction();
    const confirmation = calls.find(call => call.url === '/api/runs/stop-serving');
    check(JSON.stringify(JSON.parse(confirmation.options.body)) === JSON.stringify({confirm: true,
        procedure: 'context_search', record: 'bundle-1', start_rung: 196608}),
        'confirmation must pass the exact search request through the shared path');
    form.elements.record.selectedIndex = 2;
    await form.elements.record.fire('change');
    check(form.elements.start_rung.children.length === 0 && button.hidden
        && elements['context-search-rung-label'].hidden
        && elements['context-search-floor'].textContent.includes('start at 96K')
        && !elements['context-search-reason'].textContent && reasonMessages.at(-1) === '',
        'a record below the floor must offer no rung or Start and explain the 96K minimum once');
    form.elements.record.selectedIndex = 3;
    await form.elements.record.fire('change');
    check(form.elements.start_rung.children.length === 0 && button.hidden
        && elements['context-search-rung-label'].hidden
        && elements['context-search-floor'].textContent.includes('could not be read')
        && !elements['context-search-reason'].textContent && reasonMessages.at(-1) === '',
        'an unreadable trained context must not be misreported as below the floor');
    process.stdout.write('PASS: context-search page state and actions\n');
}

main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
