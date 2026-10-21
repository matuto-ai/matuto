"""Settings › Argument catalogue, including profile-use conversion on removal."""

PAGE = "settings"
NAME = "catalogue"
TITLE = "Argument catalogue"
ORDER = 4

SCRIPT = r"""
(function(){
'use strict';
const form=document.getElementById('catalogue-form');
const list=document.getElementById('catalogue-list');
const result=document.getElementById('catalogue-result');
const cancel=document.getElementById('catalogue-cancel');
const tools=['server','smoke','bench'];
let entries=[];
const field=name=>form.elements.namedItem(name);
function notifyProfiles(){window.dispatchEvent(new Event('matuto:argument-catalogue-updated'));}
function setResult(text){result.textContent=text;}
function reset(){form.reset();field('argument_id').value='';
  document.getElementById('catalogue-form-title').textContent='Add argument';cancel.hidden=true;}
function draw(){
  list.textContent='';
  if(!entries.length){const empty=document.createElement('p');empty.textContent='No argument entries yet.';list.append(empty);return;}
  for(const entry of entries){
    const item=document.createElement('article');item.className='catalogue-entry';
    const title=document.createElement('h3');title.textContent=entry.flag;item.append(title);
    const details=document.createElement('p');
    details.textContent=`${entry.takes_value?'Takes a value':'Takes no value'} · ${entry.description} · Tools: ${entry.tools.join(', ')}`;item.append(details);
    const edit=document.createElement('button');edit.type='button';edit.textContent='Edit entry';
    edit.addEventListener('click',()=>{
      field('argument_id').value=entry.argument_id;field('flag').value=entry.flag;
      field('takes_value').checked=entry.takes_value;field('description').value=entry.description;
      for(const tool of tools)form.querySelector(`[name="tools"][value="${tool}"]`).checked=entry.tools.includes(tool);
      document.getElementById('catalogue-form-title').textContent=`Edit ${entry.flag}`;cancel.hidden=false;field('flag').focus();
    });item.append(edit);
    const remove=document.createElement('button');remove.type='button';remove.textContent='Review removal';
    remove.addEventListener('click',()=>window.matutoPopup.open({
      title:`Remove ${entry.flag}`,
      content:(()=>{const note=document.createElement('p');
        note.textContent=entry.uses.length?`Removing this entry turns ${entry.uses.length} existing profile argument row(s) into Custom. Their flag, value and position stay unchanged.`:'No saved profile rows use this entry.';return note;})(),
      safe:'Keep entry',confirm:'Remove entry',confirmAction:async()=>{
        const removed=await api.request('/api/argument-catalogue',{method:'POST',body:{action:'remove',argument_id:entry.argument_id}});
        notifyProfiles();
        const rows=removed.converted.map(row=>`${row.name} (${row.tool}: ${row.flag})`);
        setResult(rows.length?`${removed.flag} removed. These rows are now Custom: ${rows.join('; ')}.`:`${removed.flag} removed; no profile rows needed conversion.`);
        await load();
      }}));item.append(remove);list.append(item);
  }
}
async function load(){const data=await api.request('/api/argument-catalogue');entries=data.entries;draw();}
form.addEventListener('submit',async event=>{
  event.preventDefault();
  const identifier=field('argument_id').value;
  const entry={flag:field('flag').value,takes_value:field('takes_value').checked,
    description:field('description').value,tools:tools.filter(tool=>form.querySelector(`[name="tools"][value="${tool}"]`).checked)};
  try{
    await api.request('/api/argument-catalogue',{method:'POST',body:{action:identifier?'edit':'add',argument_id:identifier||undefined,entry}});
    notifyProfiles();setResult(identifier?'Argument entry updated.':'Argument entry added.');reset();await load();
  }catch(error){setResult(error.message);}
});
cancel.addEventListener('click',reset);
load().catch(error=>setResult(`Argument catalogue is unavailable. ${error.message}`));
})();
"""


def html(_status):
    return '''<section class="panel" id="argument-catalogue">
<p>Manage the argument options offered by profile forms. Existing profile arguments are not changed when an entry is edited.</p>
<div id="catalogue-result" role="status" aria-live="polite"></div>
<div id="catalogue-list" aria-live="polite"></div>
<form id="catalogue-form">
<h2 id="catalogue-form-title">Add argument</h2>
<input type="hidden" name="argument_id">
<label>Flag <input name="flag" required pattern="--?[A-Za-z0-9][A-Za-z0-9-]*" placeholder="--parallel"></label>
<label class="takes-value"><input name="takes_value" type="checkbox" checked> Takes a value</label>
<label>Description <input name="description" required></label>
<fieldset><legend>Tools</legend>
<label><input type="checkbox" name="tools" value="server"> Server</label>
<label><input type="checkbox" name="tools" value="smoke"> Smoke</label>
<label><input type="checkbox" name="tools" value="bench"> Bench</label>
</fieldset>
<button type="submit">Save argument</button> <button type="button" id="catalogue-cancel" hidden>Cancel edit</button>
</form>
</section><script src="/argument-catalogue.js" defer></script>'''
