'use strict';

(() => {
  const root = document.querySelector('[data-maintenance]');
  if (!root) return;
  const repairButton = root.querySelector('[data-repair]');
  const repairMessage = root.querySelector('[data-repair-message]');
  const removeButton = root.querySelector('[data-remove]');
  let plan;
  let polling = false;
  const node = (tag, text) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    return element;
  };
  async function api(action, removalId) {
    const method = action ? 'POST' : 'GET';
    const url = removalId && !action ? `/api/maintenance?removal_id=${encodeURIComponent(removalId)}` : '/api/maintenance';
    const response = await fetch(url, {
      method, cache: 'no-store',
      headers: action ? {'Content-Type': 'application/json', 'X-CSRF-Token': window.matutoCsrf || ''} : {},
      body: action ? JSON.stringify({action, ...(removalId ? {removalId} : {})}) : undefined,
    });
    let result;
    try { result = await response.json(); }
    catch (cause) { throw new Error('Matuto returned an unreadable Maintenance response.'); }
    if (!response.ok) throw Object.assign(new Error(result.error || 'The request was refused.'),
      {blockers: result.blockers || []});
    return result;
  }
  function setBlockers(blockers) {
    const reasons = (blockers || []).map(item => typeof item === 'string' ? item : item.text);
    window.matutoReason(removeButton, reasons.join(' '));
    if (reasons.length) {
      const links = reasons.map((text, index) => {
        const separator = index ? '\u00a0·\u00a0' : '';
        if (/serving/i.test(text)) return {href: '#/overview/serving', label: separator + 'Overview — serving'};
        if (/update/i.test(text)) return {href: '#/settings/maintenance', label: separator + 'Maintenance — update'};
        return {href: '#/runs', label: separator + 'Runs & jobs'};
      });
      window.notice('settings', 'maintenance-removal-blockers', 'blocker',
        'Removal is unavailable', reasons.join(' '), links);
    } else window.clearNotice('settings', 'maintenance-removal-blockers');
    removeButton.dataset.blocked = String(Boolean(reasons.length));
  }
  function renderRepair(result) {
    if (!result) return;
    if (result.stage === 'complete') repairMessage.textContent = 'Repair completed. The installed Matuto version was preserved.';
    else if (result.stage === 'failed') repairMessage.textContent = `Repair did not complete: ${result.error || 'See the recorded outcome.'}`;
    else if (result.stage === 'in_progress') repairMessage.textContent = 'Repair is in progress; the manager may briefly stop answering.';
  }
  async function refresh() {
    if (document.body.dataset.matutoResultOnly === 'true') return;
    try {
      const result = await api();
      plan = result.removal;
      setBlockers(plan.blockers);
      renderRepair(result.repair);
      if (result.installation && !['complete', 'failed'].includes(result.installation.stage))
        repairButton.dataset.installing = 'true';
      else delete repairButton.dataset.installing;
    } catch (error) {
      repairMessage.textContent = `Maintenance status unavailable: ${error.message}`;
      window.matutoReason(removeButton, 'Removal state is unknown. Retry when Matuto answers.');
    }
  }
  function list(items) {
    const ul = node('ul');
    for (const item of items || []) ul.append(node('li', `${item.item}: ${item.path} (${item.size_bytes} bytes)`));
    return ul;
  }
  function showResult(kept, message, terminal = false, unconfirmed = false) {
    const body = document.body;
    polling = false;
    body.dataset.matutoResultOnly = 'true';
    body.textContent = '';
    const main = node('main');
    const heading = node('h1', unconfirmed ? 'Matuto removal status is unconfirmed' :
      terminal ? 'Matuto removal needs a terminal step' : 'Matuto removed');
    const summary = node('p', message);
    main.append(heading, summary, node('h2', 'Kept data'), list(kept));
    if (terminal || unconfirmed)
      main.append(node('p', 'If the final step did not finish, complete removal from the terminal with the CLI `remove` command.'));
    body.append(main);
  }
  const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  async function waitForRemoval(removalId) {
    let kept = null;
    let requestDone = false;
    let requestResult = null;
    let requestError = null;
    api('remove', removalId).then(result => {
      requestDone = true;
      requestResult = result;
    }, error => {
      requestDone = true;
      requestError = error;
    });
    while (true) {
      try {
        const state = await api(null, removalId);
        if (state.kept) kept = state.kept;
        if (state.stage === 'complete') {
          showResult(state.kept || kept || [], 'Matuto removal completed; the manager and updater were removed last.');
          return;
        }
        if (state.stage === 'failed') {
          showResult(state.kept || kept || [], state.error || 'The final removal step failed.', true);
          return;
        }
        if (state.stage === 'refused') throw new Error(state.error || 'Matuto removal was refused.');
      } catch (error) {
        if (kept) {
          showResult(kept, 'Matuto did not confirm the final removal step. The kept list below is the actual removal result.', true, true);
          return;
        }
        if (requestDone && requestError) throw requestError;
        if (requestDone && requestResult?.finalStepFailed) {
          showResult(requestResult.kept || [], requestResult.error, true);
          return;
        }
        if (requestDone && requestResult?.removed) {
          showResult(requestResult.kept || [], 'Matuto removal completed; the manager and updater were removed last.');
          return;
        }
        if (requestDone) throw error;
        await delay(100);
        continue;
      }
      if (requestDone && requestError && !kept) throw requestError;
      if (requestDone && requestResult?.removed) {
        showResult(requestResult.kept || kept || [], 'Matuto removal completed; the manager and updater were removed last.');
        return;
      }
      await delay(100);
    }
  }
  repairButton.addEventListener('click', () => {
    window.matutoPopup.preview({
      title: 'Review Repair Matuto',
      stops: ['The manager restarts; open tabs lose contact until it answers again.'],
      starts: ['Repair restores the installed Matuto version. It does not select or install a newer release.'],
      stays: ['Serving and every run, build and download stay untouched.'],
      safe: 'Keep current installation', confirm: 'Repair Matuto',
      confirmAction: async () => {
        await api('repair');
        repairMessage.textContent = 'Repair started. This tab will wait for the manager to answer again.';
        if (!polling) { polling = true; pollRepair(); }
      },
    });
  });
  async function pollRepair() {
    while (polling && document.body.dataset.matutoResultOnly !== 'true') {
      await new Promise(resolve => setTimeout(resolve, 1000));
      if (!polling || document.body.dataset.matutoResultOnly === 'true') break;
      try {
        const result = await api();
        if (document.body.dataset.matutoResultOnly === 'true') break;
        renderRepair(result.repair);
        if (result.repair?.stage === 'complete' || result.repair?.stage === 'failed') polling = false;
      } catch (_error) {
        repairMessage.textContent = 'The manager is restarting for Repair. Waiting for it to answer…';
      }
    }
  }
  removeButton.addEventListener('click', async () => {
    if (removeButton.dataset.blocked === 'true') return;
    try {
      plan = await api('remove-preview');
      setBlockers(plan.blockers);
      if (plan.blockers.length) return;
      const content = node('div');
      content.append(node('h3', 'Removed'), list(plan.remove),
        node('h3', 'Kept'), list(plan.keep),
        node('p', plan.consumer_configuration), node('p', plan.later_bootstrap));
      window.matutoPopup.preview({
        title: 'Review removal of Matuto',
        stops: ['Matuto removes its own release and services; it does not stop serving or jobs.'],
        starts: ['Nothing. A later bootstrap reuses the kept data.'],
        stays: ['The store, login, sessions, API key, downloads, build files, weights and speech executables.'],
        content, safe: 'Keep Matuto', confirm: 'Remove Matuto',
        confirmAction: async () => waitForRemoval(window.crypto.randomUUID()),
      });
    } catch (error) {
      window.matutoReason(removeButton, error.message);
      window.notice('settings', 'maintenance-removal-blockers', 'blocker',
        'Removal was refused', error.message, {href: '#/runs', label: 'Check Runs & jobs'});
    }
  });
  window.addEventListener('hashchange', () => {
    if (document.body.dataset.matutoResultOnly !== 'true' &&
        window.location.hash === '#/settings/maintenance') refresh();
  });
  refresh();
})();
