'use strict';

(() => {
  const page = document.getElementById('languageProfiles');
  const list = document.getElementById('languageProfileList');
  if (!page || !list) return;
  const status = document.createElement('p');
  status.className = 'language-profile-action-status';
  status.setAttribute('role', 'status');
  page.append(status);

  async function request(body) {
    const response = await fetch('/api/language-profile-actions', {
      method: body ? 'POST' : 'GET',
      headers: body ? {'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || ''} : {},
      body: body ? JSON.stringify(body) : undefined,
      cache: 'no-store'
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'The request was refused.');
    return payload;
  }

  function actionButton(actions, label, disabledReason, callback) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    actions.append(button);
    reason(button, disabledReason);
    button.addEventListener('click', () => {
      if (button.getAttribute('aria-disabled') !== 'true') callback();
    });
  }

  function duplicate(profile) {
    const content = document.createElement('form');
    content.className = 'language-profile-duplicate-form';
    const fields = {};
    for (const [key, label, value] of [
      ['new_id', 'New stable ID', `${profile.profile_id}-copy`],
      ['name', 'Name', `Copy of ${profile.name}`],
      ['alias', 'Alias', `${profile.alias}-copy`],
    ]) {
      const wrapper = document.createElement('label');
      wrapper.textContent = label;
      const input = document.createElement('input');
      input.name = key;
      input.required = true;
      input.value = value;
      fields[key] = input;
      wrapper.append(input);
      content.append(wrapper);
    }
    window.matutoPopup.open({title: `Duplicate ${profile.name}`, content, safe: 'Cancel',
      confirm: 'Duplicate profile', confirmAction: async () => {
        await request({action: 'duplicate', profile_id: profile.profile_id,
          new_id: fields.new_id.value, name: fields.name.value, alias: fields.alias.value});
        status.textContent = `Created unpublished copy of ${profile.name}.`;
        window.dispatchEvent(new Event('language-profile-actions-changed'));
      }});
  }

  function candidatePublication(profile) {
    const content = document.createElement('p');
    content.textContent = `${profile.name} is not qualified. Consumers will be able to select it as ${profile.alias}. Publishing starts and loads nothing.`;
    window.matutoPopup.open({title: 'Publish a candidate?', content, safe: 'Cancel',
      confirm: 'Publish', confirmAction: async () => {
        await request({action: 'publish', profile_id: profile.profile_id,
          confirmation: `Publish ${profile.name} as not qualified`});
        status.textContent = `Published ${profile.name}; publishing starts and loads nothing.`;
        window.dispatchEvent(new Event('language-profile-actions-changed'));
      }});
  }

  async function publish(profile) {
    if (profile.qualification.state !== 'qualified') {
      candidatePublication(profile);
      return;
    }
    try {
      await request({action: 'publish', profile_id: profile.profile_id});
      status.textContent = `Published ${profile.name}; publishing starts and loads nothing.`;
      window.dispatchEvent(new Event('language-profile-actions-changed'));
    } catch (error) {
      if (/not qualified/i.test(error.message)) candidatePublication(profile);
      else status.textContent = error.message;
    }
  }

  function unpublish(profile) {
    const content = document.createElement('p');
    content.textContent = `Consumers can no longer select ${profile.alias} once the router reloads. Nothing is stopped.`;
    window.matutoPopup.open({title: `Unpublish ${profile.name}?`, content, safe: 'Cancel',
      confirm: 'Unpublish', confirmAction: async () => {
        await request({action: 'unpublish', profile_id: profile.profile_id});
        status.textContent = `Unpublished ${profile.name}.`;
        window.dispatchEvent(new Event('language-profile-actions-changed'));
      }});
  }

  function remove(profile) {
    const content = document.createElement('p');
    content.textContent = `Remove ${profile.name}? Its qualification records and runs will remain browsable in Runs & jobs.`;
    window.matutoPopup.open({title: 'Remove language profile?', content, safe: 'Cancel',
      confirm: 'Remove profile', confirmAction: async () => {
        await request({action: 'remove', profile_id: profile.profile_id});
        status.textContent = `Removed ${profile.name}; its history remains in Runs & jobs.`;
        window.dispatchEvent(new Event('language-profile-actions-changed'));
      }});
  }

  function draw(profiles) {
    for (const item of list.children) item.querySelector('.language-profile-record-actions')?.remove();
    for (const profile of profiles) {
      const item = [...list.children].find(row => row.getAttribute('data-profile-id') === profile.profile_id);
      if (!item) continue;
      const actions = document.createElement('div');
      actions.className = 'profile-actions language-profile-record-actions';
      actionButton(actions, 'Duplicate…', '', () => duplicate(profile));
      if (profile.available && profile.qualification.state === 'candidate') {
        const qualify = document.createElement('a');
        qualify.setAttribute('href', `#/runs?profile=${encodeURIComponent(profile.profile_id)}`);
        qualify.textContent = 'Qualify…';
        actions.append(qualify);
      }
      if (profile.published) {
        actionButton(actions, 'Unpublish…', profile.unpublish_reason, () => unpublish(profile));
      } else {
        actionButton(actions, 'Publish…', '', () => publish(profile));
      }
      actionButton(actions, 'Remove', profile.remove_reason, () => remove(profile));
      item.append(actions);
    }
  }

  async function refresh() {
    const data = await request();
    draw(data.profiles);
  }
  window.addEventListener('language-profile-list-rendered', () =>
    refresh().catch(error => { status.textContent = `Language profile actions are unavailable: ${error.message}`; }));
  refresh().catch(error => { status.textContent = `Language profile actions are unavailable: ${error.message}`; });
})();
