"""The Model files acquisition wizard (ACQ-2)."""

import json

PAGE = "model-files"
NAME = "acquisition"
TITLE = "Get model files"
ORDER = 1


def html(status):
    csrf = json.dumps((status or {}).get("csrf", "")).replace("<", "\\u003c")
    return f'''<div class="acquisition-wizard" id="acquisitionWizard">
<nav aria-label="Download steps" class="wizard-steps">
<button type="button" data-step="prepare" aria-current="step">Prepare</button>
<button type="button" data-step="select">Select files</button>
<button type="button" data-step="review">Review</button>
<button type="button" data-step="acquire">Acquire</button>
</nav>
<div id="wizard-message" role="status" aria-live="polite"></div>
<section data-wizard-step="prepare"><h4>Prepare</h4><div id="prepare-status"></div>
<button type="button" id="prepare-continue" data-next="select">Continue to Select files</button></section>
<section data-wizard-step="select" hidden><h4>Select files</h4>
<label>Hugging Face repository <input id="repository" autocomplete="off"></label>
<label>Branch, tag or commit <input id="revision" value="main" autocomplete="off"></label>
<button type="button" id="list-files">List files</button><div id="file-list"></div>
<button type="button" data-back="prepare">Back to Prepare</button>
<button type="button" id="resolve-files">Continue to Review</button></section>
<section data-wizard-step="review" hidden><h4>Review</h4><div id="review-details"></div>
<label id="consent-row" hidden><input id="no-checksum-consent" type="checkbox"> I consent to these files being locally hashed; no published checksum is available for them.</label>
<button type="button" data-back="select">Back to Select files</button>
<button type="button" data-next="acquire">Continue to Acquire</button></section>
<section data-wizard-step="acquire" hidden><h4>Acquire</h4><div id="acquire-status"></div>
<button type="button" data-back="review">Back to Review</button><button type="button" id="start-download">Start download</button></section>
</div>
<section id="model-records" aria-labelledby="model-records-title">
<h4 id="model-records-title">Acquisition records</h4><div id="acquisition-record-list"><p>Loading records…</p></div>
</section>
<section id="adopt-model-files" aria-labelledby="adopt-model-files-title">
<h4 id="adopt-model-files-title">Adopt files already on disk</h4>
<label>Artifact location <select id="adopt-location"><option value="">Choose a configured artifact location</option></select></label>
<button type="button" id="list-artifact-files">List files</button><div id="artifact-file-list"></div>
<label>Record name <input id="adopt-name"></label>
<label><input type="checkbox" id="adopt-consent"> I consent to “locally hashed; publisher unverified”. Source and revision will remain unknown.</label>
<button type="button" id="adopt-submit" disabled>Adopt selected file</button>
<p id="adopt-reason" class="reason"></p><a id="adopt-settings-link" href="#/settings/storage-artifacts" hidden>Settings › Storage & artifacts</a>
<p id="adopt-status" role="status"></p>
</section>
<section id="download-jobs" aria-labelledby="download-jobs-title">
<h4 id="download-jobs-title">Downloads</h4><div id="download-list"><p>Loading downloads…</p></div>
</section>
<style nonce="{(status or {}).get('csrf', '')}">
.wizard-steps{{display:flex;flex-wrap:wrap;gap:.35rem;margin:0 0 1rem}}
.wizard-steps button[aria-current=step]{{border-color:var(--accent);color:var(--accent);font-weight:600}}
.acquisition-wizard section{{margin:0 0 1rem}}
.acquisition-wizard label{{display:block;margin:.7rem 0}}
.acquisition-wizard input[type=text],.acquisition-wizard input:not([type]){{display:block;width:min(100%,32rem);min-height:44px;padding:.5rem;border:1px solid var(--line);border-radius:.4rem;background:var(--panel);color:var(--matuto-text)}}
.acquisition-wizard li,#review-details{{overflow-wrap:anywhere}}
#download-list article{{border-top:1px solid var(--line);padding:.7rem 0;overflow-wrap:anywhere}}
#download-list article p{{margin:.35rem 0}}
#download-list article a{{display:block;overflow-wrap:anywhere}}
#download-list article button{{display:block;margin:.35rem 0}}
#acquisition-record-list article{{border-top:1px solid var(--line);padding:.8rem 0;overflow-wrap:anywhere}}
#acquisition-record-list ul{{padding-left:1.3rem}}
#acquisition-record-list button{{margin:.25rem .35rem .25rem 0}}
#adopt-model-files label{{display:block;margin:.7rem 0;overflow-wrap:anywhere}}
#adopt-location{{display:block;width:100%;max-width:100%;min-height:44px}}
#adopt-name{{display:block;width:min(100%,32rem);min-height:44px}}
#artifact-file-list label{{margin:.25rem 0}}
</style>
<script nonce="{(status or {}).get('csrf', '')}">
'use strict';
(()=>{{
const csrf={csrf}, root=document.getElementById('acquisitionWizard'), actionRefusals=new Map();
if(!root)return;
document.addEventListener('DOMContentLoaded',()=>{{
const $=id=>document.getElementById(id), state={{step:'prepare',repository:'',revision:'main',resolution:null,reviewKey:'',files:[],selected:new Set(),ready:false,generation:0,artifactFiles:[],selectedArtifact:'',profileChoices:new Set()}};
const api=async(action,body={{}})=>{{
 const response=await fetch('/api/acquisition',{{method:body.method||'POST',cache:'no-store',headers:body.method==='GET'?{{}}:{{'Content-Type':'application/json','X-CSRF-Token':csrf}},body:body.method==='GET'?undefined:JSON.stringify({{action,...body}})}});
 let value={{}};try{{value=await response.json()}}catch(error){{}}
 if(!response.ok)throw Object.assign(new Error(value.error||'The request was refused.'),value);return value;
}};
function message(text){{$('wizard-message').textContent=text||''}}
function sourceKey(){{return JSON.stringify([$('repository').value.trim(),$('revision').value.trim(),[...state.selected]])}}
function stepReason(step){{
 if(step==='select'&&!state.ready)return [...(state.blockers||[]).map(item=>item.text),state.work&&state.work.text].filter(Boolean).join(' ')||'Complete the named prerequisites first.';
 if((step==='review'||step==='acquire')&&!state.resolution)return 'List and select files, then continue to Review.';
 return '';
}}
function refreshReasons(){{
 for(const button of root.querySelectorAll('[data-step]'))reason(button,stepReason(button.dataset.step));
 const blockers=state.blockers||[];
 const messageText=[...blockers.map(item=>item.text),state.work&&state.work.text].filter(Boolean).join(' ');
 reason($('prepare-continue'),state.ready?'':messageText||'Complete the named prerequisites first.');
 reason($('resolve-files'),state.selected.size?'':'List source files and choose at least one file.');
 reason($('start-download'),!state.resolution?'Review the exact request before starting.':
   state.resolution.files.some(file=>!file.sha256)&&!$('no-checksum-consent').checked?
   'Consent to locally hashing these files is required before starting.':'' );
}}
function go(step){{
 state.step=step;
 for(const panel of root.querySelectorAll('[data-wizard-step]'))panel.hidden=panel.dataset.wizardStep!==step;
 for(const button of root.querySelectorAll('[data-step]')){{if(button.dataset.step===step)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current')}}
}}
function navigate(step,control){{
 const returningToReview=step==='review'&&control&&control.dataset.back==='review';
 const blocked=returningToReview?'':stepReason(step);
 if(blocked){{message(blocked);if(control)reason(control,blocked);return}}
 if(control)reason(control,'');message('');go(step);
 if(step==='prepare')prepare();
}}
function invalidate(){{
 state.generation++;state.resolution=null;state.reviewKey='';state.files=[];state.selected.clear();
 $('file-list').textContent='';$('review-details').textContent='';$('no-checksum-consent').checked=false;refreshReasons();
}}
function destinationChanged(error){{
 const text=error&&error.message?error.message:String(error);
 return /model storage changed since review|confirmed download destination .* is no longer the saved model storage/i.test(text);
}}
function reportError(error){{
 const text=error&&error.message?error.message:String(error);message(text);
 if(/storage|download origin|admission slot|bytes short|space/i.test(text)){{
   const key=/storage/i.test(text)?'storage':/origin/i.test(text)?'origin':'work';
   const link=key==='storage'?{{href:'#/settings/storage-artifacts',label:'Settings › Storage & artifacts'}}:
     key==='origin'?{{href:'#/settings/origins',label:'Settings › Download origins'}}:
     {{href:'#runs',label:'Runs & jobs'}};
   notice('model-files','acquisition-'+key,'blocker',text,'',link);
 }}
}}
function discardPopup(preview){{
 const content=document.createElement('div');
 const fill=()=>{{content.textContent='';for(const file of preview.files){{const row=document.createElement('p');row.textContent=`${{file.name}} — ${{file.size}} bytes`;content.append(row)}}
 const consequence=document.createElement('p');consequence.textContent=`Total: ${{preview.total_size}} bytes. ${{preview.consequence}}`;content.append(consequence)}};
 fill();
 window.matutoPopup.open({{title:'Discard partial download?',content,safe:'Keep partial files',confirm:'Discard partial download',returnFocus:()=>downloadControl(preview.job_id,'discard')||downloadHeading(preview.job_id),confirmAction:async()=>{{
   try{{await api('discard',{{job_id:preview.job_id,preview}});refreshDownloads()}}
   catch(error){{if(error.preview){{preview=error.preview;fill()}}throw error}}
 }}}});
}}
function downloadControl(id,action){{
 return [...$('download-list').querySelectorAll('a,button')].find(control=>control.dataset.downloadId===id&&control.dataset.downloadAction===action)||null;
}}
function downloadHeading(id){{
 return [...$('download-list').querySelectorAll('h5')].find(heading=>heading.dataset.downloadId===id)||null;
}}
function updateReadiness(data){{
 state.ready=data.ready;state.blockers=data.blockers||[];state.work=data.work;
 for(const key of ['storage','origin','work','load'])clearNotice('model-files','acquisition-'+key);
 for(const item of state.blockers){{const key=item.href.includes('origins')?'origin':'storage';notice('model-files','acquisition-'+key,'blocker',item.text,'',{{href:item.href,label:item.label}})}}
 if(data.work)notice('model-files','acquisition-work','blocker',data.work.text,'Nothing is queued; finish or cancel this work.',{{href:'#runs',label:'Runs & jobs'}});
 $('prepare-status').textContent=data.work?'':'Nothing running.';
 refreshReasons();
}}
function profileHref(record,speech){{
 let file;
 if(speech){{
  const sidecar=/(?:mmproj|mtp|metadata|config|tokenizer|processor|preprocessor|vocab|merges|index|manifest)/i;
  const weights=record.files.filter(item=>/\\.(bin|safetensors|gguf)$/i.test(item.file_name)&&!sidecar.test(item.file_name));
  const named=weights.filter(item=>/(?:^|[-_.])(?:breeze|whisper|speech|voice|tts)(?:[-_.]|$)/i.test(item.file_name));
  file=weights.length===1?weights[0]:named.length===1?named[0]:null;
 }}else file=record.files.find(item=>/\\.gguf$/i.test(item.file_name)&&!/(mmproj|mtp)/i.test(item.file_name))||record.files[0];
 const route=speech?'#/profiles/speech-profiles':'#/profiles/language';
 const pick=file?`&file_name=${{encodeURIComponent(file.file_name)}}`:'';
 return `${{route}}?new&acquisition_id=${{encodeURIComponent(record.acquisition_id)}}${{pick}}`;
}}
function renderRecords(records){{
 const box=$('acquisition-record-list'),focused=document.activeElement;
 const focusRecord=focused?.dataset?.recordId,focusAction=focused?.dataset?.recordAction;
 box.textContent='';
 if(!records.length){{box.textContent='No acquisition records yet.';return}}
 for(const record of records){{
  const article=document.createElement('article'), heading=document.createElement('h5');heading.textContent=record.name;article.append(heading);
  for(const text of [`Source: ${{record.source}}`, `Revision: ${{record.revision}}`, `Provenance: ${{record.label}}`, `Last verified: ${{record.verified_at||'Never'}}`,
   record.complete?'Complete and usable':`Unusable: ${{record.reason}}`]){{const p=document.createElement('p');p.textContent=text;article.append(p)}}
  const files=document.createElement('ul');
  for(const file of record.files){{const li=document.createElement('li');li.textContent=`${{file.file_name}} — ${{file.size_bytes}} bytes; published checksum: ${{file.published_sha256||'none'}}; SHA-256: ${{file.computed_sha256||'not recorded'}}${{file.reason?' — '+file.reason:''}}`;files.append(li)}}
  article.append(files);
  const verify=document.createElement('button');verify.type='button';verify.textContent='Verify full set';verify.dataset.recordId=record.acquisition_id;verify.dataset.recordAction='verify';
  verify.addEventListener('click',async()=>{{verify.disabled=true;try{{const result=await api('verify-record',{{acquisition_id:record.acquisition_id,consent:true}});$('adopt-status').textContent=result.complete?'Full set verified.':`Record is unusable: ${{result.reason}}`;await refreshDownloads()}}catch(error){{$('adopt-status').textContent=`Nothing new was recorded: ${{error.message}}`}}finally{{verify.disabled=false}}}});article.append(verify);
  if(record.complete){{
   if(record.profile_kind==='unknown'){{
    const use=document.createElement('button');use.type='button';use.textContent='Use in a new profile';use.dataset.recordId=record.acquisition_id;use.dataset.recordAction='use';
    const expanded=state.profileChoices.has(record.acquisition_id), prompt=document.createElement('p');
    prompt.textContent='Choose a profile destination';prompt.hidden=!expanded;
    use.setAttribute('aria-expanded',String(expanded));use.addEventListener('click',()=>{{state.profileChoices.add(record.acquisition_id);prompt.hidden=false;language.hidden=false;speech.hidden=false;use.setAttribute('aria-expanded','true')}});
    const language=document.createElement('a');language.textContent='Profiles › Language profiles';language.href=profileHref(record,false);language.hidden=!expanded;language.dataset.recordId=record.acquisition_id;language.dataset.recordAction='language';
    const speech=document.createElement('a');speech.textContent='Profiles › Speech profiles';speech.href=profileHref(record,true);speech.hidden=!expanded;speech.dataset.recordId=record.acquisition_id;speech.dataset.recordAction='speech';
    article.append(use,prompt,language,speech);
   }}else{{
    const use=document.createElement('a');use.textContent='Use in a new profile';use.href=profileHref(record,record.profile_kind==='speech');use.dataset.recordId=record.acquisition_id;use.dataset.recordAction='use';article.append(use);
   }}
  }}
  box.append(article);
 }}
 if(focusRecord&&focusAction){{
  const replacement=Array.from(box.children).flatMap(article=>Array.from(article.children)).find(control=>
   control.dataset?.recordId===focusRecord&&control.dataset?.recordAction===focusAction);
  replacement?.focus();
 }}
}}
function selectedFileSet(){{
 const selected=state.artifactFiles.find(file=>file.name===state.selectedArtifact);if(!selected)return {{files:[],complete:false}};
 const parent=selected.name.includes('/')?selected.name.slice(0,selected.name.lastIndexOf('/')+1):'';
 const match=selected.name.slice(parent.length).match(/^(.+)-([0-9]+)-of-([0-9]+)\\.gguf$/i);
 if(!match)return {{files:[selected],complete:true}};
 const index=Number(match[2]),count=Number(match[3]);
 if(!Number.isSafeInteger(index)||!Number.isSafeInteger(count)||count<1||index<1||index>count||count>state.artifactFiles.length)return {{files:[selected],complete:false}};
 const shards=state.artifactFiles.filter(file=>{{if(!file.name.startsWith(parent))return false;const shard=file.name.slice(parent.length).match(/^(.+)-([0-9]+)-of-([0-9]+)\\.gguf$/i);return shard&&shard[1]===match[1]&&Number(shard[3])===count}});
 const indices=new Set(shards.map(file=>Number(file.name.slice(parent.length).match(/^.+-([0-9]+)-of-/i)[1])));
 return {{files:shards.sort((a,b)=>Number(a.name.slice(parent.length).match(/^.+-([0-9]+)-of-/i)[1])-Number(b.name.slice(parent.length).match(/^.+-([0-9]+)-of-/i)[1])),complete:indices.size===count&&Array.from({{length:count}},(_,i)=>indices.has(i+1)).every(Boolean)}};
}}
function updateAdoptButton(){{
 const set=selectedFileSet(),noLocations=$('adopt-location').options.length<=1; $('adopt-settings-link').hidden=!noLocations;
 const enabled=Boolean(state.selectedArtifact&&set.complete&&$('adopt-consent').checked);$('adopt-submit').disabled=!enabled;
 const location=$('adopt-location').selectedOptions[0]?.textContent||'';
 $('adopt-reason').textContent=state.selectedArtifact?set.complete?`Files to adopt: ${{set.files.map(file=>`${{file.name}} (${{file.size}} bytes)`).join(', ')}}. Location: ${{location}}. Provenance: locally hashed; publisher unverified. Source and revision: unknown.${{enabled?'':' Consent to this label before adopting.'}}`:`The selected shard set is incomplete in this location.`:noLocations?'No artifact location is configured. Add one in Settings › Storage & artifacts.':!state.artifactFiles.length?'Choose a configured artifact location and list its files.':'Choose a file from the listing.';
}}
function renderArtifactFiles(files){{
 const box=$('artifact-file-list');box.textContent='';state.artifactFiles=files;state.selectedArtifact='';
 for(const file of files){{const label=document.createElement('label'),input=document.createElement('input');input.type='radio';input.name='artifact-file';input.value=file.name;
  input.addEventListener('change',()=>{{state.selectedArtifact=input.value;$('adopt-name').value=input.value.split('/').pop();updateAdoptButton()}});
  label.append(input,document.createTextNode(` ${{file.name}} — ${{file.size}} bytes`));box.append(label)}}
 updateAdoptButton();
}}
function renderDownloads(items){{
 const box=$('download-list'),focused=document.activeElement;
 const focusId=focused&&focused.dataset&&focused.dataset.downloadId,focusAction=focused&&focused.dataset&&focused.dataset.downloadAction;
 box.textContent='';
 if(!items.length){{box.textContent='No downloads yet.';return}}
 for(const item of items){{
   const article=document.createElement('article'), heading=document.createElement('h5'), summary=document.createElement('p'), progress=document.createElement('p'), reasonText=document.createElement('p');
   heading.textContent=`Download ${{item.purpose}}`;heading.dataset.downloadId=item.id;heading.tabIndex=-1;
   summary.textContent=`${{item.outcome?'Outcome: '+item.outcome:'State: '+item.state}}. Elapsed: ${{Math.floor(item.progress?.elapsed||0)}} seconds.`;
   if(item.progress?.unknown)progress.textContent=item.progress.unknown;
   else if(item.phase)progress.textContent=`Phase: ${{item.phase}}. Current file: ${{item.file||'Not reported'}}. Transferred: ${{item.transferred??'Not reported'}} of ${{item.total??'Not reported'}} bytes.`;
   else progress.textContent='Phase and current file have not been reported.';
   reasonText.textContent=item.reason?`Reason: ${{item.reason}}`:'';
   article.append(heading,summary,progress,reasonText);
   const link=document.createElement('a');link.href=`#/runs?work=${{encodeURIComponent(item.id)}}`;link.textContent='View this download in Runs & jobs';link.dataset.downloadId=item.id;link.dataset.downloadAction='open';article.append(link);
   if(!item.outcome){{
     if(item.state==='running'){{
       const cancel=document.createElement('button');cancel.type='button';cancel.textContent='Cancel download…';cancel.dataset.downloadId=item.id;cancel.dataset.downloadAction='cancel';cancel.addEventListener('click',()=>window.matutoPopup.preview({{title:'Cancel download?',stops:['The download'],starts:[],stays:['Its partial files, kept for Resume'],safe:'Keep downloading',confirm:'Cancel download',returnFocus:()=>downloadControl(item.id,'cancel')||downloadHeading(item.id),confirmAction:async()=>{{const response=await fetch('/api/runs',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}},body:JSON.stringify({{action:'stop',id:item.id}})}});let answer={{}};try{{answer=await response.json()}}catch(error){{}}if(!response.ok)throw new Error(answer.error||'The download was not cancelled.');await refreshDownloads()}}}}));article.append(cancel);
     }}
   }}else if(item.partials){{
     const resume=document.createElement('button'),resumeReason=document.createElement('p');resume.type='button';resume.textContent='Resume';resume.dataset.downloadId=item.id;resume.dataset.downloadAction='resume';
     resumeReason.textContent=actionRefusals.get(item.id+':resume')||'';
     resume.addEventListener('click',async()=>{{try{{await api('resume',{{job_id:item.id}});actionRefusals.delete(item.id+':resume');await refreshDownloads()}}catch(error){{actionRefusals.set(item.id+':resume',`Resume refused: ${{error.message}}`);resumeReason.textContent=actionRefusals.get(item.id+':resume')}}}});
     const discard=document.createElement('button'),discardReason=document.createElement('p');discard.type='button';discard.textContent='Discard partial download…';discard.dataset.downloadId=item.id;discard.dataset.downloadAction='discard';
     discardReason.textContent=actionRefusals.get(item.id+':discard')||'';
     discard.addEventListener('click',async()=>{{try{{const preview=await api('discard-preview',{{job_id:item.id}});actionRefusals.delete(item.id+':discard');discardPopup(preview)}}catch(error){{actionRefusals.set(item.id+':discard',`Discard refused: ${{error.message}}`);discardReason.textContent=actionRefusals.get(item.id+':discard')}}}});
     article.append(resume,resumeReason,discard,discardReason);
   }}
   box.append(article);
 }}
 if(focusId&&focusAction){{const replacement=downloadControl(focusId,focusAction);if(replacement)replacement.focus()}}
}}
async function refreshDownloads(){{try{{const data=await api('prepare',{{method:'GET'}});updateReadiness(data);renderDownloads(data.downloads||[]);renderRecords(data.records||[]);const locations=$('adopt-location'),previous=locations.value;locations.textContent='';const empty=document.createElement('option');empty.value='';empty.textContent='Choose a configured artifact location';locations.append(empty);for(const location of data.artifact_locations||[]){{const option=document.createElement('option');option.value=location.location_id;option.textContent=location.path;locations.append(option)}}locations.value=(data.artifact_locations||[]).some(location=>location.location_id===previous)?previous:'';updateAdoptButton()}}catch(error){{$('download-list').textContent=error.message;$('acquisition-record-list').textContent=error.message}}
}}
let downloadPoll=0;
function routeDownloads(){{
 const page=(window.location.hash||'').replace(/^#\\/?/,'').split(/[/?]/)[0];
 if(page==='model-files'&&!downloadPoll)downloadPoll=window.setInterval(refreshDownloads,2000);
 else if(page!=='model-files'&&downloadPoll){{window.clearInterval(downloadPoll);downloadPoll=0}}
}}
window.addEventListener('hashchange',routeDownloads);
async function prepare(){{
 state.ready=false;refreshReasons();
 try{{const data=await api('prepare',{{method:'GET'}});updateReadiness(data);renderDownloads(data.downloads||[]);renderRecords(data.records||[]);
  const locations=$('adopt-location'),empty=document.createElement('option');empty.value='';empty.textContent='Choose a configured artifact location';locations.append(empty);
  for(const location of data.artifact_locations||[]){{const option=document.createElement('option');option.value=location.location_id;option.textContent=location.path;locations.append(option)}}updateAdoptButton();
 }}catch(error){{notice('model-files','acquisition-load','blocker',error.message,'');reason($('prepare-continue'),error.message)}}
}}
async function listFiles(){{
 state.repository=$('repository').value.trim();state.revision=$('revision').value.trim();invalidate();message('');
 const generation=state.generation;
 try{{const data=await api('list',{{repository:state.repository,revision:state.revision}});if(generation!==state.generation)return;
 state.files=data.files;clearNotice('model-files','acquisition-storage');clearNotice('model-files','acquisition-origin');const box=$('file-list');box.textContent='';
 for(const file of state.files){{const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.value=file.name;
 input.addEventListener('change',()=>{{if(input.checked)state.selected.add(file.name);else state.selected.delete(file.name);state.generation++;state.resolution=null;state.reviewKey='';$('review-details').textContent='';$('no-checksum-consent').checked=false;refreshReasons()}});
 label.append(input,document.createTextNode(' '+file.name+' ('+file.size+' bytes)'));box.append(label)}}
 refreshReasons();message('Choose the files you need. No file is pre-selected or ranked.');
 }}catch(error){{if(generation===state.generation)reportError(error)}}
}}
function showReview(data){{
 state.resolution=data;
 const box=$('review-details');box.textContent='';
 for(const text of [
 'Permitted source: '+data.repository+' via '+data.origin,
 'Resolved commit: '+data.revision,
 ...data.files.map(file=>file.name+' — '+file.size+' bytes — '+(file.sha256||'no published checksum')),
 'Total: '+data.total_size+' bytes','Destination: '+data.destination]){{const row=document.createElement('p');row.textContent=text;box.append(row)}}
 const needsConsent=data.files.some(file=>!file.sha256);$('consent-row').hidden=!needsConsent;
 if(needsConsent){{const row=document.createElement('p');row.textContent='Acquire requires your explicit consent that these files will be locally hashed; no published checksum is available for them.';box.append(row)}}
 refreshReasons();message('Review the complete request before continuing.');go('review');
}}
async function resolveFiles(){{
 state.repository=$('repository').value.trim();state.revision=$('revision').value.trim();
 const key=sourceKey(), sameRequest=state.reviewKey===key;
 if(state.resolution&&sameRequest){{showReview(state.resolution);return}}
 state.resolution=null;
 if(!sameRequest){{state.reviewKey='';$('no-checksum-consent').checked=false}}
 refreshReasons();
 const generation=state.generation;
 try{{const data=await api('resolve',{{repository:state.repository,revision:state.revision,files:[...state.selected]}});
 if(generation!==state.generation||key!==sourceKey())return;
 clearNotice('model-files','acquisition-storage');clearNotice('model-files','acquisition-origin');
 state.reviewKey=key;showReview(data);
 }}catch(error){{if(generation===state.generation)reportError(error)}}
}}
async function start(){{
 if(!state.resolution||state.reviewKey!==sourceKey()){{message('Resolve and review the current source and file selection before starting.');return}}
 const reviewed=state.resolution, consent=$('no-checksum-consent').checked;
 if(reviewed.files.some(file=>!file.sha256)&&!consent){{const text='Consent to locally hashing these files is required before starting.';message(text);reason($('start-download'),text);return}}
 try{{const data=await api('acquire',{{repository:reviewed.repository,revision:reviewed.revision,files:reviewed.files.map(file=>file.name),consent,destination:reviewed.destination}});
 clearNotice('model-files','acquisition-storage');clearNotice('model-files','acquisition-origin');clearNotice('model-files','acquisition-work');
 $('acquire-status').textContent=data.text+(data.warning?' '+data.warning:'');if(data.job_id){{const link=document.createElement('a');link.href='#runs';link.textContent='View this job in Runs & jobs';$('acquire-status').append(document.createTextNode(' '),link)}}message('');
 }}catch(error){{
   reportError(error);
   if(destinationChanged(error)){{
     state.resolution=null;$('review-details').textContent='';refreshReasons();
   }}
 }}
}}
for(const button of root.querySelectorAll('[data-step]'))button.addEventListener('click',()=>navigate(button.dataset.step,button));
for(const button of root.querySelectorAll('[data-back]'))button.addEventListener('click',()=>navigate(button.dataset.back,button));
for(const button of root.querySelectorAll('[data-next]'))button.addEventListener('click',()=>navigate(button.dataset.next,button));
$('repository').addEventListener('input',invalidate);$('revision').addEventListener('input',invalidate);
$('list-files').addEventListener('click',listFiles);$('resolve-files').addEventListener('click',resolveFiles);$('start-download').addEventListener('click',start);
$('no-checksum-consent').addEventListener('change',refreshReasons);
$('list-artifact-files').addEventListener('click',async()=>{{try{{if(!$('adopt-location').value)throw new Error('Choose a configured artifact location.');const data=await api('artifact-files',{{location_id:$('adopt-location').value}});renderArtifactFiles(data.files||[])}}catch(error){{$('adopt-status').textContent=error.message}}}});
$('adopt-location').addEventListener('change',()=>{{renderArtifactFiles([]);$('adopt-name').value='';$('adopt-status').textContent=''}});
$('adopt-consent').addEventListener('change',updateAdoptButton);
$('adopt-submit').addEventListener('click',async()=>{{try{{const result=await api('adopt',{{location_id:$('adopt-location').value,file_name:state.selectedArtifact,name:$('adopt-name').value,consent:$('adopt-consent').checked}});$('adopt-status').textContent=`Adopted ${{result.acquisition_id}}: locally hashed; publisher unverified. Source and revision are unknown.`;$('adopt-consent').checked=false;renderArtifactFiles([]);await refreshDownloads()}}catch(error){{$('adopt-status').textContent=error.message}}}});
prepare();routeDownloads();
}},{{once:true}});
}})();
</script>'''
