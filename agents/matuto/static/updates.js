'use strict';

(async () => {
  const root = document.querySelector('[data-updates]');
  if (!root) return;
  const installed = root.querySelector('[data-installed-version]');
  const checked = root.querySelector('[data-last-checked]');
  const message = root.querySelector('[data-update-message]');
  const offerPanel = root.querySelector('[data-update-offer]');
  const withdrawnPanel = root.querySelector('[data-withdrawn-offer]');
  const offerText = root.querySelector('[data-offered-version]');
  const withdrawnText = root.querySelector('[data-withdrawn-message]');
  const installButton = root.querySelector('[data-install-update]');
  const blockersList = root.querySelector('[data-install-blockers]');
  const installMessage = root.querySelector('[data-install-message]');
  const formatTime = value => value ? value.replace('T', ' ').replace(/Z$/, ' UTC') : 'None yet';
  let currentListNumber = null;
  const show = result => {
    currentListNumber = result.listNumber;
    installed.textContent = result.installedVersion || 'Unknown';
    checked.textContent = formatTime(result.lastChecked);
    offerPanel.hidden = !result.offer;
    withdrawnPanel.hidden = !result.withdrawn;
    installButton.hidden = !result.offer;
    blockersList.replaceChildren();
    const blockers = result.blockers || [];
    blockersList.hidden = !blockers.length;
    for (const blocker of blockers) {
      const row = document.createElement('li');
      row.append(document.createTextNode(blocker.text + ' — '));
      const link = document.createElement('a');
      link.href = blocker.href;
      link.textContent = blocker.label;
      row.append(link);
      blockersList.append(row);
    }
    if (result.offer) {
      offerText.textContent = result.offer.version;
      root.querySelector('[data-fact="installed"]').textContent = result.installedVersion || 'Unknown';
      root.querySelector('[data-fact="notes"]').textContent = result.offer.notes;
      root.querySelector('[data-fact="size"]').textContent = `${result.offer.size} bytes`;
      root.querySelector('[data-fact="prerequisites"]').textContent = result.offer.prerequisites.join(', ') || 'None listed';
      root.querySelector('[data-fact="database"]').textContent = result.offer.changesDatabase ? 'Yes' : 'No';
      window.matutoReason(installButton, blockers.map(item => item.text).join(' '));
    } else {
      window.matutoReason(installButton, 'No installable update is offered.');
    }
    if (result.withdrawn) {
      withdrawnText.textContent = result.withdrawn.installed
        ? `Installed release ${result.withdrawn.version} is withdrawn and cannot be installed again.`
        : `Release ${result.withdrawn.version} is withdrawn and cannot be installed.`;
    }
    const last = result.lastChecked ? ` Last successful check: ${formatTime(result.lastChecked)}.` : ' No successful check yet.';
    if (result.state === 'no-feed') message.textContent = 'There are no public releases yet.' + last;
    else if (result.state === 'failed') message.textContent = `Check failed: ${result.reason}.${last}`;
    else if (result.state === 'stale') message.textContent = `No news: ${result.reason}.${last}`;
    else if (result.offer) message.textContent = '';
    else if (result.withdrawn) message.textContent = '';
    else message.textContent = 'No newer release is offered.';
    const outcome = result.installation;
    if (outcome?.stage === 'complete')
      installMessage.textContent = `Matuto ${outcome.version} installed successfully.`;
    else if (outcome?.stage === 'failed')
      installMessage.textContent = `Matuto ${outcome.version} was not installed: ${outcome.reason}. ${installed.textContent} remains installed.`;
    else if (outcome?.stage === 'maintenance')
      installMessage.textContent = `Matuto ${outcome.version} remains in maintenance: ${outcome.reason}`;
    else if (outcome?.stage === 'interrupted')
      installMessage.textContent = `Matuto ${outcome.version} update was interrupted during ${outcome.phase}; run Repair Matuto to continue.`;
    else if (outcome?.stage === 'starting')
      installMessage.textContent = `The updater is starting for Matuto ${outcome.version}; the manager will be unavailable while it installs.`;
    else if (outcome)
      installMessage.textContent = `Matuto ${outcome.version} update is being verified.`;
    else installMessage.textContent = '';
  };
  async function request(method, body) {
    const headers = {};
    if (method === 'POST') {
      headers['Content-Type'] = 'application/json';
      headers['X-CSRF-Token'] = window.matutoCsrf;
    }
    const response = await fetch('/api/updates', {
      method, cache: 'no-store', headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let result;
    try { result = await response.json(); } catch (error) { throw new Error('Matuto returned an unreadable update status.'); }
    if (!response.ok) {
      const refusal = new Error(result.error || 'The update request was refused.');
      refusal.blockers = result.blockers || [];
      throw refusal;
    }
    return result;
  }
  try { show(await request('GET')); }
  catch (error) { message.textContent = `Update status unavailable: ${error.message}`; }
  root.querySelector('[data-check-updates]').addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    message.textContent = 'Checking the signed release list…';
    try { show(await request('POST', { action: 'check' })); }
    catch (error) {
      offerPanel.hidden = true;
      installButton.hidden = true;
      message.textContent = `Check failed: ${error.message}`;
    }
    finally { button.disabled = false; }
  });
  installButton.addEventListener('click', async event => {
    if (blockersList.children.length) return;
    installButton.disabled = true;
    installMessage.textContent = 'Rechecking the offer and install requirements…';
    try {
      const version = offerText.textContent;
      const listNumber = currentListNumber;
      const status = await request('POST', { action: 'preview', version, listNumber });
      const groups = {
        stops: [`The manager while Matuto ${version} installs; open tabs lose contact until it returns.`,
          'Matuto refuses new browser, CLI and serving starts while the update runs.',
          'Matuto will not stop serving or cancel unfinished work. If either restarts before confirmation, Install is refused with Stop or Cancel links.'],
        starts: [`Matuto ${version}, after a verified store snapshot${status.offer.changesDatabase ? ' and the offered store migration' : ''}.`,
          `Required OS prerequisites, rechecked at confirmation: ${status.offer.prerequisites.join(', ') || 'None listed'}.`],
        stays: ['Serving stays stopped; login, sessions, router API key, runtimes and model weights stay.'],
      };
      window.matutoPopup.preview({
        title: `Review installing Matuto ${version}`,
        stops: groups.stops,
        starts: groups.starts,
        stays: groups.stays,
        safe: 'Keep current version',
        returnFocus: () => installButton,
        confirm: `Install ${version}`,
        confirmAction: async () => {
          await request('POST', { action: 'install', version, listNumber });
          installMessage.textContent = `The updater started for Matuto ${version}. The manager will be unavailable while it installs.`;
        },
      });
    } catch (error) {
      installMessage.textContent = error.message;
      if (error.blockers?.length) {
        const latest = await request('GET');
        show(latest);
        error.blockers = latest.blockers;
      }
    } finally { installButton.disabled = false; }
  });
})();
