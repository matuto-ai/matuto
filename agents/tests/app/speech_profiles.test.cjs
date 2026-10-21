#!/usr/bin/env node
// Exercise the Speech profiles page script at its DOM and shared-popup boundaries.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.attributes = {};
    this.dataset = {}; this.hidden = false; this.disabled = false; this.readOnly = false;
    this.value = ''; this.text = '';
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.text = ''; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  async fire(name, event = {}) { for (const callback of this.listeners[name] || []) await callback(event); }
  focus() { this.focused = true; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
}

async function main() {
  const ids = ['speechProfiles','speechProfileNew','speechProfileList','speechProfileError',
    'speechProfileForm','speechProfileFormTitle','speechProfileKind','speechProfileId',
    'speechProfileName','speechProfileExecutable','speechProfileExecutableHelp','speechProfileWeights',
    'speechProfileWeightsHelp',
    'speechProfileCardLabel','speechProfileCard','speechProfileArguments','speechProfileAddArgument',
    'speechProfileNotes','speechProfileSave','speechProfileCancel'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id.includes('Kind') ||
    ['speechProfileExecutable','speechProfileWeights','speechProfileCard'].includes(id) ? 'select' : 'div')]));
  const document = {head:new Element('head'), getElementById: id => elements[id] || null,
    querySelector: () => ({content:'csrf-fixture'}), createElement: tag => new Element(tag),
    createTextNode: text => ({textContent:text,children:[]})};
  const synthesis = {id:'s-1', kind:'synthesis', name:'Breeze profile', speech_executable_id:'b-1',
    weights:{acquisition_id:'a-1',file_name:'breeze.gguf'}, weights_name:'breeze.gguf', card_id:'card-1', card:'GPU',
    executable:'Breeze-TTS-2.cpp — vulkan — abc', bundles:[{id:'bundle-1',name:'Voice',
      state:{state:'qualified'}}], arguments:[
        {tool:'server',option:'--prefix',value:''},
        {tool:'smoke',option:'--help',value:null},
        {tool:'bench',catalogue:'--iterations',value:'2'}], notes:'old', available:true};
  const recognition = {id:'r-1', kind:'recognition', name:'Whisper profile', speech_executable_id:'w-1',
    weights:{acquisition_id:'a-2',file_name:'whisper.bin'}, weights_name:'whisper.bin', card_id:null, card:null, executable:'whisper.cpp — cpu — def',
    bundles:[], arguments:[], notes:'', available:true};
  const data = {profiles:[synthesis, recognition],
    executables:[{id:'b-1',kind:'synthesis',project:'Breeze-TTS-2.cpp',backend:'vulkan',commit:'abc'},
      {id:'b-2',kind:'synthesis',project:'Breeze-TTS-2.cpp',backend:'vulkan',commit:'def'},
      {id:'w-1',kind:'recognition',project:'whisper.cpp',backend:'cpu',commit:'ghi'}],
    weights:[{name:'Breeze',file_name:'breeze.gguf',pick:synthesis.weights},
      {name:'Whisper config',file_name:'whisper-config.json',pick:{acquisition_id:'a-2',file_name:'whisper-config.json'}},
      {name:'Whisper',file_name:'whisper.bin',pick:recognition.weights},
      {name:'Voice A',file_name:'voice-a.bin',pick:{acquisition_id:'a-3',file_name:'voice-a.bin'}},
      {name:'Voice B',file_name:'voice-b.safetensors',pick:{acquisition_id:'a-3',file_name:'voice-b.safetensors'}}],
    cards:[{card_id:'card-1',name:'GPU'}]};
  elements.speechProfileCard.required = true;
  const calls = [], popups = [], windowListeners = {};
  let releaseInitialGet, releaseNewGet, pauseNewGet = false;
  elements.speechProfileForm.hidden = true;
  global.document = document;
  global.window = {location:{hash:'#/model-files'}, addEventListener:(name,callback) => { windowListeners[name] = callback; },
    matutoPopup:{open:options => popups.push(options)}};
  global.fetch = async (_url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    if (body) calls.push(body);
    else if (pauseNewGet) {
      pauseNewGet = false;
      await new Promise(resolve => { releaseNewGet = resolve; });
    } else if (!releaseInitialGet) await new Promise(resolve => { releaseInitialGet = resolve; });
    let value = data;
    if (body?.action === 'preview') value = {preview:{kind:'served-change',
      changed_values:[{label:'Speech executable',old:'b-1',new:'b-2'}],state:null,
      affected:[{kind:'bundle',id:'bundle-1',name:'Voice',state:{state:'candidate',changed_since_qualification:true}}]},
      availability:{available:true},review_token:'review-token'};
    if (body?.action === 'save') value = {profile:{...synthesis,speech_executable_id:body.record.speech_executable_id}};
    if (body?.action === 'remove') value = {removed:body.profile_id};
    return {ok:true,status:200,json:async()=>value};
  };
  const source = fs.readFileSync(path.resolve(__dirname, '../../matuto/static/speech_profiles.js'), 'utf8');
  vm.runInThisContext(source, {filename:'speech_profiles.js'});
  assert.equal(typeof releaseInitialGet, 'function', 'the initial inactive-page GET is held');
  global.window.location.hash = '#/profiles/speech-profiles?new&acquisition_id=a-2&file_name=whisper.bin';
  windowListeners.hashchange();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(elements.speechProfileList.textContent, /Synthesis/);
  assert.match(elements.speechProfileList.textContent, /Recognition/);
  assert.doesNotMatch(elements.speechProfileList.textContent, /Qualification|Candidate/);
  assert.match(elements.speechProfileList.textContent, /Breeze-TTS-2.cpp — vulkan/);
  assert.equal(elements.speechProfileForm.hidden, false, 'hash navigation opens the speech form');
  assert.equal(elements.speechProfileWeights.value,
    JSON.stringify({acquisition_id:'a-2',file_name:'whisper.bin'}));
  assert.equal(elements.speechProfileName.value, '');
  assert.equal(elements.speechProfileId.value, '');
  assert.equal(elements.speechProfileExecutable.value, '');
  elements.speechProfileKind.value = 'recognition';
  await elements.speechProfileKind.fire('change');
  assert.equal(elements.speechProfileWeights.value,
    JSON.stringify({acquisition_id:'a-2',file_name:'whisper.bin'}));
  elements.speechProfileName.value = 'Operator-entered name';
  releaseInitialGet();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.speechProfileName.value, 'Operator-entered name',
    'a superseded inactive-page response cannot clear the active handoff form');

  global.window.location.hash = '#/profiles/speech-profiles?new&acquisition_id=a-3';
  windowListeners.hashchange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.speechProfileWeights.value, '', 'ambiguous weights require operator selection');
  assert.match(elements.speechProfileWeightsHelp.textContent, /Choose the weights file/);
  assert.deepEqual(elements.speechProfileWeights.children.map(option => option.value), ['',
    JSON.stringify({acquisition_id:'a-3',file_name:'voice-a.bin'}),
    JSON.stringify({acquisition_id:'a-3',file_name:'voice-b.safetensors'})]);

  await elements.speechProfileNew.fire('click');
  elements.speechProfileKind.value = 'recognition';
  await elements.speechProfileKind.fire('change');
  assert.equal(elements.speechProfileCardLabel.hidden, true);
  assert.deepEqual(elements.speechProfileExecutable.children.map(option => option.value), ['', 'w-1']);
  assert.equal(elements.speechProfileExecutableHelp.textContent, '');
  elements.speechProfileId.value = 'new-r'; elements.speechProfileName.value = 'New whisper';
  elements.speechProfileExecutable.value = 'w-1';
  elements.speechProfileWeights.value = JSON.stringify(recognition.weights);
  await elements.speechProfileForm.fire('submit', {preventDefault(){}});
  assert.equal(calls.at(-1).record.kind, 'recognition');
  assert.equal('card_id' in calls.at(-1).record, false);

  await elements.speechProfileList.children[1].children.at(-1).children[0].fire('click');
  assert.equal(elements.speechProfileCardLabel.hidden, true);
  assert.equal(elements.speechProfileCard.disabled, true);
  elements.speechProfileName.value = 'Renamed whisper';
  await elements.speechProfileForm.fire('submit', {preventDefault(){}});
  assert.equal(calls.at(-1).action, 'save');
  assert.equal(calls.at(-1).record.kind, 'recognition');
  assert.equal('card_id' in calls.at(-1).record, false);

  const executables = data.executables;
  data.executables = executables.filter(item => item.kind === 'recognition');
  await elements.speechProfileNew.fire('click');
  assert.match(elements.speechProfileExecutableHelp.textContent, /No installed synthesis speech executable/);
  assert.equal(elements.speechProfileExecutableHelp.children[1].href, '#/runtime/speech-runtimes');
  data.executables = executables;

  await elements.speechProfileList.children[0].children.at(-1).children[0].fire('click');
  assert.equal(elements.speechProfileSave.textContent, 'Save profile');
  elements.speechProfileName.value = 'Renamed Breeze';
  await elements.speechProfileForm.fire('input');
  assert.equal(elements.speechProfileSave.textContent, 'Save profile');
  await elements.speechProfileForm.fire('submit', {preventDefault(){}});
  assert.equal(calls.at(-1).action, 'save');
  assert.deepEqual(calls.at(-1).record.arguments, synthesis.arguments);
  await elements.speechProfileList.children[0].children.at(-1).children[0].fire('click');
  elements.speechProfileExecutable.value = 'b-2';
  await elements.speechProfileForm.fire('change');
  assert.equal(elements.speechProfileSave.textContent, 'Review…');
  await elements.speechProfileForm.fire('submit', {preventDefault(){}});
  assert.equal(calls.at(-1).action, 'preview');
  assert.equal(popups.length, 1);
  assert.equal(popups[0].safe, 'Cancel');
  assert.match(popups[0].title, /Breeze profile/);
  assert.match(popups[0].content.textContent, /Speech executable: b-1 → b-2/);
  assert.match(popups[0].content.textContent, /Bundle Voice: candidate — changed since qualification/);
  assert.doesNotMatch(popups[0].content.textContent, /Speech profile state/);
  await popups[0].confirmAction();
  assert.equal(calls.at(-1).action, 'save');
  assert.equal(calls.at(-1).confirm, true);
  assert.equal(calls.at(-1).review_token, 'review-token');

  const installedExecutables = data.executables, installedWeights = data.weights, installedCards = data.cards;
  data.executables = []; data.weights = []; data.cards = [];
  pauseNewGet = true;
  const opening = elements.speechProfileNew.fire('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(typeof releaseNewGet, 'function', 'New waits for its catalogue read');
  data.executables = installedExecutables; data.weights = installedWeights; data.cards = installedCards;
  releaseNewGet();
  await opening;
  assert.deepEqual(elements.speechProfileExecutable.children.map(option => option.value), ['', 'b-1', 'b-2']);
  assert.ok(elements.speechProfileWeights.children.some(option => option.value ===
    JSON.stringify({acquisition_id:'a-2',file_name:'whisper.bin'})));
  console.log('PASS: speech profiles filter picks, omit recognition cards, and review served changes');
}

main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
