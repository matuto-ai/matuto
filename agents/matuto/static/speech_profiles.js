'use strict';

(() => {
  const root = document.getElementById('speechProfiles');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const form = $('speechProfileForm'), list = $('speechProfileList');
  const stylesheet = document.createElement('link');
  stylesheet.rel = 'stylesheet'; stylesheet.href = '/speech-profiles.css'; document.head.append(stylesheet);
  let data = {profiles: [], executables: [], weights: [], cards: []};
  let mode = 'create', source = null, handoffAcquisitionId = null, routeGeneration = 0;

  async function request(body) {
    const response = await fetch('/api/speech-profiles', {
      method: body ? 'POST' : 'GET',
      headers: body ? {'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || ''} : {},
      body: body ? JSON.stringify(body) : undefined, cache: 'no-store'
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'The request was refused.');
    return result;
  }
  function fail(error) {
    $('speechProfileError').textContent = error.message;
    $('speechProfileError').hidden = false;
    $('speechProfileError').focus();
  }
  function clearError() { $('speechProfileError').textContent = ''; $('speechProfileError').hidden = true; }
  function addOption(select, value, text) {
    const option = document.createElement('option'); option.value = value; option.textContent = text;
    select.append(option);
  }
  function fill(select, rows, label, required = false) {
    select.replaceChildren();
    addOption(select, '', required ? 'Choose one' : 'None');
    for (const row of rows) addOption(select, row.value, label(row));
  }
  function render() {
    list.replaceChildren();
    for (const profile of data.profiles) {
      const article = document.createElement('article'); article.className = 'speech-profile-record';
      const title = document.createElement('h4'); title.textContent = profile.name; article.append(title);
      for (const text of [`Kind: ${profile.kind[0].toUpperCase()}${profile.kind.slice(1)}`, `Speech executable: ${profile.executable}`,
        `Weights: ${profile.weights_name || 'None'}`,
        ...(profile.kind === 'synthesis' ? [`Card: ${profile.card || 'None'}`] : []),
        `Bundles: ${profile.bundles.length ? profile.bundles.map(bundle => bundle.name).join(', ') : 'None'}`,
        ...(profile.notes ? [`Notes: ${profile.notes}`] : [])]) {
        const line = document.createElement('p'); line.textContent = text; article.append(line);
      }
      if (!profile.available) {
        const unavailable = document.createElement('p'); unavailable.textContent = `Unavailable: ${profile.reason}`;
        article.append(unavailable);
      }
      const actions = document.createElement('div'); actions.className = 'speech-profile-actions';
      for (const [label, callback] of [['Edit', () => openForm('edit', profile)],
        ['Duplicate…', () => openForm('duplicate', profile)], ['Remove', () => remove(profile)]]) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
        button.addEventListener('click', callback); actions.append(button);
      }
      article.append(actions); list.append(article);
    }
  }
  function argumentRow(argument = {}) {
    const row = document.createElement('div'); row.className = 'speech-argument-row';
    row.dataset.catalogue = argument.catalogue || '';
    row.dataset.tool = argument.tool || 'server';
    row.dataset.value = JSON.stringify(argument.value ?? null);
    const flag = document.createElement('input'); flag.type = 'text'; flag.value = argument.option || argument.catalogue || '';
    flag.setAttribute('aria-label', 'Argument flag');
    const value = document.createElement('input'); value.type = 'text'; value.value = argument.value ?? '';
    value.setAttribute('aria-label', 'Argument value');
    value.addEventListener('input', () => { row.dataset.valueEdited = 'true'; updateSaveLabel(); });
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Remove argument';
    remove.addEventListener('click', () => { row.remove(); updateSaveLabel(); }); row.append(flag, value, remove);
    $('speechProfileArguments').append(row);
  }
  function fillForm(profile) {
    const kind = $('speechProfileKind').value;
    fill($('speechProfileExecutable'), data.executables.filter(item => item.kind === kind).map(item => ({
      value: item.id, text: `${item.project} · ${item.backend} · ${item.commit}`})), row => row.text, true);
    const weights = data.weights.filter(item => mode !== 'create' || !handoffAcquisitionId ||
      item.pick.acquisition_id === handoffAcquisitionId);
    fill($('speechProfileWeights'), weights.map(item => ({value: JSON.stringify(item.pick), text: `${item.name} — ${item.file_name}`})), row => row.text, true);
    fill($('speechProfileCard'), data.cards.map(item => ({value: item.card_id, text: item.name})), row => row.text, true);
    $('speechProfileCardLabel').hidden = kind !== 'synthesis';
    $('speechProfileCard').disabled = kind !== 'synthesis';
    $('speechProfileExecutable').value = profile?.speech_executable_id || '';
    $('speechProfileWeights').value = profile?.weights ? JSON.stringify(profile.weights) : '';
    $('speechProfileCard').value = profile?.card_id || '';
    $('speechProfileArguments').replaceChildren();
    for (const argument of profile?.arguments || []) argumentRow(argument);
    const noExecutable = !data.executables.some(item => item.kind === kind);
    const executableHelp = $('speechProfileExecutableHelp');
    executableHelp.textContent = '';
    if (noExecutable) {
      executableHelp.append(document.createTextNode(`No installed ${kind} speech executable. `));
      const link = document.createElement('a'); link.href = '#/runtime/speech-runtimes';
      link.textContent = 'Runtime › Speech runtimes'; executableHelp.append(link);
    }
  }
  function openForm(nextMode, profile = null) {
    mode = nextMode; source = profile;
    form.dataset.mode = mode;
    $('speechProfileKind').disabled = mode !== 'create';
    $('speechProfileKind').value = profile?.kind || 'synthesis';
    $('speechProfileId').readOnly = mode === 'edit';
    $('speechProfileId').value = mode === 'edit' ? profile.id : '';
    $('speechProfileName').value = mode === 'create' ? '' : mode === 'duplicate' ? `Copy of ${profile.name}` : profile.name;
    $('speechProfileNotes').value = profile?.notes || '';
    $('speechProfileFormTitle').textContent = mode === 'create' ? 'New speech profile' :
      `${mode === 'edit' ? 'Edit' : 'Duplicate'} ${profile.name}`;
    $('speechProfileSave').textContent = mode === 'duplicate' ? 'Duplicate profile' :
      mode === 'edit' ? 'Save profile' : 'Create profile';
    fillForm(profile);
    updateSaveLabel();
    for (const id of ['speechProfileExecutable', 'speechProfileWeights', 'speechProfileAddArgument'])
      $(id).disabled = mode === 'duplicate';
    $('speechProfileCard').disabled = mode === 'duplicate' || $('speechProfileKind').value !== 'synthesis';
    if (mode === 'duplicate')
      for (const row of $('speechProfileArguments').children)
        for (const control of row.children) control.disabled = true;
    $('speechProfileNotes').disabled = mode === 'duplicate';
    clearError(); form.hidden = false;
    $(mode === 'edit' ? 'speechProfileName' : 'speechProfileId').focus();
  }
  function servedChanged(record) {
    return record.speech_executable_id !== source.speech_executable_id ||
      JSON.stringify(record.weights) !== JSON.stringify(source.weights) ||
      (record.kind === 'synthesis' && record.card_id !== source.card_id) ||
      JSON.stringify(record.arguments) !== JSON.stringify(source.arguments);
  }
  function updateSaveLabel() {
    if (mode === 'edit' && source) {
      try { $('speechProfileSave').textContent = servedChanged(payload()) ? 'Review…' : 'Save profile'; }
      catch (_) { $('speechProfileSave').textContent = 'Save profile'; }
    }
  }
  function payload() {
    const kind = $('speechProfileKind').value;
    const record = {kind, profile_id: $('speechProfileId').value, name: $('speechProfileName').value,
      speech_executable_id: $('speechProfileExecutable').value,
      weights: JSON.parse($('speechProfileWeights').value || 'null'),
      arguments: [...$('speechProfileArguments').children].filter(row => row.children[0].value || row.children[1].value)
        .map(row => ({tool: row.dataset.tool, ...(row.dataset.catalogue && row.dataset.catalogue === row.children[0].value
          ? {catalogue: row.dataset.catalogue} : {option: row.children[0].value}),
          value: row.dataset.valueEdited === 'true' ? row.children[1].value : JSON.parse(row.dataset.value)})),
      notes: $('speechProfileNotes').value};
    if (kind === 'synthesis') record.card_id = $('speechProfileCard').value;
    return record;
  }
  function reviewContent(result) {
    const content = document.createElement('div');
    for (const changed of result.preview.changed_values) {
      const line = document.createElement('p');
      line.textContent = `${changed.label}: ${display(changed.old)} → ${display(changed.new)}`;
      content.append(line);
    }
    for (const affected of result.preview.affected || []) {
      const line = document.createElement('p');
      const state = affected.state?.state === 'candidate' && affected.state.changed_since_qualification
        ? 'candidate — changed since qualification' : affected.state?.state || 'unavailable';
      line.textContent = `Bundle ${affected.name || affected.id}: ${state}`; content.append(line);
    }
    if (result.availability && !result.availability.available) {
      const line = document.createElement('p'); line.textContent = `Unavailable: ${result.availability.reason}`;
      content.append(line);
    }
    return content;
  }
  function display(value) {
    return value === null || value === undefined ? 'None' :
      typeof value === 'object' ? JSON.stringify(value) : String(value);
  }
  async function submit(event) {
    event.preventDefault(); clearError();
    try {
      if (mode === 'duplicate') {
        await request({action: 'duplicate', profile_id: source.id, kind: source.kind,
          new_id: $('speechProfileId').value, name: $('speechProfileName').value});
        form.hidden = true; await refresh(); return;
      }
      const record = payload();
      if (mode === 'edit' && servedChanged(record)) {
        const preview = await request({action: 'preview', record});
        if (['served-change', 'unavailable'].includes(preview.preview.kind)) {
          window.matutoPopup.open({title: `Review changes to ${record.name}`,
            content: reviewContent(preview), safe: 'Cancel', confirm: 'Save profile',
            confirmAction: async () => {
              await request({action: 'save', mode, record, confirm: true, review_token: preview.review_token});
              form.hidden = true; clearError(); refresh().catch(fail);
            }});
          return;
        }
      }
      await request({action: 'save', mode, record});
      form.hidden = true; await refresh();
    } catch (error) { fail(error); }
  }
  function remove(profile) {
    const content = document.createElement('p');
    content.textContent = `Remove ${profile.name}? Qualification records and runs remain browsable.`;
    window.matutoPopup.open({title: 'Remove speech profile', content, safe: 'Cancel', confirm: 'Remove profile',
      confirmAction: async () => { await request({action: 'remove', kind: profile.kind, profile_id: profile.id});
        refresh().catch(fail); }});
  }
  async function refresh(isCurrent = () => true) {
    const fresh = await request();
    if (!isCurrent()) return false;
    data = fresh;
    render();
    return true;
  }
  $('speechProfileNew').addEventListener('click', async () => {
    const generation = ++routeGeneration;
    handoffAcquisitionId = null;
    $('speechProfileWeightsHelp').textContent = '';
    try {
      if (!await refresh(() => generation === routeGeneration) || generation !== routeGeneration) return;
      openForm('create');
    } catch (error) { fail(error); }
  });
  $('speechProfileKind').addEventListener('change', () => {
    const weights = $('speechProfileWeights').value;
    fillForm(null);
    $('speechProfileWeights').value = weights;
  });
  form.addEventListener('input', updateSaveLabel);
  form.addEventListener('change', updateSaveLabel);
  $('speechProfileAddArgument').addEventListener('click', () => { argumentRow(); updateSaveLabel(); });
  $('speechProfileCancel').addEventListener('click', () => { form.hidden = true; clearError(); });
  form.addEventListener('submit', submit);
  async function routeHandoff() {
    const hash = window.location.hash, generation = ++routeGeneration;
    const isCurrent = () => generation === routeGeneration && window.location.hash === hash;
    if (!await refresh(isCurrent) || !isCurrent()) return;
    const [route, query = ''] = hash.split('?');
    if (route !== '#/profiles/speech-profiles') return;
    const params = new URLSearchParams(query);
    const acquisition_id = params.get('acquisition_id'), file_name = params.get('file_name');
    if (!(params.has('new') || params.has('acquisition_id')) || !acquisition_id) return;
    const candidates = data.weights.filter(item => item.pick.acquisition_id === acquisition_id);
    const selected = file_name && candidates.find(item => item.pick.file_name === file_name);
    if (!candidates.length || (file_name && !selected)) {
      form.hidden = true;
      fail(new Error('That acquisition file set is no longer available as complete speech weights.'));
      return;
    }
    handoffAcquisitionId = acquisition_id;
    $('speechProfileWeightsHelp').textContent = selected ? '' :
      'Choose the weights file from this acquisition record.';
    openForm('create');
    $('speechProfileWeights').value = selected ? JSON.stringify(selected.pick) : '';
  }
  window.addEventListener('hashchange', () => {
    routeGeneration++;
    if (window.location.hash.split('?')[0] === '#/profiles/speech-profiles') routeHandoff().catch(fail);
  });
  routeHandoff().catch(fail);
})();
