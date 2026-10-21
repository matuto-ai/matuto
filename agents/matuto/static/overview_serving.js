'use strict';

(() => {
  const headline = document.getElementById('serving-headline');
  const error = document.getElementById('serving-error');
  const details = document.getElementById('serving-details');
  const controls = document.getElementById('serving-controls');
  const current = document.getElementById('overview-current-work');
  const currentSection = current && current.parentElement;
  const currentDetail = document.getElementById('overview-current-work-detail');
  const currentLink = document.getElementById('overview-current-work-link');
  if (!headline || !details || !controls) return;

  const node = (tag, text, attributes = {}) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    return element;
  };
  const request = async (path, method, body) => {
    let response;
    try {
      response = await fetch(path, {method, cache: 'no-store',
        headers: method ? {'Content-Type': 'application/json', 'X-CSRF-Token': window.matutoCsrf || ''} : {},
        body: body === undefined ? undefined : JSON.stringify(body)});
    } catch (_) { throw new Error('Matuto is not answering.'); }
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'The request was refused.');
    return value;
  };
  let detailsKey = null;
  let controlsKey = null;
  let data;
  let refreshPromise = null;
  let refreshAgain = false;
  let pollTimer = null;

  function list(title, values) {
    const section = node('section');
    section.append(node('h4', title));
    const items = node('ul');
    for (const value of values) items.append(node('li', value));
    section.append(items);
    return section;
  }

  function renderDetails(value) {
    details.replaceChildren();
    if (value.reason) details.append(node('p', value.reason));
    if (value.state === 'voice' && value.bundle) details.append(node('p', `Voice bundle: ${value.bundle}`));
    else if (value.model) details.append(node('p', `Loaded model: ${value.model}`));
    if (value.runtime) details.append(node('p',
      `Runtime: ${value.runtime.build}; backend ${value.runtime.backend}; card ${value.runtime.card}`));
    else details.append(node('p', 'No runtime is selected.'));
    if (value.profiles.length) {
      const section = node('section');
      section.append(node('h4', 'Published language profiles'));
      const records = node('ul');
      for (const profile of value.profiles) {
        const qualification = profile.qualification;
        const label = qualification.state === 'qualified' ? 'Qualified'
          : qualification.state === 'candidate' && qualification.changed_since_qualification
            ? 'Candidate (changed since qualification)'
            : qualification.state === 'candidate' ? 'Candidate' : 'Unavailable';
        const row = node('li');
        row.append(node('span', `${profile.name} — ${label}`));
        if (qualification.state === 'candidate' || qualification.state === 'changed') {
          row.append(node('span', ' '));
          const link = node('a', 'Qualify…', {href: `#/runs?profile=${encodeURIComponent(profile.profile_id)}`});
          link.className = 'overview-qualify';
          row.append(link);
        }
        records.append(row);
      }
      section.append(records);
      details.append(section);
    } else if (value.state !== 'voice') {
      details.append(node('p', 'Nothing published.'));
      const link = node('a', 'Profiles › Language profiles', {href: '#/profiles/language'});
      details.append(link);
    }
    if (value.parts.length)
      details.append(list('Voice parts', value.parts.map(part =>
        `${part.role || 'Voice'} — ${part.profile}: ${part.state}${part.stray ? ' (not in the serving record)' : ''}`)));
    if (value.addresses.length) {
      details.append(list('Same-machine addresses', value.addresses.map(item => `${item.label}: ${item.address}`)));
    }
  }

  function refreshCurrent(value) {
    if (!current) return;
    if (!value) { current.hidden = true; if (currentSection) currentSection.hidden = true; return; }
    current.hidden = false;
    if (currentSection) currentSection.hidden = false;
    currentDetail.textContent = value.label;
    currentLink.href = value.href;
  }

  async function refreshOnce() {
    try {
      const result = await request('/api/overview/serving');
      if (!isOverview()) return;
      data = result;
      headline.textContent = result.headline;
      headline.dataset.state = result.state;
      error.hidden = true;
      const nextDetailsKey = JSON.stringify([result.state, result.reason, result.bundle, result.model,
        result.runtime, result.addresses, result.parts, result.profiles]);
      if (nextDetailsKey !== detailsKey) {
        renderDetails(result);
        detailsKey = nextDetailsKey;
      }
      refreshCurrent(result.currentWork);
      const nextControlsKey = JSON.stringify([result.controls, result.bundles, result.publishLink]);
      if (nextControlsKey !== controlsKey) {
        renderControls(result.controls);
        controlsKey = nextControlsKey;
      }
    } catch (cause) {
      if (!isOverview()) return;
      error.textContent = `Serving status unavailable: ${cause.message}`;
      error.hidden = false;
      headline.textContent = 'Serving state is unknown.';
      controls.replaceChildren();
      controlsKey = null;
    }
  }

  function refresh() {
    if (refreshPromise) {
      refreshAgain = true;
      return refreshPromise;
    }
    const task = (async () => {
      do {
        refreshAgain = false;
        await refreshOnce();
      } while (refreshAgain && isOverview());
    })();
    refreshPromise = task.finally(() => { refreshPromise = null; });
    return refreshPromise;
  }

  function renderControls(entries) {
    controls.replaceChildren();
    const group = node('div');
    for (const entry of entries) {
      const button = node('button', entry.label, {type: 'button'});
      group.append(button);
      window.matutoReason(button, entry.reason);
      if (entry.name === 'start_agentic' && data.publishLink) {
        const note = node('span', 'Open ');
        note.className = 'reason';
        note.append(node('a', 'Profiles › Language profiles', {href: '#/profiles/language'}));
        group.append(note);
      }
      if (entry.name === 'serve_voice') {
        const picker = node('select', undefined, {'aria-label': 'Voice bundle'});
        const choices = data.bundles;
        for (const bundle of choices.filter(item => item.offered))
          picker.append(node('option', bundle.name, {value: bundle.id}));
        picker.hidden = entry.reason !== '' || !picker.children.length;
        group.append(picker);
        const unavailable = choices.filter(item => !item.offered);
        if (unavailable.length) {
          const reasons = node('ul');
          for (const bundle of unavailable)
            reasons.append(node('li', `${bundle.name}: ${bundle.reasons.join(' ')}`));
          group.append(reasons);
        }
        button.addEventListener('click', () => activate(entry, picker.value));
      } else {
        button.addEventListener('click', () => activate(entry));
      }
    }
    controls.append(group);
  }

  async function activate(entry, bundle) {
    if (entry.reason) {
      error.textContent = entry.reason;
      error.hidden = false;
      return;
    }
    if (entry.name === 'start_agentic') {
      try { const result = await request(entry.href, 'POST', {});
        if (!result.healthy) error.textContent = `Agentic serving is unhealthy: ${result.reason || result.state}`;
        await refresh();
      } catch (cause) {
        await refresh();
        const button = [...controls.querySelectorAll('button')].find(item => item.textContent === entry.label);
        if (button) window.matutoReason(button, cause.message);
        error.textContent = cause.message;
        error.hidden = false;
      }
      return;
    }
    const body = entry.name === 'serve_voice' ? {confirm: false, bundle} : {confirm: false};
    try {
      const preview = await request(entry.href, 'POST', body);
      if (preview.acted || !preview.preview) { await refresh(); return; }
      const content = entry.name === 'serve_voice' ? voicePicker(bundle) :
      entry.name === 'restart' ? node('p', 'The model unloads until the next request loads it.') : undefined;
      window.matutoPopup.preview({title: previewTitle(entry.name),
        stops: preview.preview.stops, starts: preview.preview.starts, stays: preview.preview.stays,
        safe: preview.preview.safe_choice || 'Cancel', confirm: confirmLabel(entry.name), content,
        returnFocus: () => [...controls.querySelectorAll('button')]
          .find(item => item.textContent === entry.label) || document.getElementById('heading-overview'),
        confirmAction: async () => {
          try {
            const completed = await request(entry.href, 'POST', entry.name === 'serve_voice'
              ? {confirm: true, bundle} : {confirm: true});
            if (!completed.acted || completed.stopped === false)
              throw new Error(completed.reason || 'The control was not carried out.');
            await refresh();
          } catch (cause) {
            await refresh();
            throw cause;
          }
        }});
    } catch (cause) {
      await refresh();
      const button = [...controls.querySelectorAll('button')].find(item => item.textContent === entry.label);
      if (button) window.matutoReason(button, cause.message);
      error.textContent = cause.message;
      error.hidden = false;
    }
  }

  function voicePicker(selected) {
    const content = node('div');
    const heading = node('p', 'Choose a qualified, available bundle. Candidate and unavailable bundles cannot be served.');
    content.append(heading);
    const chosen = data.bundles.find(item => item.id === selected);
    if (chosen) content.append(node('p', `Selected bundle: ${chosen.name}`));
    for (const item of data.bundles.filter(bundle => !bundle.offered))
      content.append(node('p', `${item.name}: ${item.reasons.join(' ')}`));
    return content;
  }

  function previewTitle(name) {
    return ({stop_serving: 'Stop serving?', restart: 'Restart serving?', serve_voice: 'Serve voice?',
      switch_to_agentic: 'Switch to agentic serving?'})[name] || 'Confirm serving change';
  }
  function confirmLabel(name) {
    return ({stop_serving: 'Stop serving', restart: 'Restart', serve_voice: 'Serve voice',
      switch_to_agentic: 'Switch to agentic serving'})[name] || 'Confirm';
  }

  const isOverview = () => (window.location.hash || '').replace(/^#\/?/, '').split('/')[0] === 'overview'
    || !(window.location.hash || '').replace(/^#\/?/, '').split('/')[0];
  function schedulePoll() {
    if (pollTimer || !isOverview()) return;
    pollTimer = window.setTimeout(async () => {
      pollTimer = null;
      if (!isOverview()) return;
      await refresh();
      schedulePoll();
    }, 2000);
  }

  document.addEventListener('DOMContentLoaded', () => {
    if (isOverview()) refresh().finally(schedulePoll);
  });
  window.addEventListener('hashchange', () => {
    if (isOverview()) return refresh().finally(schedulePoll);
  });
})();
