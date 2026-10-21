'use strict';

(() => {
  const list = document.getElementById('build-activity-list');
  const views = new Map();
  const retryReasons = new Map();
  const recipes = {
    'llama.cpp HIP': 'llama.cpp HIP',
    'llama.cpp Vulkan': 'llama.cpp Vulkan',
    'Breeze synthesis (Vulkan)': 'Breeze synthesis (Vulkan)',
    'whisper recognition (CPU)': 'whisper recognition (CPU)',
  };
  const node = (tag, text, attrs = {}) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
    return element;
  };
  const api = async (options = {}) => {
    const response = await fetch('/api/build-activity', {
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
  const format = value => value ? new Date(value).toLocaleString() : 'Unknown';
  const targetLink = recipe => recipe === 'Breeze synthesis (Vulkan)' || recipe === 'whisper recognition (CPU)'
    ? ['#/runtime/speech-runtimes', 'Runtime › Speech runtimes']
    : ['#/runtime/builds', 'Runtime › llama.cpp builds'];

  function viewFor(build) {
    let view = views.get(build.id);
    if (view) return view;
    const row = node('article');
    const heading = node('h4');
    heading.setAttribute('tabindex', '-1');
    const pin = node('p', '', {class: 'build-pin'});
    const owner = node('p');
    const state = node('p');
    const progress = node('p');
    const log = node('pre', '', {class: 'runs-log'});
    const failure = node('p');
    const provenance = node('details');
    const provenanceSummary = node('summary', 'Kept build provenance and output digests');
    const provenanceText = node('pre', '', {class: 'runs-log'});
    const runs = node('a', 'Runs & jobs');
    const install = node('a');
    const actions = node('div');
    const stop = node('button', 'Stop…', {type: 'button'});
    const retry = node('button', 'Retry', {type: 'button'});
    const remove = node('button', 'Delete history…', {type: 'button'});
    view = {row, heading, pin, owner, state, progress, log, failure, provenance,
      provenanceText, runs, install, actions, stop, retry, remove, build};
    stop.addEventListener('click', () => window.matutoPopup.preview({
      title: 'Stop this build?',
      stops: ['The build; its staging is never installed'], starts: [],
      stays: ['Serving', 'every installed build', 'the runtime selection'],
      safe: 'Keep build running', confirm: 'Stop build',
      confirmAction: async () => {
        await api({method: 'POST', body: {action: 'stop', id: view.build.id}});
        window.setTimeout(refresh, 0);
      },
    }));
    retry.addEventListener('click', async () => {
      retryReasons.delete(view.build.id);
      window.matutoReason(retry, '');
      try {
        await api({method: 'POST', body: {action: 'retry', id: view.build.id}});
        await refresh();
      } catch (cause) {
        retryReasons.set(view.build.id, cause.message);
        window.matutoReason(retry, cause.message);
      }
    });
    remove.addEventListener('click', () => window.matutoPopup.open({
      title: 'Delete build history?', safe: 'Keep history', confirm: 'Delete history',
      content: node('p', 'The build log will be removed. Provenance and output digests needed by installations or runs will be kept.'),
      confirmAction: async () => {
        await api({method: 'POST', body: {action: 'delete-history', id: view.build.id}});
        window.setTimeout(refresh, 0);
      },
    }));
    provenance.append(provenanceSummary, provenanceText);
    actions.append(stop, retry, remove);
    row.append(heading, pin, owner, state, progress, log, failure, runs, install, provenance, actions);
    views.set(build.id, view);
    return view;
  }

  function updateView(build) {
    const view = viewFor(build);
    view.build = build;
    const [destination, destinationLabel] = targetLink(build.recipe);
    view.heading.textContent = recipes[build.recipe] || build.recipe;
    const target = build.target ? `; target ${build.target}` : '';
    view.pin.textContent = `Build job: ${build.id}. Pinned ref: ${build.ref || 'Unknown'}; commit: ${build.commit || 'Unknown'}${target}.`;
    view.owner.textContent = `Started by ${build.startedBy || 'the operator'} on ${format(build.startedAt)}.`;
    view.state.textContent = build.outcome
      ? `Outcome: ${build.outcome}.`
      : `State: ${build.state}.`;
    show(view.progress, Boolean(build.phase || build.unknown || !build.outcome));
    view.progress.textContent = build.unknown || (build.outcome ? ''
      : `Phase: ${build.phase || 'Not reported'}. Current step: ${build.step || 'Not reported'}. Elapsed: ${Math.floor(build.elapsed || 0)} seconds.`);
    view.log.textContent = build.log || '';
    if (build.logTruncated && build.log) view.log.textContent += '\nLog tail truncated.';
    show(view.log, Boolean(build.log));
    view.failure.textContent = build.outcome && build.outcome !== 'completed'
      ? `Failure reason: ${build.failureReason || 'No failure reason was recorded.'}` : '';
    show(view.failure, Boolean(view.failure.textContent));
    view.runs.setAttribute('href', `#/runs?work=${encodeURIComponent(build.id)}`);
    view.install.setAttribute('href', destination);
    view.install.textContent = `Continue to ${destinationLabel}`;
    show(view.install, build.outcome === 'completed');
    view.provenanceText.textContent = JSON.stringify({provenance: build.provenance,
      outputDigests: build.outputDigests}, null, 2);
    view.provenance.open = Boolean(build.historyLogEmpty);
    show(view.provenance, Boolean(build.outcome && build.provenance));
    show(view.stop, build.canStop);
    if (!build.canStop && document.activeElement === view.stop) view.heading.focus();
    show(view.retry, build.canRetry);
    window.matutoReason(view.retry, retryReasons.get(build.id) || '');
    show(view.remove, build.canDeleteHistory);
    return view.row;
  }

  function fill(container, builds, emptyText) {
    const rows = new Set(builds.map(build => views.get(build.id)?.row).filter(Boolean));
    const knownRows = new Set([...views.values()].map(view => view.row));
    for (const child of [...container.children])
      if ((!knownRows.has(child) || !rows.has(child)) && child !== container.emptyMessage)
        child.remove();
    if (builds.length && container.emptyMessage?.parentNode === container)
      container.emptyMessage.remove();
    builds.forEach((build, index) => {
      const row = updateView(build);
      const current = container.children[index] || null;
      if (current !== row) container.insertBefore(row, current);
    });
    if (!builds.length) {
      if (!container.emptyMessage) {
        container.emptyMessage = node('p', emptyText);
        container.emptyMessage.append(node('a', 'Open Runtime › Install a version', {href: '#/runtime/install'}));
      }
      if (container.emptyMessage.parentNode !== container) container.append(container.emptyMessage);
    }
  }

  function render(data) {
    fill(list, data.builds, 'No runtime builds yet.');
    for (const [id] of views) if (!data.builds.some(build => build.id === id)) views.delete(id);
    if (data.contention) notice('runtime', 'build-activity-contention', 'warning',
      'Build may slow serving', 'CPU, RAM and disk contention may slow serving. Matuto does not stop or restart anything.',
      {href: '#/runs', label: 'Open Runs & jobs'});
    else clearNotice('runtime', 'build-activity-contention');
    clearNotice('runtime', 'build-activity-error');
  }

  async function refresh() {
    try {
      const data = await api();
      if (typeof data.csrf === 'string') window.matutoCsrf = data.csrf;
      render(data);
    } catch (cause) {
      notice('runtime', 'build-activity-error', 'blocker', 'Build activity unavailable', cause.message);
    }
  }

  refresh();
  window.setInterval(refresh, 2000);
})();
