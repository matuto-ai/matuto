#!/usr/bin/env node
// Run the section's own script against element doubles; HTTP/store behavior is covered separately.
'use strict';

const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const AGENTS = path.resolve(__dirname, '../../../agents');
const PYTHON = process.env.PYTHON || 'python3';
const source = execFileSync(PYTHON, ['-B', '-c',
    'from matuto.page.language_profiles import SCRIPT; print(SCRIPT)'],
    { cwd: AGENTS, encoding: 'utf8' });
const headerScript = readFileSync(path.join(AGENTS, 'matuto/static/header_status.js'), 'utf8');
const shellHtml = execFileSync(PYTHON, ['-B', '-c',
    "import sys; from matuto.shell import render; sys.stdout.write(render({}, 'csrf-fixture'))"],
    { cwd: AGENTS, encoding: 'utf8' });
function shellApi(fetch) {
    const shellScript = shellHtml.slice(shellHtml.indexOf('<script>') + 8, shellHtml.lastIndexOf('</script>'));
    const start = shellScript.indexOf('const api={');
    const end = shellScript.indexOf('\n}};', start);
    assert.ok(start >= 0 && end >= 0, 'the rendered shell exposes its real API request helper');
    return new Function('CSRF', 'fetch', shellScript.slice(start, end + 4) + '\nreturn api;')('csrf-fixture', fetch);
}

