#!/usr/bin/env node
// Exercise the section's own browser script at its DOM/API seam.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.resolve(__dirname, '../../../agents/matuto/static/storage.js'), 'utf8');

class Element {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.listeners = {};
        this.attributes = {};
        this.dataset = {};
        this.value = '';
        this.type = '';
    }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
    append(...nodes) { this.children.push(...nodes); }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    async fire(name, event = {}) {
        event.currentTarget ||= this;
        event.preventDefault ||= (() => {});
        for (const callback of this.listeners[name] || []) await callback(event);
    }
    querySelector(selector) {
        return selector === 'button' ? this.children.find(child => child.tagName === 'BUTTON') : null;
    }
}

function page(data, post, failLoad = false) {
    const elements = new Map();
    for (const [id, tag] of [['storage-artifacts', 'div'], ['storage-message', 'div'],
        ['model-storage', 'div'], ['artifact-locations', 'div'],
        ['add-artifact-form', 'form'], ['new-artifact-path', 'input']]) {
        const node = new Element(tag);
        node.id = id;
        elements.set(id, node);
    }
    elements.get('add-artifact-form').append(new Element('button'));
    const document = {
        getElementById(id) {
            const find = node => node.id === id ? node : node.children.map(find).find(Boolean) || null;
            return elements.get(id) || [...elements.values()].map(find).find(Boolean) || null;
        },
        createElement: tag => new Element(tag),
    };
    const calls = [];
    const popupCalls = [];
    const notices = [];
    const reasons = [];
    const api = { async request(url, options = {}) {
        calls.push({ url, ...options });
        if (options.method === 'POST') return post(options.body);
        if (failLoad) throw new Error('manager unavailable');
        return data;
    }};
    const popup = { open: options => popupCalls.push(options) };
    const notice = (...args) => notices.push(args);
    const clearNotice = (...args) => notices.push(['clear', ...args]);
    const reason = (control, text) => reasons.push({ control, text });
    new Function('document', 'api', 'popup', 'notice', 'clearNotice', 'reason', source)(
        document, api, popup, notice, clearNotice, reason);
    return { document, calls, popupCalls, notices, reasons };
}

