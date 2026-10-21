'use strict';

(() => {
  const app = document.getElementById('context-search-app');
  const form = document.getElementById('context-search-start');
  const recordSelect = form.elements.record;
  const rungSelect = form.elements.start_rung;
  const rungLabel = document.getElementById('context-search-rung-label');
  const button = form.querySelector('button[type="submit"]');
  const floor = document.getElementById('context-search-floor');
  const reason = document.getElementById('context-search-reason');
  const error = document.getElementById('context-search-error');
  const current = document.getElementById('context-search-current');
  const history = document.getElementById('context-search-history');
  let records = [];
  let serving = 'unknown';
  let addressedRecord = null;
  let refusal = '';
  let progressById = new Map();

  const node = (tag, text, attrs = {}) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
    return element;
  };
  const api = async (path, options = {}) => {
    const response = await fetch(path, {method: options.method || 'GET', cache: 'no-store',
      headers: options.method ? {'Content-Type': 'application/json', 'X-CSRF-Token': window.matutoCsrf || ''} : {},
      body: options.body === undefined ? undefined : JSON.stringify(options.body)});
    const type = response.headers.get('Content-Type') || '';
    const value = type.includes('application/json') ? await response.json() : null;
    if (!response.ok) throw Object.assign(new Error(value?.error || 'The request was refused.'), value);
    return value;
  };
  const show = (element, visible) => { element.hidden = !visible; };
  const labelFor = rung => `${Math.round(rung / 1024)}K`;

  function addressedId() {
    const query = (window.location.hash || '').split('?')[1] || '';
    return new URLSearchParams(query).get('record') || '';
  }

  function updateRungs() {
    const record = records.find(item => item.id === recordSelect.value);
    const previous = rungSelect.value;
    rungSelect.textContent = '';
    for (const rung of record?.rungs || []) rungSelect.append(node('option', labelFor(rung), {value: rung}));
    const retained = (record?.rungs || []).findIndex(rung => String(rung) === previous);
    rungSelect.selectedIndex = retained >= 0 ? retained : (record?.rungs.length ? 0 : -1);
    const noRungs = Boolean(record && !record.rungs.length);
    const belowFloor = Number.isInteger(record?.trained_context) && record.trained_context < 96 * 1024;
    const floorMessage = 'Context searches start at 96K; this model’s trained context is below 96K.';
    const unavailable = !records.length ? 'Choose a stored language profile or bundle.'
      : !record ? 'Choose a stored language profile or bundle.'
        : noRungs ? (belowFloor ? floorMessage : 'The model’s trained context could not be read, so no rung is available.') : '';
    floor.textContent = noRungs ? unavailable : '';
    const hasRungs = Boolean(record?.rungs.length);
    window.matutoReason(button, hasRungs ? unavailable : '');
    show(button, hasRungs);
    show(rungLabel, hasRungs);
    reason.textContent = noRungs ? refusal : unavailable || refusal;
  }

  function fillRecords() {
    const previous = recordSelect.value;
    recordSelect.textContent = '';
    for (const record of records)
      recordSelect.append(node('option', `${record.name} (${record.kind})`, {value: record.id, 'data-kind': record.kind}));
    let index = records.findIndex(record => record.id === previous);
    const addressed = addressedId();
    if (addressed && addressed !== addressedRecord) {
      addressedRecord = addressed;
      index = records.findIndex(record => record.id === addressed);
    }
    recordSelect.selectedIndex = index >= 0 ? index : (records.length ? 0 : -1);
    updateRungs();
  }

  function renderSearch(search) {
    const entry = node('article', undefined, {id: `context-search-${search.run_id}`});
    entry.append(node('h5', `Context search: ${search.record_label}`));
    const progress = progressById.get(search.run_id);
    if (search.outcome) {
      const summary = search.summary;
      const winner = summary?.outcome === 'winner' && summary.winner
        ? `${labelFor(summary.winner)}${summary.checked ? '' : ' — lowest rung, not double-checked'}` : 'no winner';
      entry.append(node('p', `Outcome: ${search.outcome}. Result: ${winner}.`));
    } else {
      entry.append(node('p', `State: running.${progress ? ` Phase: ${progress.phase || 'Not reported'}. Current step: ${progress.step || 'Not reported'}.` : ''}`));
    }
    for (const child of search.children || []) {
      const row = node('section', undefined, {id: `context-rung-${child.id}`});
      row.append(node('h6', `Rung ${child.label} — ${child.outcome}`));
      const marker = child.winner ? 'Winner.' : search.summary?.outcome === 'no winner' ? 'No winner.' : 'Not the winner.';
      row.append(node('p', marker));
      const checks = node('ul');
      for (const check of child.checks || []) checks.append(node('li',
        `${check.status}: ${check.name}${check.detail ? ` — ${check.detail}` : ''}`));
      row.append(checks);
      if (child.outcome) row.append(node('p', child.citable
        ? 'Citable for acceptance at the current fingerprint.'
        : `Not citable for acceptance: ${child.citation_reason || 'the record fingerprint differs'}`));
      if (child.winner && search.summary?.outcome === 'winner') {
        const record = records.find(item => item.id === child.record_id);
        const profileId = record?.language_id || child.record_id;
        const link = node('a', `Set context to ${child.label}`, {href:
          `#/profiles/language?edit=${encodeURIComponent(profileId)}&context=${encodeURIComponent(child.context)}`});
        row.append(link);
      }
      entry.append(row);
    }
    return entry;
  }

  const rendered = new Map();
  const emptyMessages = new Map();
  function render(searches) {
    const active = searches.filter(item => !item.outcome);
    const ended = searches.filter(item => item.outcome);
    const found = new Set(searches.map(item => item.run_id));
    for (const [id, view] of rendered) if (!found.has(id)) {
      view.entry.remove(); rendered.delete(id);
    }
    function update(container, search) {
      const signature = JSON.stringify([search, progressById.get(search.run_id)]);
      const previous = rendered.get(search.run_id);
      if (previous?.signature === signature) {
        if (previous.entry.parentNode !== container) container.append(previous.entry);
        return;
      }
      const entry = renderSearch(search);
      if (previous?.entry.parentNode === container) previous.entry.replaceWith(entry);
      else {
        previous?.entry.remove();
        container.append(entry);
      }
      rendered.set(search.run_id, {entry, signature});
    }
    function empty(container, items, text) {
      let message = emptyMessages.get(container);
      if (items.length) message?.remove();
      else {
        if (!message) { message = node('p', text); emptyMessages.set(container, message); }
        if (message.parentNode !== container) container.append(message);
      }
    }
    empty(current, active, 'Nothing running.');
    empty(history, ended, 'No ended context searches yet.');
    for (const item of active) update(current, item);
    for (const item of ended) update(history, item);
  }

  async function refresh() {
    try {
      const [data, runs, status] = await Promise.all([
        api('/api/context-search'), api('/api/runs'), api('/api/status').catch(() => null),
      ]);
      if (typeof runs.csrf === 'string') window.matutoCsrf = runs.csrf;
      records = data.records;
      progressById = new Map(runs.entries.filter(item => item.procedure === 'context_search')
        .map(item => [item.id, item.progress]));
      serving = status?.items?.find(item => item.name === 'serving')?.state || 'unknown';
      fillRecords();
      button.textContent = ['agentic', 'voice'].includes(serving) ? 'Stop serving and run…' : 'Start';
      render(data.searches);
      error.hidden = true;
      error.textContent = '';
    } catch (cause) {
      error.hidden = false;
      error.textContent = cause.message;
    }
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    refusal = '';
    reason.textContent = '';
    const record = records.find(item => item.id === recordSelect.value);
    const request = {procedure: 'context_search', record: recordSelect.value,
      start_rung: Number(rungSelect.value)};
    try {
      await api('/api/runs', {method: 'POST', body: {action: 'start', ...request}});
      await refresh();
    } catch (cause) {
      refusal = cause.message;
      reason.textContent = refusal;
      if (cause.offer) window.matutoPopup.preview({
        title: 'Stop serving and run?', stops: ['Serving; consumers lose the model'],
        starts: [`context search for ${record?.name || request.record}`],
        stays: ['Serving remains stopped after the run'], safe: 'Keep serving',
        confirm: 'Stop serving and run',
        confirmAction: async () => {
          await api('/api/runs/stop-serving', {method: 'POST', body: {confirm: true, ...request}});
          await refresh();
        },
      });
    }
  });
  recordSelect.addEventListener('change', () => { refusal = ''; updateRungs(); });
  rungSelect.addEventListener('change', () => { refusal = ''; reason.textContent = ''; });
  if (window.addEventListener) window.addEventListener('hashchange', () => {
    addressedRecord = null;
    fillRecords();
  });
  refresh();
  window.setInterval(refresh, 2000);
})();
