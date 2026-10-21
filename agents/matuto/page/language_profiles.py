"""Language profile authoring and status in Profiles › Language profiles."""

PAGE = "profiles"
NAME = "language"
TITLE = "Language profiles"
ORDER = 1


SCRIPT = r"""
document.addEventListener('DOMContentLoaded', () => {
  const root = document.getElementById('languageProfiles');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const form = $('languageProfileForm'), list = $('languageProfileList');
  const create = $('newLanguageProfile');
  const error = $('profileError'), success = $('profileSuccess');
  let catalog = {}, prerequisitesReady = false, readGeneration = 0, editing = null;
  create.hidden = true;
  form.hidden = true;

  function languageProfileRoute() {
    const route = (window.location.hash || '').replace(/^#\/?/, '').split('?')[0];
    return route === 'profiles' || route === 'profiles/language';
  }
  function createRequested() {
    return (window.location.hash.split('?')[1] || '').split('&').includes('new');
  }
  function clearForm() {
    form.hidden = true;
    form.reset();
    editing = null;
    $('profileId').readOnly = false;
    $('languageProfileFormTitle').textContent = 'New language profile';
    $('languageProfileSave').textContent = 'Create profile';
    for (const tool of ['server', 'smoke', 'bench'])
      root.querySelector(`[data-argument-rows="${tool}"]`).textContent = '';
  }

  window.addEventListener('language-profile-actions-changed', async () => {
    try {
      const refreshed = await api.request('/api/language-profiles');
      drawProfiles(refreshed.profiles);
      clearNotice('profiles', 'language-profile-load');
    } catch (failure) {
      notice('profiles', 'language-profile-load', 'blocker', 'Language profiles could not be refreshed.', failure.message);
    }
  });

  function option(select, value, label) {
    const node = document.createElement('option');
    node.value = value; node.textContent = label; select.append(node);
  }
  function fillPick(select, records, required) {
    select.textContent = '';
    option(select, '', required ? 'Choose a complete acquired file' : 'None');
    for (const record of records)
      option(select, JSON.stringify(record.pick), `${record.name} — ${record.file_name}`);
  }
  function drawProfiles(records) {
    list.textContent = '';
    for (const record of records) {
      const item = document.createElement('article');
      item.className = 'language-profile-record';
      item.setAttribute('data-profile-id', record.profile_id);
      const heading = document.createElement('h4'); heading.textContent = record.name;
      const alias = document.createElement('p'); alias.textContent = `Alias: ${record.alias}`;
      const labels = document.createElement('ul'); labels.setAttribute('aria-label', `${record.name} status`);
      const status = record.qualification.state === 'qualified' ? 'Qualified' :
        record.qualification.changed_since_qualification ? 'Candidate — changed since qualification' : 'Candidate';
      for (const label of [status, record.published ? 'Published' : 'Not published']) {
        const entry = document.createElement('li'); entry.textContent = label; labels.append(entry);
      }
      const edit = document.createElement('button'); edit.type = 'button'; edit.textContent = 'Edit';
      edit.addEventListener('click', () => openEditor(record.profile_id));
      item.append(heading, alias, labels, edit);
      if (!record.available) {
        const reasonText = document.createElement('p');
        reasonText.className = 'unavailable-reason';
        reasonText.textContent = `Unavailable: ${record.reason}`;
        item.append(reasonText);
      }
      list.append(item);
    }
    window.dispatchEvent(new Event('language-profile-list-rendered'));
  }
  function addArgument(tool, data = {}) {
    const rows = root.querySelector(`[data-argument-rows="${tool}"]`);
    const row = document.createElement('div'); row.className = 'argument-row';
    const choice = document.createElement('select'); choice.setAttribute('aria-label', `${tool} argument source`);
    option(choice, '', 'Choose catalogue entry or Custom');
    for (const entry of catalog[tool] || []) option(choice, entry.flag, entry.flag);
    option(choice, '__custom__', 'Custom');
    if (data.catalogue) choice.value = data.catalogue;
    else if (data.option) choice.value = '__custom__';
    const flag = document.createElement('input'); flag.type = 'text';
    flag.setAttribute('aria-label', `${tool} Custom flag`); flag.value = data.option || '';
    const value = document.createElement('input'); value.type = 'text';
    value.setAttribute('aria-label', `${tool} argument value`); value.value = data.value || '';
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Remove';
    remove.addEventListener('click', () => { row.remove(); updateSaveLabel(); });
    function update() { flag.hidden = choice.value !== '__custom__'; }
    choice.addEventListener('change', update); update();
    row.append(choice, flag, value, remove); rows.append(row);
  }
  function populate(data) {
    catalog = data.catalogue;
    for (const [field, records] of Object.entries(data.picks))
      fillPick($(`${field}Pick`), records, field === 'model');
    const cards = $('cardPick'); cards.textContent = '';
    option(cards, '', 'Choose a stored card');
    for (const card of data.cards) option(cards, card.card_id, card.name);
    for (const tool of ['server', 'smoke', 'bench'])
      root.querySelector(`[data-argument-rows="${tool}"]`).textContent = '';
    drawProfiles(data.profiles);
    const prerequisites = data.prerequisites || [];
    prerequisitesReady = prerequisites.length === 0;
    create.hidden = !prerequisitesReady;
    form.hidden = true;
    if (!prerequisitesReady) {
      const names = prerequisites.map(item => item.name);
      notice('profiles', 'language-profile-prerequisites', 'blocker',
        `This profile needs ${names.join(' and ')} first.`,
        'Complete each missing prerequisite before creating a profile.',
        prerequisites.map(item => ({label: item.destination, href: item.href})));
    } else clearNotice('profiles', 'language-profile-prerequisites');
    const canCreate = data.cards.length > 0 && data.picks.model.length > 0;
    const why = !data.picks.model.length ? 'No complete acquired model file is available.' : '';
    reason(create, !prerequisitesReady || canCreate ? '' : why);
  }
  function refreshCatalogue(next) {
    const previous = catalog;
    catalog = next;
    for (const tool of ['server', 'smoke', 'bench']) {
      const oldEntries = previous[tool] || [], newEntries = catalog[tool] || [];
      for (const row of root.querySelectorAll(`[data-argument-rows="${tool}"] .argument-row`)) {
        const choice = row.children[0], oldFlag = choice.value;
        const oldEntry = oldEntries.find(entry => entry.flag === oldFlag);
        const newEntry = newEntries.find(entry => entry.flag === oldFlag);
        let selected = oldFlag;
        if (oldFlag && oldFlag !== '__custom__'
            && (!newEntry || (oldEntry && oldEntry.takes_value !== newEntry.takes_value))) {
          row.children[1].value = oldFlag;
          selected = '__custom__';
        }
        choice.textContent = '';
        option(choice, '', 'Choose catalogue entry or Custom');
        for (const entry of newEntries) option(choice, entry.flag, entry.flag);
        option(choice, '__custom__', 'Custom');
        choice.value = selected;
        row.children[1].hidden = choice.value !== '__custom__';
      }
    }
  }
  window.addEventListener('matuto:argument-catalogue-updated', async () => {
    try {
      refreshCatalogue((await api.request('/api/language-profiles')).catalogue);
      clearNotice('profiles', 'argument-catalogue-refresh');
    } catch (failure) {
      notice('profiles', 'argument-catalogue-refresh', 'blocker',
        'Argument options could not be refreshed.', failure.message);
    }
  });
  function loadRecord(record) {
    $('profileId').value = record.profile_id;
    $('profileName').value = record.name;
    $('profileAlias').value = record.alias;
    $('profileContext').value = record.context_size;
    $('profileThinking').value = record.thinking;
    $('cardPick').value = record.card_id;
    $('profileNotes').value = record.notes || '';
    for (const field of ['model', 'mmproj', 'mtp'])
      $(`${field}Pick`).value = JSON.stringify(record[field] || null);
    for (const tool of ['server', 'smoke', 'bench'])
      root.querySelector(`[data-argument-rows="${tool}"]`).textContent = '';
    for (const row of record.arguments || []) addArgument(row.tool, row);
    $('profileId').readOnly = true;
    $('languageProfileFormTitle').textContent = `Edit ${record.name}`;
    updateSaveLabel();
    form.hidden = false;
    $('profileName').focus();
  }
  async function openEditor(profileId, contextSize) {
    try {
      const data = await api.request(`/api/language-profile-edits?profile_id=${encodeURIComponent(profileId)}`);
      populate(data);
      editing = data.profile;
      loadRecord(editing);
      const proposedContext = Number(contextSize);
      if (contextSize && Number.isSafeInteger(proposedContext) && proposedContext > 0) {
        $('profileContext').value = String(proposedContext);
        updateSaveLabel();
      }
    } catch (failure) { error.textContent = failure.message; error.focus(); }
  }
  function display(value) {
    return value === null || value === undefined ? 'None' :
      typeof value === 'object' ? JSON.stringify(value) : String(value);
  }
  function servedChanged(record) {
    const served = value => JSON.stringify({context_size:value.context_size, thinking:value.thinking,
      card_id:value.card_id, model:value.model ?? null, mmproj:value.mmproj ?? null, mtp:value.mtp ?? null,
      arguments:value.arguments.filter(row => row.tool === 'server')});
    return served(record) !== served(editing);
  }
  function updateSaveLabel() {
    $('languageProfileSave').textContent = editing && servedChanged(payload()) ? 'Review…' :
      editing ? 'Save profile' : 'Create profile';
  }
  function payload() {
    const record = {kind: 'language', profile_id: $('profileId').value,
      name: $('profileName').value, alias: $('profileAlias').value,
      context_size: Number($('profileContext').value), thinking: $('profileThinking').value,
      card_id: $('cardPick').value, notes: $('profileNotes').value,
      model: JSON.parse($('modelPick').value || 'null'),
      mmproj: JSON.parse($('mmprojPick').value || 'null'),
      mtp: JSON.parse($('mtpPick').value || 'null'), arguments: []};
    for (const tool of ['server', 'smoke', 'bench'])
      for (const [position, row] of [...root.querySelectorAll(`[data-argument-rows="${tool}"] .argument-row`)].entries()) {
        const source = row.children[0].value, custom = source === '__custom__';
        if (!source) {
          if (row.children[1].value || row.children[2].value)
            throw new Error(`${tool} argument row ${position + 1} must choose a catalogue entry or Custom.`);
          continue;
        }
        record.arguments.push({tool, ...(custom ? {option: row.children[1].value} : {catalogue: source}),
          value: row.children[2].value || null});
      }
    return record;
  }
  async function finishSave(result, wasEditing) {
    clearForm();
    success.textContent = wasEditing ? 'Saved language profile.' : 'Created unpublished profile.';
    if (result.warnings?.length)
      notice('profiles', 'language-profile-save', 'warning',
        wasEditing ? 'Profile saved with launch warnings.' : 'Profile created with launch warnings.', result.warnings.join(' '));
    else clearNotice('profiles', 'language-profile-save');
    await readProfiles(false);
  }
  for (const tool of ['server', 'smoke', 'bench'])
    root.querySelector(`[data-add-argument="${tool}"]`).addEventListener('click', () => addArgument(tool));
  create.addEventListener('click', () => { if (prerequisitesReady && create.getAttribute('aria-disabled') !== 'true') {
    clearForm(); form.hidden = false; $('profileId').focus();
  }});
  form.addEventListener('input', updateSaveLabel);
  form.addEventListener('change', updateSaveLabel);
  $('languageProfileCancel').addEventListener('click', clearForm);
  form.addEventListener('submit', async event => {
    event.preventDefault(); error.textContent = ''; success.textContent = '';
    let result;
    const wasEditing = Boolean(editing);
    let record;
    try { record = payload(); }
    catch (failure) { error.textContent = failure.message; error.focus(); return; }
    try {
      if (editing) {
        if (servedChanged(record)) {
          const preview = await api.request('/api/language-profile-edits',
            {method:'POST',body:{action:'preview',record}});
          if (['cosmetic-only', 'unchanged'].includes(preview.preview.kind)) {
            result = await api.request('/api/language-profile-edits', {method:'POST',body:{action:'save',record}});
          } else {
            const content = document.createElement('div');
            for (const changed of preview.preview.changed_values) {
              const line = document.createElement('p');
              line.textContent = `${changed.label}: ${display(changed.old)} → ${display(changed.new)}`; content.append(line);
            }
            const profileState = preview.preview.state;
            const stateText = profileState?.state === 'candidate' && profileState.changed_since_qualification
              ? 'candidate — changed since qualification' : profileState?.state || 'unavailable';
            const profileLine = document.createElement('p');
            profileLine.textContent = `${record.name}: ${stateText}`; content.append(profileLine);
            for (const affected of preview.preview.affected || []) {
              const line = document.createElement('p');
              const state = affected.state?.state === 'candidate' && affected.state.changed_since_qualification
                ? 'candidate — changed since qualification' : affected.state?.state || 'unavailable';
              line.textContent = `Bundle ${affected.name || affected.id}: ${state}`; content.append(line);
            }
            window.matutoPopup.open({title:`Review changes to ${record.name}`,content,safe:'Cancel',confirm:'Save profile',
              confirmAction:async()=>{
                const saved = await api.request('/api/language-profile-edits',
                  {method:'POST',body:{action:'save',record,confirm:true,review_token:preview.review_token}});
                await finishSave(saved, true);
              }});
            return;
          }
        } else result = await api.request('/api/language-profile-edits', {method:'POST',body:{action:'save',record}});
      } else result = await api.request('/api/language-profiles', {method: 'POST', body: record});
    } catch (failure) { error.textContent = failure.message; error.focus(); return; }
    await finishSave(result, wasEditing);
  });
  function readProfiles(openCreate = true) {
    const generation = ++readGeneration;
    prerequisitesReady = false;
    create.hidden = true;
    clearForm();
    clearNotice('profiles', 'language-profile-prerequisites');
    clearNotice('profiles', 'language-profile-load');
    return api.request('/api/language-profiles').then(data => {
      if (generation !== readGeneration || !languageProfileRoute()) return;
      populate(data);
      if (!openCreate) form.hidden = true;
      else if (prerequisitesReady) {
        const params = new URLSearchParams(window.location.hash.split('?')[1] || '');
        if (params.get('edit')) openEditor(params.get('edit'), params.get('context'));
        else if (createRequested()) {
          const pick = {acquisition_id: params.get('acquisition_id'), file_name: params.get('file_name')};
          const model = $('modelPick');
          if (pick.acquisition_id && pick.file_name) {
            const value = JSON.stringify(pick);
            if ([...model.options].some(option => option.value === value)) model.value = value;
          }
          form.hidden = false; $('profileId').focus();
        }
      }
      clearNotice('profiles', 'language-profile-load');
    }).catch(failure => {
      if (generation !== readGeneration || !languageProfileRoute()) return;
      create.hidden = true;
      form.hidden = true;
      notice('profiles', 'language-profile-load', 'blocker',
        'Language profiles are unavailable.', failure.message);
    });
  }
  function routeChanged() {
    if (languageProfileRoute()) return readProfiles();
    ++readGeneration;
    prerequisitesReady = false;
    create.hidden = true;
    clearForm();
  }
  window.addEventListener('hashchange', routeChanged);
  routeChanged();
});
"""


