#!/usr/bin/env node
// Exercise the Bundles page at its element and shared-popup boundaries.
'use strict';

const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert/strict');

class Element {
    constructor(tag = 'div') {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.listeners = {};
        this.attributes = {};
        this.dataset = {};
        this.hidden = false;
        this.value = '';
        this.disabled = false;
        this.readOnly = false;
        this.text = '';
    }
    set textContent(value) { this.text = String(value); this.children = [] }
    get textContent() { return this.text + this.children.map(child => child.textContent || '').join('') }
    append(...items) { this.children.push(...items) }
    replaceChildren(...items) { this.children = items; this.text = '' }
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler) }
    async fire(name, event = {}) {
        for (const handler of this.listeners[name] || []) await handler(event);
    }
    focus() { this.focused = true }
    scrollIntoView() {}
}

async function main() {
    const names = ['mode', 'bundle_id', 'name', 'language', 'synthesis', 'recognition',
        'load_tokens', 'load_prompt', 'notes'];
    const controls = Object.fromEntries(names.map(name => [name, new Element('input')]));
    const form = new Element('form');
    form.hidden = true;
    form.elements = {namedItem: name => controls[name]};
    const ids = Object.fromEntries(['bundle-form', 'bundle-error', 'bundle-save',
        'bundle-form-title', 'bundle-list', 'bundle-create', 'bundle-cancel'].map(id => [id, new Element()]));
    ids['bundle-form'] = form;
    ids['bundle-save'].textContent = 'Save bundle';
    const document = {
        head: new Element('head'),
        getElementById: id => ids[id] || null,
        querySelector: () => ({content: 'csrf-fixture'}),
        createElement: tag => new Element(tag),
    };
    const bundle = {id: 'b-1', name: 'Studio', members: {
        language: {id: 'l-1', name: 'Language', available: true},
        synthesis: {id: 's-1', name: 'Synthesis', available: true},
        recognition: {id: 'r-1', name: 'Recognition', available: true}},
        load_tokens: 128, load_prompt: 'Old prompt', notes: '',
        qualification: {state: 'candidate', changed_since_qualification: false},
        available: true};
    const calls = [];
    const popups = [];
    global.document = document;
    global.window = new Element();
    window.matutoPopup = {open: options => popups.push(options)};
    window.location = {hash: '#/profiles/bundles'};
    global.fetch = async (_url, options = {}) => {
        const body = options.body ? JSON.parse(options.body) : null;
        if (body) calls.push(body);
        const value = !body ? {bundles: [bundle], profiles: {
            language: [{id: 'l-1', name: 'Language'}, {id: 'l-2', name: 'New language'}],
            synthesis: [{id: 's-1', name: 'Synthesis'}],
            recognition: [{id: 'r-1', name: 'Recognition'}]}}
            : body.action === 'preview' ? {preview: {kind: 'served-change',
                changed_values: [{field: 'stated_load', label: 'Bundle load', old: 128, new: body.load_tokens},
                    {field: 'members.language', label: 'Language profile', old: 'l-1', new: 'l-2'}],
                state: {state: 'candidate', changed_since_qualification: true}},
                availability: {available: true}, review_token: 'review-1'}
            : {bundle: {...bundle, load_tokens: body.load_tokens}, warnings: []};
        return {ok: true, json: async () => value};
    };
    const source = fs.readFileSync(path.resolve(__dirname, '../../matuto/static/bundles.js'), 'utf8');
    vm.runInThisContext(source, {filename: 'bundles.js'});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(document.head.children[0].href, '/bundles.css');

    const actions = ids['bundle-list'].children[0].children.at(-1).children;
    await actions[0].fire('click');
    controls.load_tokens.value = '256';
    controls.load_prompt.value = 'New prompt';
    let prevented = false;
    await form.fire('submit', {preventDefault: () => { prevented = true }});
    assert.equal(prevented, true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].action, 'preview');
    assert.equal(calls[0].load_tokens, 256);
    assert.equal(popups.length, 1);
    assert.equal(popups[0].safe, 'Cancel');
    assert.equal(popups[0].confirm, 'Save bundle');
    assert.match(popups[0].content.textContent, /Bundle load: 128 → 256/);
    assert.match(popups[0].content.textContent,
        /Language profile: Language \(l-1\) → New language \(l-2\)/);
    assert.match(popups[0].content.textContent,
        /Bundle state after saving: candidate — changed since qualification/);

    await popups[0].confirmAction();
    assert.equal(calls.length, 2);
    assert.equal(calls[1].action, 'save');
    assert.equal(calls[1].confirm, true);
    assert.equal(calls[1].review_token, 'review-1');
    assert.equal(calls[1].load_prompt, 'New prompt');

    const removeCalls = calls.length;
    await actions[2].fire('click');
    assert.equal(calls.length, removeCalls);
    assert.equal(popups.length, 2);
    assert.equal(popups[1].safe, 'Cancel');
    assert.equal(popups[1].confirm, 'Remove bundle');
    assert.match(popups[1].content.textContent, /qualification records and runs will remain browsable/);
    await popups[1].confirmAction();
    assert.equal(calls.at(-1).action, 'remove');

    bundle.qualification.state = 'qualified';
    form.hidden = false;
    controls.language.value = 'l-2';
    await window.fire('hashchange');
    await new Promise(resolve => setImmediate(resolve));
    assert.match(ids['bundle-list'].textContent, /Qualification: qualified/);
    assert.equal(controls.language.value, 'l-2', 'Returning from acceptance keeps the open editor selection');
    console.log('PASS: bundle review and removal use the shared confirmation popup');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
