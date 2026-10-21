'use strict';

(() => {
  const current = document.getElementById('runs-current');
  const history = document.getElementById('runs-history');
  const start = document.getElementById('runs-start');
  const error = document.getElementById('runs-error');
  const startReason = document.getElementById('runs-start-reason');
  const acceptanceRecord = document.getElementById('acceptance-record');
  const acceptanceReview = document.getElementById('acceptance-review');
  const acceptanceContent = document.getElementById('acceptance-content');
  const procedureSelect = start.elements.procedure;
  const recordSelect = start.elements.record;
  const languageProcedures = new Set(['verify', 'smoke', 'bench', 'serve', 'wq']);
  const wqLimitNames = ['first_byte_seconds', 'short_total_seconds', 'generation_tokens_per_second',
    'vram_mb', 'rss_kib', 'cancel_recovery_seconds', 'restart_seconds', 'long_execution_seconds',
    'retrieval_samples', 'retrieval_generation_tokens'];
  const wqLimitsSection = document.getElementById('runs-wq-limits');
  const wqFields = Object.fromEntries(wqLimitNames.map(name => {
    const input = document.getElementById(`wq-${name}`);
    input.setAttribute('aria-describedby', `wq-error-${name}`);
    return [name, {input, error: document.getElementById(`wq-error-${name}`)}];
  }));
  for (const {input, error} of Object.values(wqFields)) {
    input.addEventListener('invalid', () => {
      input.setAttribute('aria-invalid', 'true');
      error.textContent = input.validationMessage;
    });
  }
  const startButton = start.querySelector('button[type="submit"]');
  const views = new Map();
  const actionRefusals = new Map();
  const downloadStates = new Map();
  const emptyMessages = new Map();
  let admissionRefusal = '';
  let serving = 'unknown';
  let addressedRecord = null;
  let languageRecords = [];
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
  const format = value => value ? new Date(value).toLocaleString() : 'Unknown';
  const show = (element, visible) => { element.hidden = !visible; };

  function viewFor(work) {
    let view = views.get(work.id);
    if (view) return view;
    const row = node('article', undefined, {id: `work-${work.id}`, tabindex: '-1'});
    const heading = node('h5');
    const summary = node('p');
    const record = node('p');
    const progress = node('p');
    const log = node('pre', '', {class: 'runs-log'});
    const checks = node('ul');
    const address = node('p');
    const stop = node('button', '', {type: 'button'});
    const resume = node('button', 'Resume', {type: 'button'});
    const discard = node('button', 'Discard partial download…', {type: 'button'});
    const actionReason = node('p');
    const download = node('a', '', {class: 'runs-evidence'});
    const remove = node('button', 'Delete…', {type: 'button'});
    view = {row, heading, summary, record, progress, log, checks, address, stop, resume, discard, actionReason, download, remove, work};
    stop.addEventListener('click', () => window.matutoPopup.preview({
      title: view.work.kind === 'download' ? 'Cancel download?' : 'Stop this work?',
      stops: [`${view.work.id}: ${view.work.kind} for ${view.work.purpose}`], starts: [],
      stays: view.work.kind === 'download' ? ['Its partial files, kept for Resume'] : [],
      safe: 'Keep running', confirm: view.work.kind === 'download' ? 'Cancel download' : 'Stop',
      confirmAction: async () => {
        await api('/api/runs', {method: 'POST', body: {action: 'stop', id: view.work.id}});
        window.setTimeout(refresh, 0);
      },
    }));
    resume.addEventListener('click', async () => {
      try {
        await api('/api/acquisition', {method: 'POST', body: {action: 'resume', job_id: view.work.id}});
        actionRefusals.delete(`${view.work.id}:resume`);
        window.setTimeout(refresh, 0);
      } catch (cause) {
        actionRefusals.set(`${view.work.id}:resume`, `Resume refused: ${cause.message}`);
        view.actionReason.textContent = actionRefusals.get(`${view.work.id}:resume`);
      }
    });
    discard.addEventListener('click', async () => {
      try {
        const preview = await api('/api/acquisition', {method: 'POST', body: {action: 'discard-preview', job_id: view.work.id}});
        actionRefusals.delete(`${view.work.id}:discard`);
        discardPreview(view.work.id, preview);
      } catch (cause) {
        actionRefusals.set(`${view.work.id}:discard`, `Discard refused: ${cause.message}`);
        view.actionReason.textContent = actionRefusals.get(`${view.work.id}:discard`);
      }
    });
    remove.addEventListener('click', () => window.matutoPopup.open({
      title: `Delete run ${view.work.id}?`, safe: 'Keep run', confirm: 'Delete run',
      confirmAction: async () => {
        await api('/api/runs', {method: 'POST', body: {action: 'delete', id: view.work.id}});
        window.setTimeout(refresh, 0);
      },
    }));
    row.append(heading, summary, record, progress, log, checks, address, stop, resume, discard, actionReason, download, remove);
    views.set(work.id, view);
    return view;
  }

  function updateView(work) {
    const view = viewFor(work);
    view.work = work;
    const downloadState = downloadStates.get(work.id);
    const contextSearch = work.context_search;
    view.heading.textContent = contextSearch?.role === 'search' ? `Context search: ${work.record}`
      : contextSearch?.role === 'rung' ? `Context-search rung ${contextSearch.rung}: ${work.record}`
        : work.kind === 'download' ? `download: ${downloadState?.purpose || 'model files'}` : `${work.kind}: ${work.purpose}`;
    view.summary.textContent = `${work.outcome ? `Outcome: ${work.outcome}` : `State: ${work.state}`}.`
      + (contextSearch ? ` Search result: ${contextSearch.result}.${contextSearch.winner ? ' Winning rung.' : ''}` : '')
      + ` Started: ${format(work.started_at)}.`;
    view.record.textContent = `Record: ${work.record}; procedure: ${work.procedure}.`;
    show(view.record, Boolean(work.record));
    show(view.progress, Boolean(work.progress));
    if (work.progress?.unknown) view.progress.textContent = work.progress.unknown;
    else if (work.kind === 'download' && downloadState && !work.outcome && downloadState.phase) {
      view.progress.textContent = `Phase: ${downloadState.phase}. Current file: ${downloadState.file || 'Not reported'}. Transferred: ${downloadState.transferred ?? 'Not reported'} of ${downloadState.total ?? 'Not reported'} bytes. Elapsed: ${Math.floor(downloadState.progress?.elapsed || 0)} seconds.`;
    } else if (work.kind === 'download' && work.outcome) {
      view.progress.textContent = downloadState?.reason ? `Reason: ${downloadState.reason}` : `Outcome: ${work.outcome}.`;
    } else if (work.progress) view.progress.textContent = `Phase: ${work.progress.phase || 'Not reported'}. Current step: ${work.progress.step || 'Not reported'}. Elapsed: ${Math.floor(work.progress.elapsed)} seconds.`;
    view.log.textContent = work.progress?.log || '';
    show(view.log, Boolean(work.progress?.log));
    view.checks.textContent = '';
    for (const check of work.checks || []) view.checks.append(node('li', `${check.status}: ${check.name}${check.detail ? ` — ${check.detail}` : ''}`));
    show(view.checks, Boolean(work.checks?.length));
    view.address.textContent = work.address || 'Temporary server address is recorded in the run output.';
    show(view.address, work.kind === 'serve' && work.state === 'running');
    view.stop.textContent = work.kind === 'download' ? 'Cancel download…' : 'Stop…';
    show(view.stop, !work.outcome && (work.kind === 'download'
      ? work.state === 'running' : work.state !== 'unknown'));
    show(view.resume, Boolean(downloadState?.partials && work.outcome));
    show(view.discard, Boolean(downloadState?.partials && work.outcome));
    view.actionReason.textContent = actionRefusals.get(`${work.id}:resume`)
      || actionRefusals.get(`${work.id}:discard`) || '';
    view.download.textContent = `Download evidence for run ${work.id}`;
    view.download.setAttribute('href', `/api/run-evidence?id=${encodeURIComponent(work.id)}`);
    show(view.download, Boolean(work.record && work.outcome));
    show(view.remove, Boolean(work.record && work.outcome));
    return view.row;
  }

  function fill(container, items, emptyText) {
    const rows = new Set(items.map(item => views.get(item.id)?.row).filter(Boolean));
    const knownRows = new Set([...views.values()].map(view => view.row));
    for (const child of [...container.children]) {
      if ((!knownRows.has(child) || !rows.has(child)) && child !== emptyMessages.get(container)) {
        if (child.contains(document.activeElement)) startButton.focus();
        child.remove();
      }
    }
    for (const item of items) {
      const row = updateView(item);
      if (row.parentNode !== container) container.append(row);
    }
    let empty = emptyMessages.get(container);
    if (items.length) {
      if (empty?.parentNode === container) empty.remove();
    } else {
      if (!empty) {
        empty = node('p', emptyText);
        emptyMessages.set(container, empty);
      }
      if (empty.parentNode !== container) container.append(empty);
    }
  }

  function updateSelect(select, items, label) {
    const signature = JSON.stringify(items);
    if (select.getAttribute('data-options') === signature) return;
    const selected = select.value;
    const selectedOption = select.selectedOptions[0];
    const selectedKind = selectedOption?.getAttribute('data-kind');
    select.textContent = '';
    for (const item of items) select.append(node('option', label(item), {value: item.id, 'data-kind': item.kind || ''}));
    const index = items.findIndex(item => item.id === selected
      && (!selectedOption || (item.kind || '') === selectedKind));
    select.selectedIndex = index < 0 ? (items.length ? 0 : -1) : index;
    select.setAttribute('data-options', signature);
  }

  function selectAddressedRecord() {
    const match = /[?&](?:profile|record)=([^&]+)/.exec(window.location.hash || '');
    let id = '';
    try { if (match) id = decodeURIComponent(match[1]); } catch (cause) { return; }
    if (id === addressedRecord) return;
    addressedRecord = id;
    const index = languageRecords.findIndex(item => item.id === id);
    if (index >= 0) recordSelect.selectedIndex = index;
  }

  let limitsRecord = null;
  function updateWqLimits() {
    if (recordSelect.value !== limitsRecord) {
      limitsRecord = recordSelect.value;
      const profile = languageRecords.find(item => item.id === limitsRecord);
      for (const name of wqLimitNames) {
        wqFields[name].input.value = profile?.wq_limits?.[name] ?? '';
        wqFields[name].error.textContent = '';
        wqFields[name].input.setAttribute('aria-invalid', 'false');
      }
    }
    const visible = procedureSelect.value === 'wq';
    show(wqLimitsSection, visible);
    wqLimitsSection.disabled = !visible;
  }

  function readWqLimits() {
    return Object.fromEntries(wqLimitNames.map(name => {
      const {input, error} = wqFields[name];
      error.textContent = '';
      input.setAttribute('aria-invalid', 'false');
      return [name, Number(input.value)];
    }));
  }

  function updateStartLabel() {
    const cardProcedure = ['smoke', 'bench', 'serve', 'wq'].includes(procedureSelect.value);
    startButton.textContent = cardProcedure && ['agentic', 'voice'].includes(serving)
      ? 'Stop serving and run…' : 'Start';
  }

  let focusedWork = '';
  function focusWork() {
    const match = /(?:\?|&)work=([^&]+)/.exec(window.location.hash || '');
    if (!match) { focusedWork = ''; return; }
    let id;
    try { id = decodeURIComponent(match[1]); } catch (cause) { return; }
    if (id === focusedWork) return;
    const row = views.get(id)?.row;
    if (row) {
      focusedWork = id;
      row.focus();
      if (row.scrollIntoView) row.scrollIntoView();
    }
  }

  function discardPreview(workId, preview) {
    const content = node('div');
    const fillPreview = () => {
      content.textContent = '';
      for (const file of preview.files) content.append(node('p', `${file.name} — ${file.size} bytes`));
      content.append(node('p', `Total: ${preview.total_size} bytes. ${preview.consequence}`));
    };
    fillPreview();
    window.matutoPopup.open({title: 'Discard partial download?', content,
      safe: 'Keep partial files', confirm: 'Discard partial download',
      confirmAction: async () => {
        try {
          await api('/api/acquisition', {method: 'POST', body: {action: 'discard', job_id: workId, preview}});
          window.setTimeout(refresh, 0);
        } catch (cause) {
          if (cause.preview) { preview = cause.preview; fillPreview(); }
          throw cause;
        }
      },
    });
  }

  function render(data) {
    fill(current, data.entries.filter(item => !item.outcome), 'Nothing running.');
    fill(history, data.entries.filter(item => item.outcome), 'No ended runs yet.');
    updateSelect(acceptanceRecord, data.records, item => `${item.name} (${item.kind})`);
    const ids = new Set(data.entries.map(item => item.id));
    for (const [id] of views) if (!ids.has(id)) views.delete(id);
    const procedures = data.procedures.filter(item => languageProcedures.has(item.id));
    languageRecords = data.records.filter(item => item.kind === 'language');
    updateSelect(procedureSelect, procedures, item => item.label);
    updateSelect(recordSelect, languageRecords, item => `${item.name} (${item.kind})`);
    selectAddressedRecord();
    updateWqLimits();
    updateStartLabel();
    focusWork();
    const unavailable = !procedures.length ? 'No language-profile run procedures are available.'
      : !languageRecords.length ? 'Create a language profile before starting a run.' : '';
    window.matutoReason(startButton, unavailable);
    startReason.textContent = unavailable ? '' : admissionRefusal;
  }

  async function refresh() {
    try {
      const [data, acquisition, status] = await Promise.all([
        api('/api/runs'), api('/api/acquisition'), api('/api/status').catch(() => null),
      ]);
      serving = status?.items?.find(item => item.name === 'serving')?.state || 'unknown';
      if (typeof data.csrf === 'string') window.matutoCsrf = data.csrf;
      downloadStates.clear();
      for (const item of acquisition.downloads || []) downloadStates.set(item.id, item);
      render(data);
      error.hidden = true;
      error.textContent = '';
    } catch (cause) {
      error.hidden = false;
      error.textContent = cause.message;
    }
  }

  function acceptanceExceptions(form) {
    const exceptions = {};
    for (const field of form.querySelectorAll('[data-exception]')) {
      if (field.value.trim()) exceptions[field.getAttribute('data-exception')] = field.value;
    }
    return exceptions;
  }

  function renderAcceptance(data) {
    acceptanceContent.textContent = '';
    const record = data.record;
    const state = record.state.state === 'candidate' && record.state.changed_since_qualification
      ? 'candidate — changed since qualification' : record.state.state;
    acceptanceContent.append(node('p', `${record.name}: ${state}.${record.state.reason ? ` ${record.state.reason}` : ''}`));
    if (record.state.warning) acceptanceContent.append(node('p', record.state.warning));
    const selected = new Set();
    const exceptionReasons = new Map();
    const runs = node('div');
    for (const run of data.runs) {
      const item = node('fieldset');
      item.append(node('legend', `${run.procedure}: ${run.run_id}`));
      item.append(node('p', `Outcome: ${run.outcome || 'running'}. Fingerprint: ${run.fingerprint || 'unavailable'}.`));
      for (const check of run.checks) {
        item.append(node('p', `${check.status}: ${check.name}${check.detail ? ` — ${check.detail}` : ''}`));
      }
      if (run.citable) {
        const label = node('label');
        const checkbox = node('input', undefined, {type: 'checkbox', value: run.run_id});
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) selected.add(run.run_id); else selected.delete(run.run_id);
          updateMinimums(data.runs, selected, minimums);
          updateExceptionFields(data.runs, selected, exceptions);
        });
        label.append(checkbox, node('span', ` Cite this run (${run.started_at})`));
        item.append(label);
      } else item.append(node('p', `Cannot cite: ${run.reason}`));
      if (run.build_warning) item.append(node('p', `Warning: ${run.build_warning}`));
      runs.append(item);
    }
    acceptanceContent.append(runs);
    const form = node('form');
    const minimums = node('div');
    const exceptions = node('div');
    function updateMinimums(allRuns, selectedRuns, output) {
      const cited = allRuns.filter(run => selectedRuns.has(run.run_id));
      const procedure = record.kind === 'bundle' ? 'bundle' : null;
      const missing = record.kind === 'bundle'
        ? !cited.some(run => run.procedure === procedure && run.checks.length && run.checks.every(check => check.status === 'PASS'))
          ? ['a passed bundle run'] : []
        : [
            !cited.some(run => run.procedure === 'smoke' && run.checks.length && run.checks.every(check => check.status === 'PASS')
              && !run.checks.some(check => check.status === 'FAIL' || check.status === 'ERROR')) ? 'a passed smoke' : '',
            !cited.some(run => run.procedure === 'bench') ? 'a completed bench' : '',
          ].filter(Boolean);
      output.textContent = missing.length ? `Still needs: ${missing.join(' and ')}. These requirements cannot be overridden.` : 'Minimum evidence is present.';
    }
    function updateExceptionFields(allRuns, selectedRuns, output) {
      for (const field of output.querySelectorAll('[data-exception]'))
        exceptionReasons.set(field.getAttribute('data-exception'), field.value);
      output.textContent = '';
      const failed = new Set();
      for (const run of allRuns) if (selectedRuns.has(run.run_id))
        for (const check of run.checks) if (check.status === 'FAIL' || check.status === 'ERROR') failed.add(check.name);
      for (const name of failed) {
        const label = node('label', `Named exception for ${name}`);
        const reason = node('input', undefined, {type: 'text', required: true, 'data-exception': name});
        reason.value = exceptionReasons.get(name) || '';
        reason.addEventListener('input', () => exceptionReasons.set(name, reason.value));
        label.append(reason);
        output.append(label);
      }
    }
    form.append(minimums);
    form.append(node('p', 'Automated results are read-only. The date and login username are filled in automatically.'));
    const scopeLabel = node('label', 'Scope');
    const scope = node('textarea', undefined, {name: 'scope', required: true});
    scopeLabel.append(scope);
    const risksLabel = node('label', 'Residual risks');
    const risks = node('textarea', undefined, {name: 'residual-risks'});
    risksLabel.append(risks);
    form.append(scopeLabel, risksLabel, exceptions, node('button', 'Accept qualification', {type: 'submit'}));
    form.addEventListener('submit', async event => {
      event.preventDefault();
      try {
        await api('/api/acceptance', {method: 'POST', body: {
          action: 'accept', kind: record.kind, id: record.id,
          run_ids: [...selected], scope: scope.value, residual_risks: risks.value,
          exceptions: acceptanceExceptions(form),
        }});
        await loadAcceptance();
      } catch (cause) {
        acceptanceError.textContent = cause.message;
        acceptanceError.hidden = false;
      }
    });
    const acceptanceError = node('p', '', {role: 'alert'});
    acceptanceError.hidden = true;
    form.append(acceptanceError);
    updateMinimums(data.runs, selected, minimums);
    updateExceptionFields(data.runs, selected, exceptions);
    if (record.state.state === 'candidate') acceptanceContent.append(form);
    else acceptanceContent.append(node('p', record.state.state === 'qualified'
      ? 'Already qualified at the current fingerprint.'
      : 'Acceptance is unavailable until the current fingerprint can be computed.'));

    const history = node('section');
    history.append(node('h5', 'Acceptance history'));
    for (const acceptance of data.acceptances) {
      const entry = node('article');
      entry.append(node('h6', `${acceptance.acceptance_id}${acceptance.withdrawn_at ? ' — withdrawn' : acceptance.withdrawable ? ' — current' : ' — historical'}`));
      entry.append(node('p', `Scope: ${acceptance.scope}. Residual risks: ${acceptance.residual_risks || 'None recorded'}. Accepted by ${acceptance.accepted_by} on ${format(acceptance.accepted_at)}.`));
      entry.append(node('p', `Accepted fingerprint: ${acceptance.fingerprint}.`));
      for (const cited of acceptance.runs)
        entry.append(node('p', `Cited run: ${cited.procedure} ${cited.run_id} (${cited.record_label}); fingerprint ${cited.run_fingerprint}; build ${cited.build_id || 'unknown'}.`));
      for (const exception of acceptance.exceptions)
        entry.append(node('p', `Exception: ${exception.check_name} — ${exception.reason}`));
      if (acceptance.withdrawable) {
        const withdraw = node('button', 'Withdraw acceptance…', {type: 'button'});
        withdraw.addEventListener('click', () => {
          const content = node('p', record.kind === 'bundle'
            ? 'The record returns to candidate. This acceptance stays in history and nothing stops. A voice bundle keeps serving until stopped; its next Serve voice request will refuse.'
            : 'The record returns to candidate. This acceptance stays in history and nothing stops.');
          window.matutoPopup.open({title: 'Withdraw acceptance?', content, safe: 'Keep acceptance', confirm: 'Withdraw acceptance',
            confirmAction: async () => {
              await api('/api/acceptance', {method: 'POST', body: {
                action: 'withdraw', kind: record.kind, id: record.id,
                acceptance_id: acceptance.acceptance_id,
              }});
              await loadAcceptance();
            }});
        });
        entry.append(withdraw);
      }
      history.append(entry);
    }
    acceptanceContent.append(history);
  }

  async function loadAcceptance() {
    const option = acceptanceRecord.selectedOptions[0];
    if (!option) return;
    try {
      const data = await api(`/api/acceptance?kind=${encodeURIComponent(option.getAttribute('data-kind'))}&id=${encodeURIComponent(acceptanceRecord.value)}`);
      renderAcceptance(data);
    } catch (cause) {
      acceptanceContent.textContent = cause.message;
    }
  }
  acceptanceReview.addEventListener('click', loadAcceptance);

  for (const select of [procedureSelect, recordSelect]) {
    select.addEventListener('change', () => {
      admissionRefusal = '';
      startReason.textContent = '';
      updateWqLimits();
      updateStartLabel();
    });
  }
  start.addEventListener('submit', async event => {
    event.preventDefault();
    admissionRefusal = '';
    startReason.textContent = '';
    const selectedProcedure = procedureSelect.value;
    const selectedRecord = recordSelect.value;
    const selectedRecordLabel = recordSelect.selectedOptions[0]?.textContent || selectedRecord;
    const runRequest = {procedure: selectedProcedure, record: selectedRecord};
    if (selectedProcedure === 'wq') runRequest.limits = readWqLimits();
    try {
      await api('/api/runs', {method: 'POST', body: {
        action: 'start', ...runRequest,
      }});
      await refresh();
    } catch (cause) {
      if (cause.offer) {
        admissionRefusal = cause.message;
        startReason.textContent = admissionRefusal;
        window.matutoPopup.preview({
          title: 'Stop serving and run?', stops: ['Serving; consumers lose the model'],
          starts: [`${selectedProcedure} for ${selectedRecordLabel}`],
          stays: ['Serving remains stopped after the run'], safe: 'Keep serving',
          confirm: 'Stop serving and run',
          confirmAction: async () => {
            await api('/api/runs/stop-serving', {method: 'POST', body: {
              confirm: true, ...runRequest,
            }});
            await refresh();
          },
        });
      } else if (cause.field && wqFields[cause.field]) {
        const {input, error} = wqFields[cause.field];
        input.setAttribute('aria-invalid', 'true');
        error.textContent = cause.message;
      } else {
        admissionRefusal = cause.message;
        startReason.textContent = admissionRefusal;
      }
    }
  });

  refresh();
  if (window.addEventListener) window.addEventListener('hashchange', () => {
    addressedRecord = null;
    selectAddressedRecord();
    updateWqLimits();
    focusWork();
  });
  window.setInterval(refresh, 2000);
})();