def html(_status):
    return '''<div id="languageProfiles">
<div class="profile-actions"><button type="button" id="newLanguageProfile" hidden>New</button></div>
<div id="languageProfileList" aria-live="polite"></div>
<p id="profileSuccess" role="status"></p>
<form id="languageProfileForm" hidden>
<h4 id="languageProfileFormTitle">New language profile</h4><p id="profileError" role="alert" tabindex="-1"></p>
<label>Stable ID <input id="profileId" required></label>
<label>Name <input id="profileName" required></label>
<label>Alias <input id="profileAlias" required></label>
<label>Context <input id="profileContext" type="number" min="1" required></label>
<label>Thinking <input id="profileThinking" required></label>
<label>Model file <select id="modelPick" required></select></label>
<label>mmproj <select id="mmprojPick"></select></label>
<label>MTP <select id="mtpPick"></select></label>
<label>Card <select id="cardPick" required></select></label>
<label>Notes <textarea id="profileNotes"></textarea></label>
<section><h5>Server arguments</h5><div data-argument-rows="server"></div><button type="button" data-add-argument="server">Add server argument</button></section>
<section><h5>Smoke arguments</h5><div data-argument-rows="smoke"></div><button type="button" data-add-argument="smoke">Add smoke argument</button></section>
<section><h5>Bench arguments</h5><div data-argument-rows="bench"></div><button type="button" data-add-argument="bench">Add bench argument</button></section>
<button type="submit" id="languageProfileSave">Create profile</button><button type="button" id="languageProfileCancel">Cancel</button>
</form>
<link rel="stylesheet" href="/language-profile-actions.css">
<script src="/language-profiles.js" defer></script>
<script src="/language-profile-actions.js" defer></script>
</div>'''
