#!/usr/bin/env node
// Exercise the Model files page script against element doubles and the HTTP contract.
'use strict';

const { execFileSync } = require('child_process');
const path = require('path');

const AGENTS = path.resolve(__dirname, '../../../agents');
let activeElement = null;
const pageHtml = execFileSync(process.env.PYTHON || 'python3', ['-B', '-c',
    'import sys; from matuto.discovery import load_item; sys.stdout.write(load_item("page", "model_files").html({"csrf":"fixture-token"}))'],
    { cwd: AGENTS, encoding: 'utf8' });
const script = pageHtml.match(/<script nonce="[^"]*">([\s\S]*?)<\/script>/)[1];

class Element {
    constructor(id = '', data = {}) {
        this.id = id;
        this.dataset = data;
        this.tagName = '';
        this.attributes = {};
        this.listeners = {};
        this.children = [];
        this.value = '';
        this.checked = false;
        this.hidden = false;
        this.type = '';
        this.text = '';
    }
    set textContent(value) { this.text = String(value); this.children = [] }
    get textContent() { return this.text + this.children.map(child => child.textContent || '').join('') }
    get options() { return this.children }
    setAttribute(name, value) { this.attributes[name] = String(value) }
    getAttribute(name) { return this.attributes[name] ?? null }
    removeAttribute(name) { delete this.attributes[name] }
    get selectedOptions() { return this.children.filter(child => child.value === this.value) }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback) }
    fire(name, event = {}) { return Promise.all((this.listeners[name] || []).map(callback => callback(event))) }
    append(...nodes) { this.children.push(...nodes) }
    focus() { activeElement = this }
}

