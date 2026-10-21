#!/usr/bin/env node
// Exercise the import/export section's own behavior against element doubles.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const AGENTS = path.resolve(__dirname, '../../../agents');
const source = fs.readFileSync(path.join(AGENTS, 'matuto/static/import_export.js'), 'utf8');

class Element {
    constructor() { this.children = []; this.listeners = {}; this.attributes = {}; this.value = ''; this.hidden = false; }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return (this.text || '') + this.children.map(child => child.textContent || '').join(''); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    append(...children) { this.children.push(...children); }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    async fire(name, event = {}) { for (const fn of this.listeners[name] || []) await fn(event); }
    focus() { this.focused = true; }
    querySelectorAll(selector) { return selector === 'input:checked' ? this.children.flatMap(child => child.children).filter(node => node.checked) : []; }
}

async function main() {
    const ids = ['importExportForm','importExportFile','exportRecordList','importExportError',
        'importExportStatus','exportSelected'];
    const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
    const document = {
        addEventListener(_name, fn) { this.ready = fn; },
        getElementById(id) { return elements[id]; },
        createElement() { return new Element(); },
        createTextNode(text) { return {textContent: text}; },
    };
    const requests = [], navigations = [], notices = [], cleared = [];
    let rejectImport = '';
    const api = {async request(path, options = {}) {
        requests.push({path, options});
        if (options.method === 'POST') {
            if (rejectImport) throw new Error(rejectImport);
            return {results: [{kind:'language',id:'language-new',action:'created',warnings:[]},
                {kind:'bundle',id:'bundle-new',action:'created',warnings:[]}]};
        }
        return {records:[{kind:'language',id:'language-new',name:'Language'},
            {kind:'bundle',id:'bundle-new',name:'Bundle'}]};
    }};
    vm.runInNewContext(source, {document, api, window:{location:{assign:path => navigations.push(path)}},
        notice:(...args)=>notices.push(args), clearNotice:(...args)=>cleared.push(args)});
    await document.ready(); await new Promise(resolve => setImmediate(resolve));
    assert.match(elements.exportRecordList.textContent, /Language — language/);
    assert.match(elements.exportRecordList.textContent, /Bundle — bundle/);

    elements.importExportFile.files = [{size:12}];
    await elements.importExportForm.fire('submit', {preventDefault(){}});
    assert.match(elements.importExportStatus.textContent, /language language-new: created/);
    assert.match(elements.importExportStatus.textContent, /bundle bundle-new: created/);
    assert.equal(requests.find(item=>item.options.method==='POST').options.rawBody,
        elements.importExportFile.files[0]);

    const checks = elements.exportRecordList.children.map(node => node.children[0]);
    checks[1].checked = true;
    await elements.exportSelected.fire('click');
    assert.deepEqual(navigations, ['/api/import-export?download=1&record=bundle%3Abundle-new']);

    rejectImport = "bundle record 'Bundle' field 'members.recognition' is invalid";
    await elements.importExportForm.fire('submit', {preventDefault(){}});
    assert.match(elements.importExportError.textContent, /field 'members.recognition'/);
    assert.equal(elements.importExportError.focused, true);
    rejectImport = 'The document is malformed JSON.';
    elements.importExportFile.files = [{size:12}];
    await elements.importExportForm.fire('submit', {preventDefault(){}});
    assert.equal(elements.importExportError.textContent, 'The document is malformed JSON.');
    elements.importExportFile.files = [{size:1048577}];
    await elements.importExportForm.fire('submit', {preventDefault(){}});
    assert.match(elements.importExportError.textContent, /1 MiB limit/);
}

main().then(() => console.log('PASS: import/export page reports imports, refusals, and selected exports'))
    .catch(error => { console.error(error.stack || error); process.exit(1); });
