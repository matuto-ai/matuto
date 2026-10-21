'use strict';

(() => {
  const start = document.getElementById('bundle-runs-start');
  const bundleSelect = start.elements.bundle;
  const startButton = start.querySelector('button[type="submit"]');
  const reason = document.getElementById('bundle-runs-reason');
  const error = document.getElementById('bundle-runs-error');
  const current = document.getElementById('bundle-runs-current');
  const history = document.getElementById('bundle-runs-history');
  let bundles = [];
  let serving = 'unknown';
  let addressedBundle = null;
  let admissionRefusal = '';

  const node = (tag, text, attrs = {}) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
    return element;
  };
  const api = async (path, options = {}) => {
    const response = await fetch(path, {
      method: options.method || 'GET', cache: 'no-store',
      headers: options.method ? {'Content-Type': 'application/json', 'X-CSRF-Token': window.matutoCsrf || ''} : {},
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const contentType = response.headers.get('Content-Type') || '';
    const value = contentType.includes('application/json') ? await response.json() : null;
    if (!response.ok) throw Object.assign(new Error(value?.error || 'The request was refused.'), value);
    return value;
  };
  const show = (element, visible) => { element.hidden = !visible; };

  function selectAddressedBundle() {
    const query = (window.location.hash || '').split('?')[1] || '';
    const values = new URLSearchParams(query);
    const id = values.get('bundle') || '';
    if (id === addressedBundle) return;
    addressedBundle = id;
    if (!id) return;
    const index = bundles.findIndex(bundle => bundle.id === id);
    if (index >= 0) bundleSelect.selectedIndex = index;
  }

  function updateStartLabel() {
    startButton.textContent = ['agentic', 'voice'].includes(serving)
      ? 'Stop serving and run…' : 'Start';
  }

  function renderCheck(check, entry) {
    const detail = check.detail || '';
    const recognition = entry.procedure === 'bundle' && check.name === 'recognition'
      ? /^text (non-empty|empty|unavailable); matching words (\d+|unavailable); recognition time (\d+|unmeasured) ns; clip duration (\d+|unmeasured) ns$/.exec(detail)
      : null;
    const previousRecognition = entry.procedure === 'bundle' && check.name === 'recognition'
      ? /^returned (\d+) matching words in (\d+|unmeasured) ns; clip duration (\d+|unmeasured) ns$/.exec(detail)
      : null;
    const realTime = entry.procedure === 'bundle' && check.name === 'real-time'
      ? /^audio duration (\d+|unmeasured|None) ns; synthesis wall time (\d+|unmeasured|None) ns$/.exec(detail)
      : null;
    const item = node('li');
    item.append(node('strong', `${check.status}: ${check.name}`));
    let observation = '';
    if (recognition) {
      const responseTime = recognition[3] === 'unmeasured' ? 'unavailable' : `${recognition[3]} ns`;
      const clipDuration = recognition[4] === 'unmeasured' ? 'unavailable' : `${recognition[4]} ns`;
      const returnedText = recognition[1] === 'unavailable' ? 'text result unavailable'
        : `${recognition[1]} text returned`;
      item.append(node('span', ` — ${returnedText}; recognition time ${responseTime}; clip duration ${clipDuration}`));
      if (recognition[2] !== 'unavailable')
        observation = `Observation — ${recognition[2]} recognised words.`;
    } else if (previousRecognition) {
      const responseTime = previousRecognition[2] === 'unmeasured' ? 'unavailable' : `${previousRecognition[2]} ns`;
      const clipDuration = previousRecognition[3] === 'unmeasured' ? 'unavailable' : `${previousRecognition[3]} ns`;
      item.append(node('span', ` — text result unavailable; recognition time ${responseTime}; clip duration ${clipDuration}`));
      if (previousRecognition[2] !== 'unmeasured')
        observation = `Observation — ${previousRecognition[1]} recognised words.`;
    } else if (realTime) {
      const duration = Number(realTime[1]);
      const wall = Number(realTime[2]);
      const measured = realTime[1] !== 'unmeasured' && realTime[1] !== 'None'
        && realTime[2] !== 'unmeasured' && realTime[2] !== 'None' && duration > 0 && wall > 0;
      const ratio = measured ? duration / wall : NaN;
      const factor = Number.isFinite(ratio) ? String(ratio) : 'unavailable';
      const audio = measured || realTime[1] !== 'unmeasured' && realTime[1] !== 'None'
        ? `${realTime[1]} ns` : 'unavailable';
      const synthesis = realTime[2] !== 'unmeasured' && realTime[2] !== 'None'
        ? `${realTime[2]} ns` : 'unavailable';
      item.append(node('span', ` — real-time factor ${factor}; pass requires > 1.0 (audio duration ${audio}; synthesis wall time ${synthesis})`));
    } else if (detail) {
      item.append(node('span', ` — ${detail}`));
    }
    return {item, observation};
  }

  function renderEntry(entry) {
    const row = node('article', undefined, {id: `bundle-run-${entry.id}`});
    row.append(node('h5', `Bundle run: ${entry.record || entry.purpose}`));
    row.append(node('p', entry.outcome ? `Outcome: ${entry.outcome}.` : `State: ${entry.state}.`));
    if (entry.progress) {
      row.append(node('p', `Phase: ${entry.progress.phase || 'Not reported'}. Current step: ${entry.progress.step || 'Not reported'}.`));
    }
    if (entry.checks?.length) {
      const checks = node('ul');
      const observations = [];
      for (const check of entry.checks) {
        const rendered = renderCheck(check, entry);
        checks.append(rendered.item);
        if (rendered.observation) observations.push(rendered.observation);
      }
      row.append(checks);
      for (const observation of observations) row.append(node('p', observation));
    }
    return row;
  }

  function renderEntries(entries) {
    current.textContent = '';
    history.textContent = '';
    const active = entries.filter(entry => !entry.outcome && entry.procedure === 'bundle');
    const ended = entries.filter(entry => entry.outcome && entry.procedure === 'bundle');
    for (const entry of active) current.append(renderEntry(entry));
    for (const entry of ended) history.append(renderEntry(entry));
    if (!active.length) current.append(node('p', 'Nothing running.'));
    if (!ended.length) history.append(node('p', 'No ended bundle runs yet.'));
  }

  function render(data) {
    bundles = data.records.filter(record => record.kind === 'bundle');
    const selected = bundleSelect.value;
    bundleSelect.textContent = '';
    for (const bundle of bundles)
      bundleSelect.append(node('option', bundle.name, {value: bundle.id}));
    const selectedIndex = bundles.findIndex(bundle => bundle.id === selected);
    bundleSelect.selectedIndex = selectedIndex >= 0 ? selectedIndex : (bundles.length ? 0 : -1);
    selectAddressedBundle();
    renderEntries(data.entries);
    const unavailable = bundles.length ? '' : 'Create a bundle before starting a bundle run.';
    window.matutoReason(startButton, unavailable);
    reason.textContent = unavailable || admissionRefusal;
    updateStartLabel();
  }

  async function refresh() {
    try {
      const [data, status] = await Promise.all([
        api('/api/runs'), api('/api/status').catch(() => null),
      ]);
      if (typeof data.csrf === 'string') window.matutoCsrf = data.csrf;
      serving = status?.items?.find(item => item.name === 'serving')?.state || 'unknown';
      render(data);
      error.hidden = true;
      error.textContent = '';
    } catch (cause) {
      error.hidden = false;
      error.textContent = cause.message;
    }
  }

  start.addEventListener('submit', async event => {
    event.preventDefault();
    admissionRefusal = '';
    reason.textContent = '';
    const request = {procedure: 'bundle', record: bundleSelect.value};
    try {
      await api('/api/runs', {method: 'POST', body: {action: 'start', ...request}});
      await refresh();
    } catch (cause) {
      admissionRefusal = cause.message;
      reason.textContent = admissionRefusal;
      if (cause.offer) {
        const bundle = bundles.find(item => item.id === request.record);
        window.matutoPopup.preview({
          title: 'Stop serving and run?', stops: ['Serving; consumers lose the model'],
          starts: [`bundle for ${bundle?.name || request.record}`],
          stays: ['Serving remains stopped after the run'], safe: 'Keep serving',
          confirm: 'Stop serving and run',
          confirmAction: async () => {
            await api('/api/runs/stop-serving', {method: 'POST', body: {confirm: true, ...request}});
            await refresh();
          },
        });
      }
    }
  });
  bundleSelect.addEventListener('change', () => {
    admissionRefusal = '';
    reason.textContent = '';
  });
  if (window.addEventListener) window.addEventListener('hashchange', selectAddressedBundle);
  refresh();
  window.setInterval(refresh, 2000);
})();