async function main() {
    const direct = page({ model_storage: null, locations: [] }, async body => {
        assert.deepEqual(body, { action: 'save-model-storage', path: '/home/operator/models' });
        return { available: true };
    });
    await new Promise(resolve => setImmediate(resolve));
    assert.match(direct.document.getElementById('model-storage').textContent, /No directory is chosen/);
    assert.match(direct.document.getElementById('model-storage').textContent, /No path is guessed/);
    const rootForm = direct.document.getElementById('model-storage').children[1];
    const rootInput = rootForm.children.find(child => child.tagName === 'INPUT');
    rootInput.value = '/home/operator/models';
    await rootForm.fire('submit');
    assert.match(direct.document.getElementById('storage-message').textContent, /Save model storage saved/);

    const rejected = page({ model_storage: null, locations: [] }, async () => {
        throw new Error('the path must be absolute');
    });
    await new Promise(resolve => setImmediate(resolve));
    const rejectedForm = rejected.document.getElementById('model-storage').children[1];
    const rejectedInput = rejectedForm.children.find(child => child.tagName === 'INPUT');
    rejectedInput.value = 'relative/path';
    await rejectedForm.fire('submit');
    assert.equal(rejectedInput.value, 'relative/path');
    assert.equal(rejected.document.getElementById('storage-message').textContent, '');
    assert.equal(rejected.reasons.at(-1).control, rejectedInput);
    assert.match(rejected.reasons.at(-1).text, /absolute/);
    const addForm = rejected.document.getElementById('add-artifact-form');
    const addInput = rejected.document.getElementById('new-artifact-path');
    addInput.value = 'relative/artifacts';
    await addForm.fire('submit');
    assert.equal(addInput.value, 'relative/artifacts');
    assert.equal(rejected.reasons.at(-1).control, addInput);
    assert.match(rejected.reasons.at(-1).text, /absolute/);

    const failedLoad = page({ model_storage: null, locations: [] }, async () => {}, true);
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(failedLoad.notices.some(args => args[0] === 'settings' && args[1] === 'storage-load'));
    const repairData = { model_storage: { path: '/missing', location_id: 'missing', available: false },
        locations: [{ location_id: 'missing', path: '/missing', model_storage: true,
            available: false, reason: 'does not exist', records: [] }] };
    const unavailable = page(repairData, async () => {});
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(unavailable.notices.some(args => args[0] === 'settings'
        && args[1] === 'storage-availability' && /does not exist/.test(args[4])));
    const checkAgain = unavailable.document.getElementById('model-storage').children
        .find(node => node.tagName === 'BUTTON');
    assert.equal(checkAgain.textContent, 'Check again');
    repairData.model_storage.available = true;
    repairData.locations[0].available = true;
    await checkAgain.fire('click');
    assert.match(unavailable.document.getElementById('storage-message').textContent,
        /Model storage is readable again/);
    assert.match(unavailable.document.getElementById('model-storage').textContent, /Available/);
    assert.equal(unavailable.document.getElementById('model-storage').children
        .some(node => node.tagName === 'BUTTON'), false);

    const existing = {
        model_storage: { location_id: 'loc-root', path: '/old', available: true },
        locations: [{ location_id: 'loc-root', path: '/old', model_storage: true,
            available: true, records: [{ name: 'Model', acquisition_id: 'acq', verified_at: 'before' }],
            profiles: [{ name: 'Writer' }], bundles: [{ name: 'Studio' }] }],
    };
    const change = page(existing, async body => {
        if (body.action === 'preview-change') return {
            path: '/old', new_path: '/new', model_storage: true,
            changes: { model_storage: { from: '/old', to: '/new' },
                artifact_location: { from: '/old', to: '/new' } },
            records: [{ name: 'Model', acquisition_id: 'acq' }],
            profiles: [{ name: 'Writer' }], bundles: [{ name: 'Studio' }],
        };
        assert.deepEqual(body, { action: 'confirm-change', location_id: 'loc-root', path: '/new' });
        existing.model_storage.path = '/new';
        existing.locations[0].path = '/new';
        existing.locations[0].records[0].verified_at = null;
        return { available: false, reason: 'does not exist', records: [
            { name: 'Model', available: false, reason: 'model.gguf is missing', files: [
                { file_name: 'model.gguf', state: 'missing', reason: 'model.gguf is missing' },
            ] },
        ] };
    });
    await new Promise(resolve => setImmediate(resolve));
    const reviewForm = change.document.getElementById('model-storage').children.find(node => node.tagName === 'FORM');
    const newPath = reviewForm.children.find(node => node.tagName === 'INPUT');
    newPath.value = '/new';
    await reviewForm.fire('submit');
    assert.equal(change.popupCalls.length, 1);
    const preview = change.popupCalls[0];
    const originalReviewButton = reviewForm.children.find(node => node.tagName === 'BUTTON');
    assert.equal(preview.returnFocus(), originalReviewButton);
    assert.equal(preview.title, 'Review storage change');
    assert.equal(preview.confirm, 'Save both settings');
    assert.equal(preview.content.className, 'storage-artifacts');
    assert.match(preview.content.textContent, /No files will be moved\. \/old → \/new/);
    assert.match(preview.content.textContent, /Model storage: \/old → \/new/);
    assert.match(preview.content.textContent, /Linked artifact location: \/old → \/new/);
    assert.doesNotMatch(preview.content.textContent, /undefined/);
    assert.match(preview.content.textContent, /Affected acquisition records.*Model/);
    assert.match(preview.content.textContent, /Writer/);
    assert.match(preview.content.textContent, /Studio/);
    assert.match(preview.content.textContent, /Runs & jobs/);
    await assert.rejects(preview.confirmAction, /Path saved, but it is unavailable.*model\.gguf is missing/);
    assert.equal(change.popupCalls.length, 1, 'failed confirmation remains in the same popup');
    assert.match(change.document.getElementById('storage-message').textContent, /Path saved, but it is unavailable/);
    assert.match(change.document.getElementById('model-storage').textContent, /\/new/);
    assert.match(change.document.getElementById('artifact-locations').textContent, /files not verified at this location/);
    assert.notEqual(preview.returnFocus(), originalReviewButton);
    assert.equal(preview.returnFocus().id, 'review-model-storage-action');

    const verifiedData = structuredClone(existing);
    verifiedData.model_storage.path = '/old';
    verifiedData.locations[0].path = '/old';
    verifiedData.locations[0].records[0].verified_at = 'before';
    const verified = page(verifiedData, async body => {
        if (body.action === 'preview-change') return {
            path: '/old', new_path: '/new', model_storage: true,
            changes: { model_storage: { from: '/old', to: '/new' },
                artifact_location: { from: '/old', to: '/new' } },
            records: [], profiles: [], bundles: [],
        };
        verifiedData.model_storage.path = '/new';
        verifiedData.locations[0].path = '/new';
        return {
            available: true,
            records: [{ name: 'Model', available: true, files: [
                { file_name: 'model.gguf', state: 'verified', reason: '' },
            ] }],
            profiles: [{ name: 'Writer' }], bundles: [{ name: 'Studio' }],
        };
    });
    await new Promise(resolve => setImmediate(resolve));
    const verifiedForm = verified.document.getElementById('model-storage').children.find(node => node.tagName === 'FORM');
    verifiedForm.children.find(child => child.tagName === 'INPUT').value = '/new';
    await verifiedForm.fire('submit');
    await verified.popupCalls[0].confirmAction();
    assert.match(verified.document.getElementById('storage-message').textContent, /Acquisition record Model: available/);
    assert.match(verified.document.getElementById('storage-message').textContent, /Qualifications remain unchanged/);
    assert.match(verified.document.getElementById('model-storage').textContent, /\/new/);

    const extraData = {
        model_storage: { location_id: 'root', path: '/models', available: true },
        locations: [
            { location_id: 'root', path: '/models', model_storage: true, available: true, records: [], profiles: [], bundles: [] },
            { location_id: 'extra', path: '/artifacts', model_storage: false, available: true, records: [], profiles: [], bundles: [] },
        ],
    };
    const extra = page(extraData, async body => ({
        path: '/artifacts', new_path: '/new-artifacts', model_storage: false,
        changes: { model_storage: null, artifact_location: { from: '/artifacts', to: '/new-artifacts' } },
        records: [], profiles: [], bundles: [],
    }));
    await new Promise(resolve => setImmediate(resolve));
    const extraArticle = extra.document.getElementById('artifact-locations').children[1];
    const extraForm = extraArticle.children.find(node => node.tagName === 'FORM');
    extraForm.children.find(node => node.tagName === 'INPUT').value = '/new-artifacts';
    await extraForm.fire('submit');
    assert.equal(extra.popupCalls[0].confirm, 'Save artifact location');
    assert.match(extra.popupCalls[0].content.textContent, /Artifact location: \/artifacts → \/new-artifacts/);
    assert.doesNotMatch(extra.popupCalls[0].content.textContent, /Model storage:/);

    const busy = page(existing, async () => { throw new Error('Stop serving before changing model storage'); });
    await new Promise(resolve => setImmediate(resolve));
    const busyForm = busy.document.getElementById('model-storage').children.find(node => node.tagName === 'FORM');
    const busyButton = busyForm.children.find(node => node.tagName === 'BUTTON');
    busyForm.children.find(node => node.tagName === 'INPUT').value = '/busy';
    await busyForm.fire('submit');
    assert.equal(busy.popupCalls.length, 0);
    assert.equal(busy.reasons.at(-1).control, busyButton);
    assert.match(busy.reasons.at(-1).text, /Stop serving/);
}

main().then(() => console.log('PASS: storage page save, review and partial outcome'))
    .catch(error => { console.error(error); process.exit(1); });