async function main() {
    const names = ['prepare', 'select', 'review', 'acquire'];
    const panels = names.map((step, index) => { const panel = new Element('', { wizardStep: step }); panel.hidden = index !== 0; return panel });
    const stepButtons = names.map((step, index) => { const button = new Element('', { step }); if (index === 0) button.setAttribute('aria-current', 'step'); return button });
    const backButtons = [new Element('', { back: 'prepare' }), new Element('', { back: 'select' }),
        new Element('', { back: 'review' })];
    const nextButtons = [new Element('', { next: 'select' }), new Element('', { next: 'acquire' })];
    const ids = ['acquisitionWizard', 'wizard-message', 'prepare-status', 'prepare-continue', 'repository', 'revision',
        'file-list', 'review-details', 'consent-row', 'no-checksum-consent', 'acquire-status', 'download-list',
        'acquisition-record-list', 'adopt-location', 'list-artifact-files', 'artifact-file-list', 'adopt-name',
        'adopt-consent', 'adopt-submit', 'adopt-reason', 'adopt-settings-link', 'adopt-status',
        'list-files', 'resolve-files', 'start-download'];
    const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
    elements['adopt-settings-link'].href = '#/settings/storage-artifacts';
    const root = elements.acquisitionWizard;
    root.querySelectorAll = selector => ({
        '[data-wizard-step]': panels,
        '[data-step]': stepButtons,
        '[data-back]': backButtons,
        '[data-next]': nextButtons,
    })[selector] || [];
    elements['download-list'].querySelectorAll = selector => elements['download-list'].children
        .flatMap(article => article.children).filter(child => selector.split(',').map(tag => tag.trim().toUpperCase()).includes(child.tagName));
    elements['no-checksum-consent'].type = 'checkbox';
    elements.revision.value = 'main';
    let domReady;
    const document = {
        documentElement: { dataset: { theme: 'light' } },
        get activeElement() { return activeElement; },
        getElementById: id => elements[id] || null,
        createElement: tag => { const node = new Element(); node.tagName = tag.toUpperCase(); return node; },
        createTextNode: text => ({ textContent: String(text) }),
        addEventListener: (name, callback) => { if (name === 'DOMContentLoaded') domReady = callback; },
    };
    const calls = [], notices = new Map(), reasons = new Map();
    let pauseResolve = false, releaseResolve, pauseList = false, releaseList;
    let artifactFilesResponse = {files:[{name:'models/model-00001-of-00002.gguf',size:8}]};
    let destination = '/models', acquireError = 'The admission slot is held by a build; nothing is queued.';
    let prepareAnswer = { ready: true, blockers: [], work: null, downloads: [], artifact_locations: [
        {location_id:'loc-artifacts',path:'/artifacts'}], records: [
        {acquisition_id:'acq-language',name:'Language model',source:'owner/model',revision:'abc',
            label:'published checksums matched',verified_at:'now',complete:true,reason:'',profile_kind:'language',
            files:[{file_name:'model.gguf',size_bytes:12,published_sha256:'pub',computed_sha256:'sha'}]},
        {acquisition_id:'acq-speech',name:'Speech weights',source:'unknown',revision:'unknown',
            label:'locally hashed; publisher unverified',verified_at:'now',complete:true,reason:'',profile_kind:'speech',
            files:[{file_name:'whisper-config.json',size_bytes:8,published_sha256:null,computed_sha256:'config'},
                {file_name:'whisper.bin',size_bytes:12,published_sha256:null,computed_sha256:'digest'}]},
        {acquisition_id:'acq-bad',name:'Broken model',source:'unknown',revision:'unknown',
            label:'locally hashed; publisher unverified',verified_at:null,complete:false,reason:'model.gguf is missing',profile_kind:'language',
            files:[{file_name:'model.gguf',size_bytes:12,published_sha256:null,computed_sha256:'',reason:'model.gguf is missing'}]},
        {acquisition_id:'acq-ambiguous',name:'Ambiguous weights',source:'unknown',revision:'unknown',
            label:'locally hashed; publisher unverified',verified_at:'now',complete:true,reason:'',profile_kind:'unknown',
            files:[{file_name:'weights.gguf',size_bytes:12,published_sha256:null,computed_sha256:'sha'}]},
        {acquisition_id:'acq-speech-ambiguous',name:'Speech set',source:'unknown',revision:'unknown',
            label:'locally hashed; publisher unverified',verified_at:'now',complete:true,reason:'',profile_kind:'speech',
            files:[{file_name:'voice-a.bin',size_bytes:12,published_sha256:null,computed_sha256:'a'},
                {file_name:'voice-b.safetensors',size_bytes:12,published_sha256:null,computed_sha256:'b'}]},
    ] };
    let downloadPoll, pollCleared = false, downloadSnapshot = [];
    const popups = [];
    let changedDiscard = false, resumeError = '';
    const notice = (_page, key, kind, title, _detail, link) => notices.set(key, { kind, title, link });
    const clearNotice = (_page, key) => notices.delete(key);
    const reason = (control, text) => { if (text) reasons.set(control, text); else reasons.delete(control); };
    const files = [
        { name: 'model.gguf', size: 8, sha256: 'a'.repeat(64), checksum_status: 'published SHA-256' },
        { name: 'tokenizer.json', size: 4, sha256: null, checksum_status: 'no published SHA-256' },
    ];
    const fetch = async (_url, options) => {
        const body = options.body ? JSON.parse(options.body) : null;
        calls.push(body || { method: options.method });
        let answer;
        if (!body) answer = { ...prepareAnswer, downloads: downloadSnapshot };
        else if (body.action === 'resume') {
            if (resumeError) answer = { error: resumeError };
            else { answer = { job_id: body.job_id }; downloadSnapshot = []; }
        }
        else if (body.action === 'discard-preview') answer = { job_id: body.job_id, files: [{ name: 'model.gguf', size: 8 }], total_size: 8, consequence: 'The next attempt starts again from nothing.' };
        else if (body.action === 'discard' && changedDiscard) { changedDiscard = false; answer = { error: 'staging changed; review the fresh discard preview', preview: { job_id: body.job_id, files: [{ name: 'new.bin', size: 3 }], total_size: 3, consequence: 'The next attempt starts again from nothing.' } }; }
        else if (body.action === 'discard') answer = { job_id: body.job_id, discarded: true };
        else if (body.action === 'stop') {
            answer = { id: body.id, outcome: 'cancelled', state: 'cancelled' };
            downloadSnapshot = [{ id: body.id, purpose: 'owner/model@abc', outcome: 'cancelled', state: 'cancelled',
                progress: { elapsed: 8, unknown: '' }, phase: '', file: '', transferred: null, total: null,
                reason: 'Cancelled with partials kept' }];
        }
        else if (body.action === 'artifact-files') answer = artifactFilesResponse;
        else if (body.action === 'adopt') answer = { acquisition_id:'acq-new',complete:true,label:'locally hashed; publisher unverified' };
        else if (body.action === 'verify-record') answer = { complete:false,reason:'model-00002-of-00002.gguf changed',files:[] };
        else if (body.action === 'list') answer = { files };
        else if (body.action === 'resolve') answer = { repository: body.repository, origin: 'owner',
            revision: '0123456789abcdef0123456789abcdef01234567', files, total_size: 12,
            destination };
        else answer = { error: acquireError };
        if (body && body.action === 'resolve' && body.revision === 'denied') answer = { error: 'source refused' };
        if (body && body.action === 'resolve' && pauseResolve) await new Promise(resolve => { releaseResolve = resolve; });
        if (body && body.action === 'list' && pauseList) await new Promise(resolve => { releaseList = resolve; });
        return { ok: !body || (body.action !== 'acquire' && answer.error === undefined), json: async () => answer };
    };
    const window = { location: { hash: '#/model-files' }, listeners: {},
        addEventListener: (name, callback) => { window.listeners[name] = callback; },
        setInterval: callback => { downloadPoll = callback; return 1; },
        clearInterval: () => { pollCleared = true; }, matutoPopup: {
            preview: popup => { popups.push(popup); activeElement = new Element(); },
            open: popup => { popups.push(popup); activeElement = new Element(); },
        } };
    new Function('document', 'fetch', 'notice', 'clearNotice', 'reason', 'window', script)(document, fetch, notice, clearNotice, reason, window);
    domReady();
    await new Promise(resolve => setImmediate(resolve));
    const click = element => element.fire('click');
    const check = (condition, message) => { if (!condition) throw new Error(message) };

    check(!panels[0].hidden && panels.slice(1).every(panel => panel.hidden), 'Prepare is initially current');
    check(stepButtons[0].getAttribute('aria-current') === 'step', 'current step is exposed accessibly');
    check(elements['prepare-status'].textContent.includes('Nothing running'), 'idle work uses the approved wording');
    check(elements['acquisition-record-list'].textContent.includes('Language model') &&
        elements['acquisition-record-list'].textContent.includes('published checksum: pub') &&
        elements['acquisition-record-list'].textContent.includes('model.gguf is missing'),
        'the page lists files, provenance, verification and unusable reasons for every record: ' + elements['acquisition-record-list'].textContent);
    let recordArticles = elements['acquisition-record-list'].children;
    check(recordArticles[0].children.some(child => child.textContent === 'Use in a new profile') &&
        recordArticles[1].children.some(child => child.textContent === 'Use in a new profile') &&
        !recordArticles[2].children.some(child => child.textContent === 'Use in a new profile'),
        'only complete records offer a profile handoff');
    check(recordArticles[0].children.find(child => child.textContent === 'Use in a new profile').href.includes('acquisition_id=acq-language') &&
        recordArticles[1].children.find(child => child.textContent === 'Use in a new profile').href.includes('#/profiles/speech-profiles') &&
        recordArticles[1].children.find(child => child.textContent === 'Use in a new profile').href.includes('file_name=whisper.bin') &&
        !recordArticles[1].children.find(child => child.textContent === 'Use in a new profile').href.includes('whisper-config.json'),
        'known language and speech weights route to their forms without selecting speech metadata');
    const ambiguousSpeech = recordArticles[4].children.find(child => child.textContent === 'Use in a new profile').href;
    check(ambiguousSpeech.includes('acquisition_id=acq-speech-ambiguous') && !ambiguousSpeech.includes('file_name='),
        'multiple speech-weight candidates require an explicit choice in the profile form');
    const ambiguousUse = recordArticles[3].children.find(child => child.textContent === 'Use in a new profile');
    await ambiguousUse.fire('click');
    const languageChoice = recordArticles[3].children.find(child => child.dataset.recordAction === 'language');
    const speechChoice = recordArticles[3].children.find(child => child.dataset.recordAction === 'speech');
    check(!languageChoice.hidden && !speechChoice.hidden &&
        languageChoice.textContent === 'Profiles › Language profiles' &&
        speechChoice.textContent === 'Profiles › Speech profiles',
        'ambiguous file formats require an explicit destination choice');
    speechChoice.focus();
    await downloadPoll();
    recordArticles = elements['acquisition-record-list'].children;
    check(activeElement === recordArticles[3].children.find(child => child.dataset.recordAction === 'speech'),
        'record polling preserves focus on a profile destination choice');
    const focusedUse = recordArticles[0].children.find(child => child.textContent === 'Use in a new profile');
    focusedUse.focus();
    await downloadPoll();
    recordArticles = elements['acquisition-record-list'].children;
    check(activeElement === recordArticles[0].children.find(child => child.textContent === 'Use in a new profile'),
        'record polling preserves focus on the profile handoff');
    let focusedVerify = recordArticles[0].children.find(child => child.textContent === 'Verify full set');
    focusedVerify.focus();
    await downloadPoll();
    recordArticles = elements['acquisition-record-list'].children;
    check(activeElement === recordArticles[0].children.find(child => child.textContent === 'Verify full set'),
        'record polling preserves focus on Verify full set');
    check(elements['adopt-settings-link'].hidden, 'a configured location makes the storage link unnecessary');
    const verifyButton = recordArticles[0].children.find(child => child.textContent === 'Verify full set');
    await verifyButton.fire('click');
    check(calls.some(call => call.action === 'verify-record' && call.acquisition_id === 'acq-language'),
        'Verify full set invokes the record verification action');
    check(elements['adopt-status'].textContent.includes('Record is unusable: model-00002-of-00002.gguf changed'),
        'a completed mismatch is reported as an unusable record');
    elements['adopt-location'].value = 'loc-artifacts';
    await elements['list-artifact-files'].fire('click');
    check(elements['artifact-file-list'].textContent.includes('models/model-00001-of-00002.gguf — 8 bytes'),
        'adoption choices come from the configured artifact location listing');
    let adoptRadio = elements['artifact-file-list'].children[0].children[0];
    adoptRadio.checked = true;
    await adoptRadio.fire('change');
    check(elements['adopt-submit'].disabled &&
        elements['adopt-reason'].textContent.includes('shard set is incomplete'),
        'an incomplete shard set cannot be adopted');
    artifactFilesResponse = {files:[
        {name:'models/model-00001-of-00002.gguf',size:8},
        {name:'models/model-00002-of-00002.gguf',size:9},
    ]};
    await elements['list-artifact-files'].fire('click');
    adoptRadio = elements['artifact-file-list'].children[0].children[0];
    adoptRadio.checked = true;
    await adoptRadio.fire('change');
    check(elements['adopt-reason'].textContent.includes('models/model-00001-of-00002.gguf (8 bytes)') &&
        elements['adopt-reason'].textContent.includes('models/model-00002-of-00002.gguf (9 bytes)') &&
        elements['adopt-reason'].textContent.includes('Location: /artifacts') &&
        elements['adopt-reason'].textContent.includes('Source and revision: unknown'),
        'the adoption review shows the complete shard set, sizes, location and unknown provenance');
    elements['adopt-consent'].checked = true;
    await elements['adopt-consent'].fire('change');
    check(!elements['adopt-submit'].disabled, 'adoption requires explicit consent');
    await elements['adopt-submit'].fire('click');
    check(calls.some(call => call.action === 'adopt' &&
        call.file_name === 'models/model-00001-of-00002.gguf' && call.consent),
        'adoption submits the listed file, not a typed path');
    click(stepButtons[1]); // Every step is directly keyboard-operable.
    click(stepButtons[2]);
    check(!panels[1].hidden && panels[2].hidden, 'later steps cannot bypass resolution');
    elements.repository.value = 'owner/model';
    click(elements['list-files']);
    await new Promise(resolve => setImmediate(resolve));
    const fileChoices = elements['file-list'].children;
    check(fileChoices.length === 2 && fileChoices.every(label => !label.children[0].checked),
        'source files are listed in order and none is pre-selected');
    fileChoices[0].children[0].checked = true;
    fileChoices[0].children[0].fire('change');
    fileChoices[1].children[0].checked = true;
    fileChoices[1].children[0].fire('change');
    click(elements['resolve-files']);
    await new Promise(resolve => setImmediate(resolve));
    check(!panels[2].hidden && elements['review-details'].textContent.includes('0123456789abcdef'),
        'Review displays the full immutable resolved commit');
    check(elements['review-details'].textContent.includes('owner/model via owner') &&
        elements['review-details'].textContent.includes('/models'), 'Review names source and destination');
    check(elements['review-details'].textContent.includes('a'.repeat(64)), 'Review displays published checksums');
    check(!elements['consent-row'].hidden, 'Review exposes consent for a file without a checksum');
    check(pageHtml.includes('I consent to these files being locally hashed'),
        'the consent clearly covers files without publisher checksums');
    elements['no-checksum-consent'].checked = true;
    elements['no-checksum-consent'].fire('change', { target: elements['no-checksum-consent'] });
    click(backButtons[1]);
    click(elements['resolve-files']);
    check(calls.filter(call => call.action === 'resolve').length === 1 &&
        elements['no-checksum-consent'].checked,
        'Back to Select and Continue to Review reuse the unchanged review and consent');
    document.documentElement.dataset.theme = 'dark'; // Appearance switching does not rebuild the page.
    click(nextButtons[1]);
    check(!panels[3].hidden && elements['no-checksum-consent'].checked &&
        fileChoices.every(label => label.children[0].checked), 'Back and Continue retain selection and consent');
    click(elements['start-download']);
    await new Promise(resolve => setImmediate(resolve));
    check(elements['wizard-message'].textContent.includes('admission slot'), 'a refusal stays visible in the wizard');
    check(notices.has('acquisition-work'), 'shared-slot refusal also uses the page-wide notice region');
    check(fileChoices.every(label => label.children[0].checked) && elements['no-checksum-consent'].checked,
        'a refused start keeps the selected files and consent');

    acquireError = 'model storage changed since Review; resolve the request again';
    destination = '/new-models';
    click(elements['start-download']);
    await new Promise(resolve => setImmediate(resolve));
    click(backButtons[2]);
    click(backButtons[1]);
    click(elements['resolve-files']);
    await new Promise(resolve => setImmediate(resolve));
    check(calls.filter(call => call.action === 'resolve').length === 2 &&
        elements['review-details'].textContent.includes('/new-models') && !notices.has('acquisition-storage'),
        'destination refusal allows Back and Continue to refresh the review and clear the stale notice');
    check(fileChoices.every(label => label.children[0].checked) && elements['no-checksum-consent'].checked,
        'destination refresh keeps the file selection and checksum consent');
    click(nextButtons[1]);
    acquireError = 'The confirmed download destination "/new-models" is no longer the saved model storage "/other-models"; review the download again before starting.';
    click(elements['start-download']);
    await new Promise(resolve => setImmediate(resolve));
    click(backButtons[2]);
    click(backButtons[1]);
    click(elements['resolve-files']);
    await new Promise(resolve => setImmediate(resolve));
    check(calls.filter(call => call.action === 'resolve').length === 3 &&
        elements['review-details'].textContent.includes('/new-models') &&
        fileChoices.every(label => label.children[0].checked) && elements['no-checksum-consent'].checked,
        'runner destination refusal also refreshes Review without losing selection or consent');

    click(nextButtons[1]);
    elements.revision.value = 'release-tag';
    elements.revision.fire('input');
    click(elements['start-download']);
    check(elements['wizard-message'].textContent.includes('Resolve and review'),
        'changing revision invalidates the old resolved request');
    click(stepButtons[1]);
    elements.revision.value = 'denied';
    elements.revision.fire('input');
    click(elements['resolve-files']);
    await new Promise(resolve => setImmediate(resolve));
    check(elements['wizard-message'].textContent === 'source refused' &&
        elements.repository.value === 'owner/model' && elements.revision.value === 'denied',
        'a refused resolution keeps the selected source inputs');

    elements.revision.value = 'main';
    elements.revision.fire('input');
    const acquireCountBeforeRace = calls.filter(call => call.action === 'acquire').length;
    click(elements['list-files']);
    await new Promise(resolve => setImmediate(resolve));
    const raceChoice = elements['file-list'].children[0].children[0];
    raceChoice.checked = true;
    raceChoice.fire('change');
    pauseResolve = true;
    click(elements['resolve-files']);
    await new Promise(resolve => setImmediate(resolve));
    check(typeof releaseResolve === 'function', 'resolve request is waiting in the race probe');
    elements.repository.value = 'different/model';
    elements.repository.fire('input');
    pauseResolve = false;
    releaseResolve();
    await new Promise(resolve => setImmediate(resolve));
    check(panels[1].hidden === false && panels[2].hidden &&
        elements['review-details'].textContent === '', 'late resolve cannot restore stale Review');
    click(elements['start-download']);
    await new Promise(resolve => setImmediate(resolve));
    const acquireCalls = calls.filter(call => call.action === 'acquire');
    check(acquireCalls.length === acquireCountBeforeRace,
        'a stale resolution never reaches Acquire');
    check(acquireCalls.length === 3 && acquireCalls[0].destination === '/models' &&
        acquireCalls[1].destination === '/models' && acquireCalls[2].destination === '/new-models',
        'each Acquire transmits the destination displayed in its current Review');

    elements.repository.value = 'owner/model';
    elements.repository.fire('input');
    pauseList = true;
    click(elements['list-files']);
    await new Promise(resolve => setImmediate(resolve));
    check(typeof releaseList === 'function', 'list request is waiting in the race probe');
    elements.repository.value = 'other/model';
    elements.repository.fire('input');
    pauseList = false;
    releaseList();
    await new Promise(resolve => setImmediate(resolve));
    check(elements['file-list'].children.length === 0, 'late List response cannot restore files for old input');

    prepareAnswer = { ready: true, blockers: [], work: null, downloads: [], records: [], artifact_locations: [] };
    await downloadPoll();
    check(elements['adopt-submit'].disabled === true &&
        !elements['adopt-settings-link'].hidden &&
        elements['adopt-settings-link'].href === '#/settings/storage-artifacts' &&
        elements['adopt-reason'].textContent.includes('No artifact location is configured'),
        'without configured artifact locations adoption is unavailable with its Settings link and reason');

    prepareAnswer = { ready: false,
        blockers: [{ text: 'Model storage is not saved.', href: '#/settings/storage-artifacts', label: 'Settings › Storage & artifacts' },
            { text: 'No download origin is configured.', href: '#/settings/origins', label: 'Settings › Download origins' }],
        work: { text: 'The shared slot is held by a build; nothing is queued.' } };
    click(stepButtons[0]);
    await new Promise(resolve => setImmediate(resolve));
    check(notices.has('acquisition-storage') && notices.has('acquisition-origin') && notices.has('acquisition-work'),
        'Prepare prerequisites and current shared work use page-wide notices');
    check(reasons.get(elements['prepare-continue']).includes('Model storage is not saved.'),
        'unavailable Prepare continuation explains its reason beside the control');

    const repositoryBeforePoll = elements.repository.value, revisionBeforePoll = elements.revision.value;
    prepareAnswer = { ready: true, blockers: [], work: null };
    downloadSnapshot = [{ id: 'download-running', purpose: 'owner/model@abc', outcome: '', state: 'running',
        progress: { elapsed: 7, unknown: '' }, phase: 'download', file: 'model.gguf', transferred: 4, total: 8 }];
    await downloadPoll();
    check(!notices.has('acquisition-work') && !notices.has('acquisition-storage') &&
        !notices.has('acquisition-origin') && !reasons.has(elements['prepare-continue']) &&
        elements['prepare-status'].textContent.includes('Nothing running') &&
        elements.repository.value === repositoryBeforePoll && elements.revision.value === revisionBeforePoll,
        'polling clears completed work blockers and restores readiness without losing wizard input');
    check(elements['download-list'].textContent.includes('4 of 8 bytes') &&
        elements['download-list'].textContent.includes('Phase: download'),
        'the current download shows phase, file, bytes and elapsed time');
    const jobLink = elements['download-list'].children[0].children.find(child => child.tagName === 'A');
    jobLink.focus();
    await downloadPoll();
    const refocusedLink = elements['download-list'].children[0].children.find(child => child.tagName === 'A');
    check(activeElement === refocusedLink && refocusedLink !== jobLink,
        'polling preserves focus on the download destination link');
    downloadSnapshot = [{ id: 'download-running', purpose: 'owner/model@abc', outcome: '', state: 'running',
        progress: { elapsed: 8, unknown: '' }, phase: 'verification', file: 'model.gguf', transferred: 6, total: 8 }];
    await downloadPoll();
    check(elements['download-list'].textContent.includes('Phase: verification') &&
        elements['download-list'].textContent.includes('Current file: model.gguf') &&
        elements['download-list'].textContent.includes('6 of 8 bytes'),
        'verification progress reports the file and byte counts actually being checked');
    downloadSnapshot = [{ id: 'download-running', purpose: 'owner/model@abc', outcome: '', state: 'unknown',
        progress: { elapsed: 8, unknown: 'progress is unknown' }, phase: '', file: '', transferred: null, total: null }];
    await downloadPoll();
    check(elements['download-list'].textContent.includes('progress is unknown') &&
        !elements['download-list'].textContent.includes('Phase: download') &&
        !elements['download-list'].textContent.includes('Cancel download…'),
        'unknown progress hides stale phase and withholds Cancel');
    downloadSnapshot = [{ id: 'download-running', purpose: 'owner/model@abc', outcome: 'completed', state: 'completed',
        progress: { elapsed: 9, unknown: '' }, phase: 'verification', file: 'model.gguf', transferred: 8, total: 8 }];
    await downloadPoll();
    check(!elements['download-list'].textContent.includes('Resume') &&
        !elements['download-list'].textContent.includes('Discard partial download…') &&
        !elements['download-list'].textContent.includes('Cancel download…'),
        'completed downloads offer none of the recovery controls');
    downloadSnapshot = [{ id: 'download-running', purpose: 'owner/model@abc', outcome: '', state: 'running',
        progress: { elapsed: 7, unknown: '' }, phase: 'download', file: 'model.gguf', transferred: 4, total: 8 }];
    await downloadPoll();
    const cancel = elements['download-list'].children[0].children.find(child => child.textContent === 'Cancel download…');
    cancel.focus();
    downloadSnapshot[0].progress.elapsed = 9;
    await downloadPoll();
    const refocusedCancel = elements['download-list'].children[0].children.find(child => child.textContent === 'Cancel download…');
    check(activeElement === refocusedCancel && refocusedCancel !== cancel,
        'polling keeps keyboard focus on the refreshed Cancel control');
    refocusedCancel.fire('click');
    check(popups.at(-1).stops[0] === 'The download' && popups.at(-1).starts.length === 0 &&
        popups.at(-1).stays[0].includes('partial files'), 'Cancel previews stopping the job and keeping partials');
    await popups.at(-1).confirmAction();
    await new Promise(resolve => setImmediate(resolve));
    check(elements['download-list'].textContent.includes('Outcome: cancelled'),
        'Cancel refreshes the recorded cancelled or completed outcome');
    const returnedFocus = popups.at(-1).returnFocus();
    returnedFocus.focus();
    check(returnedFocus.tagName === 'H5' && activeElement === returnedFocus,
        'Cancel restores focus to the job heading when its action disappears');

    downloadSnapshot = [{ id: 'download-old', purpose: 'owner/model@abc', outcome: 'interrupted', state: 'interrupted',
        progress: { elapsed: 12, unknown: '' }, phase: 'download', file: 'model.gguf', transferred: 4, total: 8,
        reason: 'Connection interrupted', partials: { job_id: 'download-old', files: [{ name: 'model.gguf', size: 4 }], total_size: 4,
            consequence: 'The next attempt starts again from nothing.' } }];
    await downloadPoll();
    const old = elements['download-list'].children[0];
    check(old.textContent.includes('Outcome: interrupted') && old.textContent.includes('Resume') &&
        old.textContent.includes('Discard partial download…'), 'interrupted downloads with partials offer both recovery actions');
    resumeError = 'The slot is held by another job.';
    await old.children.find(child => child.textContent === 'Resume').fire('click');
    check(old.textContent.includes('Resume refused: The slot is held'),
        'a Resume refusal appears beside the Resume control');
    resumeError = '';
    await old.children.find(child => child.textContent === 'Resume').fire('click');
    check(calls.some(call => call.action === 'resume' && call.job_id === 'download-old'),
        'Resume reaches the pinned-job operation directly');
    downloadSnapshot = [{ id: 'download-old', purpose: 'owner/model@abc', outcome: 'interrupted', state: 'interrupted',
        progress: { elapsed: 12, unknown: '' }, partials: { job_id: 'download-old', files: [{ name: 'model.gguf', size: 4 }], total_size: 4,
            consequence: 'The next attempt starts again from nothing.' } }];
    await downloadPoll();
    const discard = elements['download-list'].children[0].children.find(child => child.textContent === 'Discard partial download…');
    discard.focus();
    await discard.fire('click');
    check(popups.at(-1).title === 'Discard partial download?' && popups.at(-1).content.textContent.includes('8 bytes'),
        'Discard previews only the listed partials and total size');
    await downloadPoll();
    const refocusedDiscard = elements['download-list'].children[0].children.find(child => child.textContent === 'Discard partial download…');
    check(popups.at(-1).returnFocus() === refocusedDiscard,
        'dismissing Discard restores focus to its replacement after polling');
    changedDiscard = true;
    let refusal;
    try { await popups.at(-1).confirmAction(); } catch (error) { refusal = error; }
    check(refusal && popups.at(-1).content.textContent.includes('new.bin'),
        'a changed staging confirmation keeps the popup open with a fresh preview');
    window.location.hash = '#/runs'; window.listeners.hashchange();
    check(pollCleared, 'download polling stops when navigation leaves Model files');
    console.log('PASS: Model files wizard and download progress/recovery');
}

main().catch(error => { console.error('FAIL Model files wizard: ' + (error.stack || error)); process.exit(1) });