class Element {
    constructor(tag = 'div') {
        this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {};
        this.listeners = {}; this.value = ''; this.text = ''; this.hidden = false;
        this.className = '';
    }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    get options() { return this.children; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    append(...children) { for (const child of children) { child.parent = this; child.parentNode = this; this.children.push(child); } }
    remove() {
        const parent = this.parentNode || this.parent;
        if (parent) parent.children = parent.children.filter(child => child !== this);
        this.parent = this.parentNode = null;
    }
    querySelector(selector) {
        const className = selector.startsWith('.') ? selector.slice(1) : '';
        return this.children.find(child => child.className.split(/\s+/).includes(className)) || null;
    }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    async fire(name, event = {}) { for (const callback of this.listeners[name] || []) await callback(event); }
    async click() { await this.fire('click'); }
    focus() { this.focused = true; }
    reset() { this.wasReset = true; }
}

async function main() {
    const ids = ['languageProfiles','languageProfileForm','languageProfileList','newLanguageProfile',
        'profileUnavailable','profileError','profileSuccess','profileId','profileName','profileAlias',
        'profileContext','profileThinking','modelPick','mmprojPick','mtpPick','cardPick','profileNotes',
        'languageProfileFormTitle','languageProfileSave','languageProfileCancel'];
    const elements = Object.fromEntries(ids.map(id => [id, new Element(id.endsWith('Pick') ? 'select' : 'div')]));
    const root = elements.languageProfiles;
    const rowGroups = Object.fromEntries(['server','smoke','bench'].map(tool => [tool, new Element()]));
    const addButtons = Object.fromEntries(['server','smoke','bench'].map(tool => [tool, new Element('button')]));
    root.querySelector = selector => {
        const rows = /^\[data-argument-rows="(server|smoke|bench)"\]$/.exec(selector);
        if (rows) return rowGroups[rows[1]];
        const add = /^\[data-add-argument="(server|smoke|bench)"\]$/.exec(selector);
        if (add) return addButtons[add[1]];
        const list = /^\[data-argument-rows="(server|smoke|bench)"\] \.argument-row$/.exec(selector);
        if (list) return rowGroups[list[1]];
        return null;
    };
    root.querySelectorAll = selector => {
        const match = /^\[data-argument-rows="(server|smoke|bench)"\] \.argument-row$/.exec(selector);
        return match ? rowGroups[match[1]].children : [];
    };
    const makeWindow = hash => ({location:{hash},listeners:{},
        addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
        async navigate(next) {
            this.location.hash = next;
            await this.fire('hashchange');
        },
        async fire(name) { for (const callback of this.listeners[name] || []) await callback(); },
        dispatchEvent(event) {
            for (const callback of this.listeners[event.type] || []) {
                const result = callback(event);
                if (result?.then) pending.push(result);
            }
            return true;
        },
        async flush() { while (pending.length) await Promise.all(pending.splice(0)); }});
    const window = makeWindow('#/profiles/language?new&acquisition_id=a&file_name=model.gguf');
    const statusSlot = new Element();
    const noticeRegion = new Element(); noticeRegion.className = 'notices';
    const profilesPage = new Element(); profilesPage.append(noticeRegion);
    const protectedPage = new Element();
    const popupSlot = new Element();
    const document = {
        activeElement: null,
        addEventListener(_name, callback) { this.ready = callback; },
        getElementById(id) { return id === 'page-profiles' ? profilesPage : elements[id] || null; },
        createElement(tag) { const item = new Element(tag); item.parent = null; return item; },
        querySelector(selector) {
            if (selector === '[data-slot="header-status"]') return statusSlot;
            if (selector === '#main') return protectedPage;
            if (selector === '[data-slot="popup"]') return popupSlot;
            return {content:'csrf-fixture'};
        },
    };
    const profileStates = [
        {profile_id:'qualified',name:'Qualified profile',alias:'qualified',published:true,qualification:{state:'qualified'},available:true,
            remove_reason:'Published profile; unpublish before removing.',unpublish_reason:''},
        {profile_id:'changed',name:'Changed profile',alias:'changed',published:false,qualification:{state:'candidate',changed_since_qualification:true},available:true,
            remove_reason:'',unpublish_reason:''},
        {profile_id:'unavailable',name:'Unavailable profile',alias:'missing',published:false,qualification:{state:'candidate'},available:false,
            reason:'model file is missing',remove_reason:'',unpublish_reason:''},
    ];
    const picks = [{name:'Fixture model',file_name:'model.gguf',pick:{acquisition_id:'a',file_name:'model.gguf'}}];
    let postBody, popup;
    let editRecord = {kind:'language',profile_id:'qualified',name:'Qualified profile',alias:'qualified',context_size:4096,
        thinking:'off',card_id:'card-a',notes:'stored notes',model:picks[0].pick,arguments:[]};
    const editCalls = [];
    let editReads = 0;
    let failEditSave = false, editWarnings = [], previewKind = 'served-change';
    let currentCatalogue = {server:[{flag:'--temperature',takes_value:true}],smoke:[],bench:[]};
    window.fetch = async (_path, options = {}) => options.method === 'POST'
        ? {ok:true,status:200,clone() { return {text:async () => { throw new Error('truncated body'); }}; }}
        : {ok:true,status:200,json:async () => ({items:[]})};
    vm.runInNewContext(headerScript.replace('headerStatus.poll();', ''),
        {document,window,AbortController,setTimeout,clearTimeout});
    const realApi = shellApi((...args) => window.fetch(...args));
    const api = {async request(pathname, options = {}) {
        if (pathname === '/api/language-profile-edits') {
            const body = options.body; editCalls.push(body);
            if (body.action === 'preview') return {review_token:'token',
                preview:{kind:previewKind,state:{state:'candidate',changed_since_qualification:true},
                    changed_values:[{label:'Context',old:4096,new:8192}],
                    affected:[{kind:'bundle',name:'Voice bundle',state:{state:'candidate'}}]}};
            if (failEditSave) throw new Error('Save refused because serving started.');
            return {profile_id:body.record.profile_id,warnings:editWarnings};
        }
        if (pathname.startsWith('/api/language-profile-edits?')) {
            editReads++;
            return {profile:editRecord,profiles:profileStates,cards:[{card_id:'card-a',name:'Card A'}],
                picks:{model:picks,mmproj:[],mtp:[]},catalogue:currentCatalogue,prerequisites:[]};
        }
        if (options.method === 'POST') { postBody = options.body; return realApi.request(pathname, options); }
        return {profiles:profileStates,cards:[{card_id:'card-a',name:'Card A'}],
            picks:{model:picks,mmproj:[],mtp:[]},catalogue:currentCatalogue,prerequisites:[]};
    }};
    const pageApi = api.request;
    const described = [], notices = [], cleared = [], pending = [];
    function reason(control, text) {
        if (text) { control.setAttribute('aria-disabled','true'); described.push(text); }
        else control.setAttribute('aria-disabled','false');
    }
    window.matutoPopup = {open(options) { popup = options; }};
    class BrowserEvent { constructor(type) { this.type = type; } }
    const context = {document, window, api, reason, Event: BrowserEvent, URLSearchParams,
        notice: (...args) => notices.push(args), clearNotice: (...args) => cleared.push(args), console};
    const actionsScript = readFileSync(path.join(AGENTS, 'matuto/static/language-profile-actions.js'), 'utf8');
    vm.runInNewContext(actionsScript, {...context, fetch: async (_url, options = {}) => {
        if (!options.body) return {ok:true,json:async () => ({profiles:profileStates})};
        const body = JSON.parse(options.body);
        if (body.action === 'duplicate') profileStates.push({profile_id:body.new_id,name:body.name,alias:body.alias,
            published:false,qualification:{state:'candidate'},available:true,remove_reason:'',unpublish_reason:''});
        if (body.action === 'remove') profileStates.splice(profileStates.findIndex(profile => profile.profile_id === body.profile_id),1);
        if (body.action === 'unpublish') {
            const profile = profileStates.find(item => item.profile_id === body.profile_id);
            profile.published = false; profile.unpublish_reason = ''; profile.remove_reason = '';
        }
        return {ok:true,json:async () => ({profile_id:body.profile_id || body.new_id})};
    }});
    vm.runInNewContext(source, context);
    await document.ready();
    await new Promise(resolve => setImmediate(resolve));
    await window.flush();
    const profileRow = id => elements.languageProfileList.children.find(row => row.getAttribute('data-profile-id') === id);
    const actionRow = id => profileRow(id)?.querySelector('.language-profile-record-actions');
    assert.ok(actionRow('qualified'), 'actions attach to a row in the existing language profile list');
    const qualificationLink = actionRow('changed').children.find(item => item.tagName === 'A');
    assert.equal(qualificationLink?.textContent, 'Qualify…');
    assert.equal(qualificationLink.getAttribute('href'), '#/runs?profile=changed');
    assert.ok(!actionRow('qualified').children.some(item => item.tagName === 'A'));
    assert.ok(!actionRow('unavailable').children.some(item => item.tagName === 'A'));
    assert.equal(elements.languageProfileForm.hidden,false, JSON.stringify(notices));
    assert.equal(elements.profileId.focused,true);
    assert.equal(elements.modelPick.value, JSON.stringify({acquisition_id:'a',file_name:'model.gguf'}));
    assert.equal(elements.profileName.value, '');
    assert.equal(elements.cardPick.value, '');
    assert.match(elements.languageProfileList.textContent, /Qualified/);
    assert.match(elements.languageProfileList.textContent, /Candidate — changed since qualification/);
    assert.match(elements.languageProfileList.textContent, /Unavailable: model file is missing/);

    await elements.newLanguageProfile.fire('click');
    await addButtons.server.fire('click');
    const row = rowGroups.server.children[0];
    const [choice, customFlag] = row.children;
    choice.value = '__custom__'; await choice.fire('change');
    assert.equal(customFlag.hidden, false);
    choice.value = '--temperature'; await choice.fire('change');
    assert.equal(customFlag.hidden, true);
    choice.value = '__custom__'; await choice.fire('change');
    customFlag.value = '--custom-flag'; row.children[2].value = "one value with 'quotes'";
    assert.equal(elements.languageProfileForm.hidden, false);
    elements.profileId.value = 'profile-input'; elements.profileName.value = 'Input stays';
    elements.profileAlias.value = 'input-stays'; elements.profileContext.value = '4096';
    elements.profileThinking.value = 'off'; elements.cardPick.value = 'card-a';
    elements.modelPick.value = JSON.stringify(picks[0].pick);
    await elements.languageProfileForm.fire('submit', {preventDefault() {}});
    assert.equal(postBody.name, 'Input stays');
    assert.equal(JSON.stringify(postBody.arguments), JSON.stringify([
        {tool:'server',option:'--custom-flag',value:"one value with 'quotes'"}]));
    assert.equal(elements.profileName.value, 'Input stays');
    assert.equal(elements.languageProfileForm.hidden, false);
    assert.equal(elements.profileError.textContent, 'Matuto is not answering.');
    assert.equal(elements.profileError.focused, true);
    assert.equal(elements.languageProfileForm.hidden, false);
    assert.equal(elements.profileSuccess.textContent, '');
    assert.match(noticeRegion.textContent, /action was not confirmed/i);

    api.request = async (_path, options = {}) => {
        if (options.method === 'POST') {
            profileStates.push({profile_id:'profile-input',name:'Input stays',alias:'input-stays',published:false,
                qualification:{state:'candidate'},available:true,remove_reason:'',unpublish_reason:''});
            return {warnings:['server argument row 1: --unknown is not advertised by the active build']};
        }
        return {profiles:profileStates,cards:[{card_id:'card-a',name:'Card A'}],
            picks:{model:picks,mmproj:[],mtp:[]},catalogue:currentCatalogue,prerequisites:[]};
    };
    await elements.languageProfileForm.fire('submit', {preventDefault() {}});
    assert.equal(elements.languageProfileForm.hidden, true);
    assert.equal(elements.profileSuccess.textContent, 'Created unpublished profile.');
    assert.equal(rowGroups.server.children.length, 0);
    assert.ok(notices.some(args => args[2] === 'warning' && args[4].includes('--unknown')));
    await window.flush();
    assert.ok(actionRow('profile-input'), 'a profile created by the existing form receives actions without reload');

    await actionRow('profile-input').children[0].click();
    const [copyId, copyName, copyAlias] = popup.content.children.map(field => field.children[0]);
    copyId.value = 'profile-copy'; copyName.value = 'Profile copy'; copyAlias.value = 'profile-copy-alias';
    await popup.confirmAction();
    await window.flush();
    assert.ok(profileRow('profile-copy') && actionRow('profile-copy'),
        'a duplicate appears once in the existing list with actions and no reload');
    await actionRow('profile-copy').children.at(-1).click();
    await popup.confirmAction();
    await window.flush();
    assert.equal(profileRow('profile-copy'), undefined, 'a removed profile leaves the existing list without reload');

    await actionRow('qualified').children[1].click();
    await popup.confirmAction();
    await window.flush();
    assert.match(profileRow('qualified').textContent, /Not published/);
    assert.ok(actionRow('qualified').children.some(button => button.textContent === 'Publish…')
        && !actionRow('qualified').children.some(button => button.textContent === 'Unpublish…'),
        'unpublishing updates status and actions in the existing list without reload');

    api.request = pageApi;
    const editButton = profileRow('qualified').children.find(child => child.textContent === 'Edit');
    await editButton.click();
    assert.equal(elements.profileId.value, 'qualified');
    assert.equal(elements.profileId.readOnly, true);
    assert.equal(elements.profileName.value, 'Qualified profile');
    assert.equal(elements.profileNotes.value, 'stored notes');
    assert.equal(elements.languageProfileFormTitle.textContent, 'Edit Qualified profile');
    assert.equal(elements.languageProfileSave.textContent, 'Save profile');
    elements.profileContext.value = '8192';
    await elements.languageProfileForm.fire('input');
    assert.equal(elements.languageProfileSave.textContent, 'Review…');
    await elements.languageProfileForm.fire('submit', {preventDefault(){}});
    assert.equal(editCalls.at(-1).action, 'preview');
    assert.equal(popup.title, 'Review changes to Qualified profile');
    assert.match(popup.content.textContent, /Context: 4096 → 8192/);
    assert.match(popup.content.textContent, /Qualified profile: candidate — changed since qualification/);
    assert.match(popup.content.textContent, /Bundle Voice bundle: candidate/);
    failEditSave = true;
    await assert.rejects(popup.confirmAction(), /Save refused because serving started/);
    assert.equal(elements.profileContext.value, '8192');
    assert.equal(elements.languageProfileForm.hidden, false);
    failEditSave = false;
    editWarnings = ['confirmed edit launch warning'];
    await popup.confirmAction();
    assert.equal(editCalls.at(-1).action, 'save');
    assert.equal(editCalls.at(-1).confirm, true);
    assert.equal(editCalls.at(-1).review_token, 'token');
    assert.equal(elements.languageProfileForm.hidden, true);
    assert.ok(notices.some(args => args[1] === 'language-profile-save' && args[2] === 'warning'
        && args[4].includes('confirmed edit launch warning')));
    editWarnings = [];

    const editReadsBeforeLink = editReads;
    await window.navigate('#/profiles/language?edit=qualified&context=196608');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(editReads, editReadsBeforeLink + 1,
        'opening an addressed editor fetches the record once instead of re-opening it from populate');
    assert.equal(elements.profileId.value, 'qualified');
    assert.equal(elements.profileContext.value, '196608');
    assert.equal(elements.languageProfileSave.textContent, 'Review…',
        'the context-search link opens the ordinary editor with only the proposed context changed');
    await elements.languageProfileCancel.fire('click');
    await window.navigate('#/profiles/language');

    await profileRow('qualified').children.find(child => child.textContent === 'Edit').click();
    elements.profileName.value = 'Cosmetic rename';
    await elements.languageProfileForm.fire('input');
    assert.equal(elements.languageProfileSave.textContent, 'Save profile');
    const reviewCount = editCalls.filter(call => call.action === 'preview').length;
    await elements.languageProfileForm.fire('submit', {preventDefault(){}});
    assert.equal(editCalls.at(-1).action, 'save');
    assert.equal(editCalls.filter(call => call.action === 'preview').length, reviewCount);
    assert.equal(elements.languageProfileForm.hidden, true);

    editRecord = {...editRecord, arguments:[{tool:'server',option:'--temperature',value:'0.2'}]};
    await profileRow('qualified').children.find(child => child.textContent === 'Edit').click();
    assert.equal(elements.languageProfileSave.textContent, 'Save profile');
    await rowGroups.server.children[0].children[3].click();
    assert.equal(elements.languageProfileSave.textContent, 'Review…',
        'removing a stored server argument updates the save control');
    const existingPopup = popup;
    previewKind = 'cosmetic-only';
    await elements.languageProfileForm.fire('submit', {preventDefault(){}});
    assert.equal(editCalls.at(-2).action, 'preview');
    assert.equal(editCalls.at(-1).action, 'save');
    assert.equal(popup, existingPopup, 'a cosmetic-only server preview does not open a save review');
    assert.equal(elements.languageProfileForm.hidden, true);
    previewKind = 'served-change';
    await elements.languageProfileCancel.fire('click');
    await elements.newLanguageProfile.fire('click');

    await addButtons.server.fire('click');
    const partial = rowGroups.server.children[0];
    assert.equal(partial.children[1].hidden, true);
    partial.children[2].value = 'entered value';
    await elements.languageProfileForm.fire('submit', {preventDefault() {}});
    assert.equal(rowGroups.server.children.length, 1);
    assert.match(elements.profileError.textContent, /server argument row 1 must choose/);
    assert.equal(elements.profileError.focused, true);

    await elements.newLanguageProfile.fire('click');
    await addButtons.server.fire('click');
    assert.equal(rowGroups.server.children.length, 1);
    elements.profileName.value = 'Unfinished draft';
    const draftRow = rowGroups.server.children[0];
    draftRow.children[0].value = '--temperature';
    draftRow.children[2].value = 'draft value';
    currentCatalogue = {server:[{flag:'--slots',takes_value:true}],smoke:[],bench:[]};
    const profileRequest = api.request, noticeCount = notices.length;
    api.request = async () => { throw new Error('store temporarily unavailable'); };
    await window.fire('matuto:argument-catalogue-updated');
    assert.equal(draftRow.children[0].children.map(item => item.value).join(','), ',--temperature,__custom__');
    assert.ok(notices.slice(noticeCount).some(args => args[0] === 'profiles'
        && args[1] === 'argument-catalogue-refresh' && args[2] === 'blocker'
        && args[4] === 'store temporarily unavailable'));
    api.request = profileRequest;
    await window.fire('matuto:argument-catalogue-updated');
    assert.ok(cleared.some(args => args[1] === 'argument-catalogue-refresh'));
    assert.equal(draftRow.children[0].children.map(item => item.value).join(','), ',--slots,__custom__');
    assert.equal(draftRow.children[0].value, '__custom__');
    assert.equal(draftRow.children[1].value, '--temperature');
    assert.equal(draftRow.children[2].value, 'draft value');
    assert.equal(elements.profileName.value, 'Unfinished draft');
    await addButtons.server.fire('click');
    assert.equal(rowGroups.server.children[1].children[0].children.map(item => item.value).join(','), ',--slots,__custom__');
    const noChoices = {async request() { return {profiles:[],cards:[],picks:{model:[],mmproj:[],mtp:[]},catalogue:{},
        prerequisites:[{name:'a selected runtime',href:'#/runtime/install',destination:'Runtime › Install a version'},
            {name:'a card',href:'#/settings/cards',destination:'Settings › Cards'}]}; }};
    const otherWindow = makeWindow('#/profiles/language?new');
    const otherDocument = {...document, getElementById(id) { return elements[id] || null; }};
    // A fresh root keeps the unavailable-control check independent of prior test state.
    const isolated = source;
    vm.runInNewContext(isolated, {document:otherDocument,window:otherWindow,Event:BrowserEvent,URLSearchParams,api:noChoices,reason,
        notice: (...args) => notices.push(args), clearNotice: (...args) => cleared.push(args), console});
    await otherDocument.ready(); await Promise.resolve();
    assert.equal(elements.newLanguageProfile.hidden,true);
    assert.equal(elements.languageProfileForm.hidden,true);
    assert.match(notices.at(-1)[3],/selected runtime and a card/);
    assert.deepEqual(notices.at(-1)[5].map(link => link.href),['#/runtime/install','#/settings/cards']);

    const runtimeOnlyWindow = makeWindow('#/profiles/language?new');
    const runtimeOnlyDocument = {...document};
    runtimeOnlyDocument.addEventListener = (_name, callback) => { runtimeOnlyDocument.ready = callback; };
    vm.runInNewContext(source, {document:runtimeOnlyDocument,window:runtimeOnlyWindow,Event:BrowserEvent,URLSearchParams,
        api:{async request() { return {profiles:[],cards:[{card_id:'card-a',name:'Card A'}],
            picks:{model:picks,mmproj:[],mtp:[]},catalogue:{},
            prerequisites:[{name:'a selected runtime',href:'#/runtime/builds',destination:'Runtime › llama.cpp builds'}]}; }},
        reason, notice: (...args) => notices.push(args), clearNotice: (...args) => cleared.push(args), console});
    await runtimeOnlyDocument.ready(); await Promise.resolve();
    assert.match(notices.at(-1)[3],/selected runtime/);
    assert.equal(notices.at(-1)[5].length,1);
    assert.equal(notices.at(-1)[5][0].label,'Runtime › llama.cpp builds');

    const cardOnlyWindow = makeWindow('#/profiles/language?new');
    const cardOnlyDocument = {...document};
    cardOnlyDocument.addEventListener = (_name, callback) => { cardOnlyDocument.ready = callback; };
    vm.runInNewContext(source, {document:cardOnlyDocument,window:cardOnlyWindow,Event:BrowserEvent,URLSearchParams,
        api:{async request() { return {profiles:[],cards:[],picks:{model:picks,mmproj:[],mtp:[]},catalogue:{},
            prerequisites:[{name:'a card',href:'#/settings/cards',destination:'Settings › Cards'}]}; }},
        reason, notice: (...args) => notices.push(args), clearNotice: (...args) => cleared.push(args), console});
    await cardOnlyDocument.ready(); await Promise.resolve();
    assert.match(notices.at(-1)[3],/a card/);
    assert.equal(notices.at(-1)[5][0].label,'Settings › Cards');

    const failedWindow = makeWindow('#/profiles/language?new');
    const failedDocument = {...document};
    failedDocument.addEventListener = (_name, callback) => { failedDocument.ready = callback; };
    vm.runInNewContext(source, {document:failedDocument,window:failedWindow,Event:BrowserEvent,URLSearchParams,
        api:{async request() { throw new Error('store unavailable'); }}, reason,
        notice: (...args) => notices.push(args), clearNotice: (...args) => cleared.push(args), console});
    await failedDocument.ready(); await Promise.resolve();
    assert.ok(notices.some(args => args[2] === 'blocker' && args[3] === 'Language profiles are unavailable.'));
    assert.equal(elements.newLanguageProfile.hidden,true);
    assert.equal(elements.languageProfileForm.hidden,true);

    const readyData = {profiles:[],cards:[{card_id:'card-a',name:'Card A'}],
        picks:{model:picks,mmproj:[],mtp:[]},catalogue:{},prerequisites:[]};
    const blockedData = {...readyData,cards:[],prerequisites:[
        {name:'a selected runtime',href:'#/runtime/install',destination:'Runtime › Install a version'},
        {name:'a card',href:'#/settings/cards',destination:'Settings › Cards'}]};
    const defaultWindow = makeWindow('#/overview');
    const defaultDocument = {...document};
    defaultDocument.addEventListener = (_name, callback) => { defaultDocument.ready = callback; };
    let defaultReads = 0;
    vm.runInNewContext(source, {document:defaultDocument,window:defaultWindow,Event:BrowserEvent,URLSearchParams,
        api:{async request() { defaultReads++; return readyData; }},reason,
        notice: (...args) => notices.push(args),clearNotice: (...args) => cleared.push(args),console});
    await defaultDocument.ready();
    assert.equal(defaultReads,0,'non-Profiles routes must not read language profiles');
    await defaultWindow.navigate('#profiles');
    assert.equal(defaultReads,1,'the page-list Profiles link opens the default Language profiles section');
    assert.equal(elements.newLanguageProfile.hidden,false);
    await elements.newLanguageProfile.fire('click');
    assert.equal(elements.languageProfileForm.hidden,false);

    let routeData = readyData, routeReads = 0;
    api.request = async () => { routeReads++; return routeData; };
    await window.navigate('#/model-files/acquisition');
    await window.navigate('#/profiles/language?new');
    assert.equal(elements.languageProfileForm.hidden,false);
    assert.equal(routeReads,1,'a same-document handoff must read prerequisites on entry');

    routeData = blockedData;
    await window.navigate('#/runtime/builds');
    await window.navigate('#/profiles/language?new');
    assert.equal(elements.languageProfileForm.hidden,true);
    assert.match(notices.at(-1)[3],/selected runtime and a card/);
    routeData = readyData;
    await window.navigate('#/runtime/builds');
    await window.navigate('#/profiles/language?new');
    assert.equal(elements.languageProfileForm.hidden,false);
    assert.ok(cleared.some(args => args[1] === 'language-profile-prerequisites'),
        'returning after setup must clear the stale blocker');

    const pendingElements = Object.fromEntries(ids.map(id => [id, new Element(id.endsWith('Pick') ? 'select' : 'div')]));
    const pendingRoot = pendingElements.languageProfiles;
    pendingRoot.querySelector = root.querySelector;
    pendingRoot.querySelectorAll = root.querySelectorAll;
    const pendingDocument = {activeElement:null,addEventListener(_name, callback) { this.ready = callback; },
        getElementById(id) { return pendingElements[id] || null; },
        createElement(tag) { return new Element(tag); }};
    const pendingWindow = makeWindow('#/model-files/acquisition');
    pendingElements.languageProfileForm.hidden = false;
    let releaseRead;
    vm.runInNewContext(source, {document:pendingDocument,window:pendingWindow,Event:BrowserEvent,URLSearchParams,
        api:{request() { return new Promise(resolve => { releaseRead = resolve; }); }},reason,
        notice: (...args) => notices.push(args),clearNotice: (...args) => cleared.push(args),console});
    await pendingDocument.ready();
    assert.equal(pendingElements.languageProfileForm.hidden,true,
        'leaving Profiles must close the form before setup navigation');
    const pendingEntry = pendingWindow.navigate('#/profiles/language?new');
    await Promise.resolve();
    await pendingElements.newLanguageProfile.fire('click');
    assert.equal(pendingElements.languageProfileForm.hidden,true,
        'New must not reveal fields while prerequisite data is pending');
    releaseRead(blockedData);
    await pendingEntry;
    assert.equal(pendingElements.languageProfileForm.hidden,true);

    api.request = async () => ({profiles:profileStates,cards:[{card_id:'card-a',name:'Card A'}],
        picks:{model:picks,mmproj:[],mtp:[]},catalogue:currentCatalogue,prerequisites:[]});
    await window.navigate('#/overview');
    await window.navigate('#/profiles/language');
}

main().then(() => console.log('PASS: language profile page labels, arguments and unconfirmed-save state'))
    .catch(error => { console.error(error.stack || error); process.exit(1); });
