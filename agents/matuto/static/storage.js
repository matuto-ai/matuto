'use strict';

(() => {
    const $ = id => document.getElementById(id);
    const root = $('storage-artifacts');
    if (!root) return;
    const element = (tag, text, className) => {
        const node = document.createElement(tag);
        if (text !== undefined) node.textContent = text;
        if (className) node.className = className;
        return node;
    };
    const button = (label, action) => {
        const node = element('button', label);
        node.type = 'button';
        node.addEventListener('click', action);
        return node;
    };
    const message = text => { $('storage-message').textContent = text || ''; };
    const outcome = item => item.available ? 'Available' : `Unavailable: ${item.reason}`;

    async function request(action, values = {}) {
        return api.request('/api/storage', {
            method: 'POST', body: { action, ...values },
        });
    }

    function pathForm({ id, label, initial = '', submit, onSaved, review = false }) {
        const form = element('form');
        const inputId = `${id}-path`;
        const labelNode = element('label', label);
        labelNode.htmlFor = inputId;
        const input = element('input');
        input.id = inputId;
        input.name = 'path';
        input.type = 'text';
        input.required = true;
        input.autocomplete = 'off';
        input.value = initial;
        const action = element('button', review ? 'Review…' : submit);
        action.id = `${id}-action`;
        action.type = 'submit';
        form.append(labelNode, input, action);
        form.addEventListener('submit', async event => {
            event.preventDefault();
            reason(review ? action : input, '');
            action.disabled = true;
            try {
                const result = await onSaved(input.value, submit);
                if (review) return;
                if (!result.available && result.reason) {
                    const unavailable = result.records
                        ? result.records.filter(record => !record.available)
                        : [];
                    const records = unavailable.map(record => `${record.name}: ${record.reason}`).join('; ');
                    message(`Path saved, but it is unavailable: ${result.reason}${records ? `. Records unavailable: ${records}` : ''}`);
                } else {
                    message(`${submit} saved.`);
                }
                await load();
            } catch (error) {
                reason(review ? action : input, error.message);
            } finally {
                action.disabled = false;
            }
        });
        input.addEventListener('input', () => reason(input, ''));
        if (review) action.textContent = 'Review…';
        return form;
    }

    function reviewContent(preview) {
        const content = element('div', undefined, 'storage-artifacts');
        content.append(element('p', `No files will be moved. ${preview.path} → ${preview.new_path}`));
        const jobs = element('a', 'Runs & jobs');
        jobs.href = '#/runs';
        content.append(jobs);
        if (preview.model_storage) {
            const change = preview.changes.model_storage;
            const linked = preview.changes.artifact_location;
            content.append(element('p', `Model storage: ${change.from} → ${change.to}`));
            content.append(element('p', `Linked artifact location: ${linked.from} → ${linked.to}`));
        } else {
            const change = preview.changes.artifact_location;
            content.append(element('p', `Artifact location: ${change.from} → ${change.to}`));
        }
        const records = element('section');
        records.append(element('h3', 'Affected acquisition records'));
        const recordList = element('ul');
        for (const record of preview.records || []) {
            const detail = [record.name, ...(record.files || []).map(file => file.file_name)];
            recordList.append(element('li', detail.join(': ')));
        }
        if (!preview.records || !preview.records.length) recordList.append(element('li', 'None'));
        records.append(recordList);
        content.append(records);
        for (const [label, key] of [['Profiles', 'profiles'], ['Bundles', 'bundles']]) {
            const group = element('section');
            group.append(element('h3', label));
            const list = element('ul');
            for (const item of preview[key] || [])
                list.append(element('li', item.name));
            if (!preview[key] || !preview[key].length) list.append(element('li', 'None'));
            group.append(list);
            content.append(group);
        }
        return content;
    }

    function changeOutcome(result) {
        const details = (result.records || []).map(record => {
            const files = (record.files || []).filter(file => file.state !== 'verified')
                .map(file => `${file.file_name}: ${file.reason}`).join('; ');
            return `Acquisition record ${record.name}: ${record.available ? 'available' : `unavailable${files ? ` (${files})` : ''}`}`;
        }).join('. ');
        if (!result.available)
            return `Path saved, but it is unavailable: ${result.reason || 'one or more records could not be verified'}. ${details}. Recorded identities and qualification evidence are unchanged.`;
        return `Storage location saved. ${details || 'No acquisition records were affected.'} Qualifications remain unchanged.`;
    }

    async function beginChange(location, path) {
        const preview = await request('preview-change', { location_id: location.location_id, path });
        popup.open({
            title: 'Review storage change',
            content: reviewContent(preview),
            returnFocus: () => $(location.model_storage
                ? 'review-model-storage-action'
                : `artifact-${location.location_id}-action`),
            safe: 'Cancel',
            confirm: location.model_storage ? 'Save both settings' : 'Save artifact location',
            confirmAction: async () => {
                const result = await request('confirm-change', {
                    location_id: location.location_id, path,
                });
                const outcome = changeOutcome(result);
                message(outcome);
                await load();
                if (!result.available) throw new Error(outcome);
            },
        });
    }

    function render(data) {
        const model = $('model-storage');
        const locations = $('artifact-locations');
        model.textContent = '';
        locations.textContent = '';
        const unavailable = (data.locations || []).filter(location => !location.available);
        if (unavailable.length) {
            notice('settings', 'storage-availability', 'repair', 'Storage locations are unavailable',
                unavailable.map(location => `${location.path}: ${location.reason}`).join('; '));
        } else {
            clearNotice('settings', 'storage-availability');
        }
        if (!data.model_storage) {
            model.append(element('p', 'No directory is chosen. No path is guessed, and downloads stay disabled until model storage is saved.'));
            model.append(pathForm({
                id: 'model-storage', label: 'Model storage path', submit: 'Save model storage',
                onSaved: path => request('save-model-storage', { path }),
            }));
        } else {
            model.append(element('p', data.model_storage.path));
            model.append(element('p', outcome(data.model_storage)));
            if (!data.model_storage.available) model.append(button('Check again', checkAgain));
            model.append(element('p', 'Download destination, native discovery directory and linked artifact location.'));
            const location = data.locations.find(item => item.location_id === data.model_storage.location_id);
            if (location) model.append(pathForm({
                id: 'review-model-storage', label: 'New model storage path', initial: location.path,
                submit: 'Save both settings', review: true,
                onSaved: path => beginChange(location, path),
            }));
        }
        for (const location of data.locations) {
            const item = element('article', undefined, 'panel');
            item.append(element('h5', location.model_storage ? 'Model storage · linked artifact location' : 'Artifact location'));
            item.append(element('p', location.path));
            item.append(element('p', outcome(location)));
            const records = element('ul');
            for (const record of location.records || []) records.append(element('li',
                `${record.name} — ${record.verified_at ? 'files verified' : 'files not verified at this location'}`));
            if (!location.records || !location.records.length) records.append(element('li', 'No acquisition records'));
            item.append(element('p', 'Records at this location:'));
            item.append(records);
            for (const profile of location.profiles || []) item.append(element('p', `Profile: ${profile.name}`));
            for (const bundle of location.bundles || []) item.append(element('p', `Bundle: ${bundle.name}`));
            if (!location.model_storage) item.append(pathForm({
                id: `artifact-${location.location_id}`, label: 'New artifact location path',
                initial: location.path, submit: 'Save artifact location', review: true,
                onSaved: path => beginChange(location, path),
            }));
            locations.append(item);
        }
    }

    async function checkAgain() {
        const data = await load();
        if (data) message(data.model_storage?.available
            ? 'Model storage is readable again.' : 'Model storage is still unavailable.');
    }

    async function load() {
        try {
            const data = await api.request('/api/storage');
            render(data);
            clearNotice('settings', 'storage-load');
            return data;
        } catch (error) {
            notice('settings', 'storage-load', 'blocker', 'Storage & artifacts could not load', error.message);
            return null;
        }
    }

    $('add-artifact-form').addEventListener('submit', async event => {
        event.preventDefault();
        const input = $('new-artifact-path');
        const submit = event.currentTarget.querySelector('button');
        reason(input, '');
        submit.disabled = true;
        try {
            const result = await request('add-artifact-location', { path: input.value });
            message(result.available ? 'Artifact location added.' :
                `Artifact location saved, but unavailable: ${result.reason}`);
            await load();
            if (result.available) input.value = '';
        } catch (error) {
            reason(input, error.message);
        } finally {
            submit.disabled = false;
        }
    });
    $('new-artifact-path').addEventListener('input', event => reason(event.currentTarget, ''));
    load();
})();
