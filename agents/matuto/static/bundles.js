'use strict';

(() => {
  const form = document.getElementById('bundle-form');
  if (!form) return;
  const stylesheet = document.createElement('link');
  stylesheet.rel = 'stylesheet';
  stylesheet.href = '/bundles.css';
  document.head.append(stylesheet);
  const byName = name => form.elements.namedItem(name);
  const error = document.getElementById('bundle-error');
  const saveButton = document.getElementById('bundle-save');
  let sourceId = null;
  const profileNames = {};

  async function request(body) {
    const response = await fetch('/api/bundles', {
      method: body ? 'POST' : 'GET',
      headers: body ? {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || ''} : {},
      body: body ? JSON.stringify(body) : undefined,
      cache: 'no-store'
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'The request was refused.');
    return payload;
  }

  function showError(message) {
    error.textContent = message;
    error.hidden = false;
    error.focus();
  }
  function clearError() { error.textContent = ''; error.hidden = true; }
  function displayValue(value) {
    return value === null || value === undefined ? 'None' :
      typeof value === 'object' ? JSON.stringify(value) : String(value);
  }
  function reviewContent(data) {
    const review = document.createElement('div');
    for (const changed of data.preview.changed_values) {
      const line = document.createElement('p');
      const value = (field, item) => {
        const role = field.startsWith('members.') ? field.slice('members.'.length) : '';
        return role && typeof item === 'string'
          ? `${profileNames[role]?.get(item) || item} (${item})` : displayValue(item);
      };
      line.textContent = `${changed.label}: ${value(changed.field, changed.old)} → ${value(changed.field, changed.new)}`;
      review.append(line);
    }
    const state = document.createElement('p');
    const qualification = data.preview.state;
    const stateName = qualification?.state === 'candidate' && qualification.changed_since_qualification
      ? 'candidate — changed since qualification' : qualification?.state || 'unavailable';
    state.textContent = `Bundle state after saving: ${stateName}`;
    review.append(state);
    if (data.availability && !data.availability.available) {
      const availability = document.createElement('p');
      availability.textContent = `Unavailable: ${data.availability.reason}`;
      review.append(availability);
    }
    return review;
  }
  function fillOptions(profiles) {
    for (const role of ['language', 'synthesis', 'recognition']) {
      const select = byName(role);
      select.replaceChildren();
      profileNames[role] = new Map();
      for (const item of profiles[role] || []) {
        profileNames[role].set(item.id, item.name);
        const option = document.createElement('option');
        option.value = item.id;
        option.textContent = `${item.name} (${item.id})${item.available ? '' : ' — unavailable'}`;
        select.append(option);
      }
    }
  }
  function bundleCard(bundle) {
    const article = document.createElement('article');
    article.className = 'panel';
    const title = document.createElement('h4');
    title.textContent = bundle.name;
    article.append(title);
    const state = bundle.qualification.state === 'candidate' && bundle.qualification.changed_since_qualification
      ? 'candidate — changed since qualification' : bundle.qualification.state;
    const qualification = document.createElement('p');
    qualification.textContent = `Qualification: ${state}`;
    article.append(qualification);
    const available = document.createElement('p');
    available.textContent = bundle.available ? 'Available' : `Unavailable: ${bundle.reason}`;
    article.append(available);
    for (const role of ['language', 'synthesis', 'recognition']) {
      const member = bundle.members[role];
      const line = document.createElement('p');
      line.textContent = `${role[0].toUpperCase()}${role.slice(1)}: ${member.name} (${member.id})` +
        (member.available ? '' : ` — unavailable: ${member.reason}`);
      article.append(line);
    }
    const load = document.createElement('p');
    load.textContent = `Stated load: ${bundle.load_tokens} tokens; ${bundle.load_prompt}`;
    article.append(load);
    if (bundle.notes) {
      const notes = document.createElement('p');
      notes.textContent = `Notes: ${bundle.notes}`;
      article.append(notes);
    }
    const actions = document.createElement('div');
    actions.className = 'bundle-actions';
    for (const [label, callback] of [
      ['Edit', () => edit(bundle)],
      ['Duplicate…', () => duplicate(bundle)],
      ['Remove', () => remove(bundle)]
    ]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = label;
      button.addEventListener('click', callback);
      actions.append(button);
    }
    article.append(actions);
    return article;
  }
  async function refresh() {
    const result = await request();
    if (form.hidden) fillOptions(result.profiles);
    const list = document.getElementById('bundle-list');
    list.replaceChildren();
    for (const bundle of result.bundles) list.append(bundleCard(bundle));
  }
  function setMode(mode, bundle) {
    form.dataset.mode = mode;
    byName('mode').value = mode;
    sourceId = mode === 'duplicate' ? bundle.id : null;
    byName('bundle_id').value = mode === 'edit' ? bundle.id : '';
    byName('bundle_id').readOnly = mode === 'edit';
    byName('name').value = mode === 'create' ? '' : mode === 'duplicate' ? `Copy of ${bundle.name}` : bundle.name;
    for (const role of ['language', 'synthesis', 'recognition'])
      byName(role).value = bundle?.members?.[role]?.id || '';
    byName('load_tokens').value = bundle?.load_tokens || '';
    byName('load_prompt').value = bundle?.load_prompt || '';
    byName('notes').value = bundle?.notes || '';
    const duplicate = mode === 'duplicate';
    for (const name of ['language', 'synthesis', 'recognition', 'load_tokens', 'load_prompt', 'notes'])
      byName(name).disabled = duplicate;
    document.getElementById('bundle-form-title').textContent =
      mode === 'create' ? 'Create bundle' : mode === 'edit' ? `Edit ${bundle.name}` : `Duplicate ${bundle.name}`;
    saveButton.textContent = duplicate ? 'Duplicate bundle' : 'Save bundle';
    clearError();
    form.hidden = false;
    form.scrollIntoView({block: 'start'});
    byName(mode === 'edit' ? 'name' : 'bundle_id').focus();
  }
  function edit(bundle) { setMode('edit', bundle); }
  function duplicate(bundle) { setMode('duplicate', bundle); }
  function remove(bundle) {
    const content = document.createElement('p');
    content.textContent = `Remove ${bundle.name}? Its qualification records and runs will remain browsable.`;
    window.matutoPopup.open({title: 'Remove bundle', content, safe: 'Cancel', confirm: 'Remove bundle',
      confirmAction: async () => {
        await request({action: 'remove', bundle_id: bundle.id});
        clearError();
        refresh().catch(cause => showError(cause.message));
      }});
  }
  function values() {
    return {bundle_id: byName('bundle_id').value, name: byName('name').value,
      members: {language: byName('language').value, synthesis: byName('synthesis').value,
        recognition: byName('recognition').value}, load_tokens: Number(byName('load_tokens').value),
      load_prompt: byName('load_prompt').value, notes: byName('notes').value};
  }
  async function submit(event) {
    event.preventDefault();
    clearError();
    const mode = form.dataset.mode;
    try {
      if (mode === 'duplicate') {
        await request({action: 'duplicate', bundle_id: sourceId, new_id: byName('bundle_id').value,
          name: byName('name').value});
      } else {
        const record = values();
        if (mode === 'edit') {
          const data = await request({action: 'preview', ...record});
          if (['served-change', 'unavailable'].includes(data.preview.kind)) {
            window.matutoPopup.open({title: `Review changes to ${record.name}`,
              content: reviewContent(data), safe: 'Cancel', confirm: 'Save bundle',
              confirmAction: async () => {
                await request({action: 'save', mode, ...record, confirm: true,
                  review_token: data.review_token});
                form.hidden = true;
                clearError();
                refresh().catch(cause => showError(cause.message));
              }});
            return;
          }
        }
        await request({action: 'save', mode, ...record});
      }
      form.hidden = true;
      await refresh();
    } catch (cause) {
      showError(cause.message);
    }
  }

  document.getElementById('bundle-create').addEventListener('click', () => setMode('create', null));
  document.getElementById('bundle-cancel').addEventListener('click', () => { form.hidden = true; });
  form.addEventListener('submit', submit);
  window.addEventListener('hashchange', () => {
    if (window.location.hash.split('?')[0] === '#/profiles/bundles')
      refresh().catch(cause => showError(cause.message));
  });
  refresh().catch(cause => showError(cause.message));
})();
