'use strict';

const fs = require('fs');
const path = require('path');
const script = fs.readFileSync(path.join(__dirname, '../../../agents/matuto/static/updates.js'), 'utf8');
const selectors = ['[data-installed-version]', '[data-last-checked]', '[data-update-message]',
  '[data-update-offer]', '[data-withdrawn-offer]', '[data-offered-version]', '[data-withdrawn-message]',
  '[data-fact="installed"]', '[data-fact="notes"]', '[data-fact="size"]',
  '[data-fact="prerequisites"]', '[data-fact="database"]', '[data-check-updates]',
  '[data-install-update]', '[data-install-blockers]', '[data-install-message]'];
const elements = new Map(selectors.map(selector => [selector, {
  textContent: '', hidden: false, disabled: false, children: [], listeners: {},
  addEventListener(name, handler) { this.listeners[name] = handler; },
  replaceChildren(...nodes) { this.children = nodes; },
  append(...nodes) { this.children.push(...nodes); },
} ]));
const root = { querySelector: selector => elements.get(selector) };
const document = {
  querySelector: selector => selector === '[data-updates]' ? root : null,
  createElement: tag => ({ tagName: tag, children: [], append(...nodes) { this.children.push(...nodes); } }),
  createTextNode: text => ({ textContent: text }),
};
const requests = [];
let failNextPost = false;
let payload = { installedVersion: '2026.9.0', lastChecked: '2026-10-12T12:00:00Z', listNumber: 2,
  offer: null, withdrawn: null, blockers: [], state: 'no-feed', reason: 'There are no public releases yet.' };
let currentPopup;
const reasons = [];
const window = {
  matutoCsrf: 'csrf-test',
  matutoReason: (button, reason) => reasons.push({ button, reason }),
  matutoPopup: { preview: value => {
    currentPopup = { ...value, groups: {
      stops: value.stops, starts: value.starts, stays: value.stays,
    } };
  } },
};
const fetch = async (url, options) => {
  requests.push({ url, options });
  if (failNextPost && options.method === 'POST') {
    failNextPost = false;
    throw new Error('network disconnected');
  }
  if (options.method === 'POST') {
    const body = JSON.parse(options.body);
    if (body.action === 'preview') return { ok: true, json: async () => ({ ...payload, offer: payload.offer }) };
    if (body.action === 'install') return { ok: true, json: async () => ({ started: true, version: body.version }) };
    return { ok: true, json: async () => payload };
  }
  return { ok: true, json: async () => payload };
};

(async () => {
  new Function('document', 'window', 'fetch', script)(document, window, fetch);
  await new Promise(resolve => setImmediate(resolve));
  assert(requests.length === 1 && requests[0].options.method === 'GET',
    'opening Maintenance reads saved status but must not check for updates');
  assert(elements.get('[data-update-message]').textContent.includes('no public releases'),
    'a missing public feed must render its no-release state');
  assert(elements.get('[data-last-checked]').textContent === '2026-10-12 12:00:00 UTC',
    'the page must show the last successful check');

  payload = { installedVersion: '2026.9.0', lastChecked: '2026-10-13T12:00:00Z', listNumber: 3,
    state: 'offer', withdrawn: null, blockers: [{ text: 'Run: verify model (Stop)', href: '#/runs?work=job-1', label: 'Runs & jobs — Stop' }],
    offer: { version: '2026.10.0', notes: '<b>Notes</b>', size: 123,
      prerequisites: ['openssh-client'], changesDatabase: true } };
  const checkButton = elements.get('[data-check-updates]');
  await checkButton.listeners.click({ currentTarget: checkButton });
  assert(requests.length === 2 && requests[1].options.method === 'POST',
    'only activating Check for updates submits a check');
  assert(requests[1].options.headers['X-CSRF-Token'] === 'csrf-test',
    'the explicit check carries the current CSRF token');
  assert(requests[1].options.body === JSON.stringify({ action: 'check' }),
    'the route receives the requested check action');
  const installButton = elements.get('[data-install-update]');
  assert(installButton.hidden === false, 'an installable offer shows Review…');
  assert(elements.get('[data-install-blockers]').children[0].children[1].textContent === 'Runs & jobs — Stop',
    'each blocker names its stop destination as a link');
  assert(reasons.at(-1).reason.includes('verify model'), 'the control exposes its blocker beside it');
  await installButton.listeners.click({ currentTarget: installButton });
  assert(currentPopup === undefined, 'an existing blocker prevents opening the preview');

  payload.blockers = [];
  await checkButton.listeners.click({ currentTarget: checkButton });
  await installButton.listeners.click({ currentTarget: installButton });
  assert(currentPopup.title === 'Review installing Matuto 2026.10.0', 'the preview names the reviewed release');
  assert(currentPopup.safe === 'Keep current version', 'the safe choice is first in the preview');
  assert(currentPopup.returnFocus() === installButton, 'closing the async preview returns focus to its Install opener');
  assert(currentPopup.groups.stops[0].includes('open tabs lose contact'), 'the preview describes the manager stop');
  assert(currentPopup.groups.stops[1].includes('refuses new browser, CLI and serving starts'),
    'the preview explains which work is gated during installation');
  assert(currentPopup.groups.stops[2].includes('will not stop serving or cancel unfinished work'),
    'the preview states that Matuto never clears blockers on the operator’s behalf');
  assert(currentPopup.groups.starts[0].includes('verified store snapshot'), 'the preview describes the verified snapshot');
  assert(currentPopup.groups.starts[1].includes('openssh-client'),
    'the confirmation repeats the exact prerequisites that will be rechecked');
  assert(currentPopup.groups.stays[0].includes('sessions'), 'the preview names what stays');
  payload = { ...payload, listNumber: 4,
    offer: { ...payload.offer, version: '2026.11.0' } };
  await checkButton.listeners.click({ currentTarget: checkButton });
  await currentPopup.confirmAction();
  const installRequest = requests.at(-1);
  assert(JSON.parse(installRequest.options.body).action === 'install', 'confirmation alone starts the updater');
  assert(JSON.parse(installRequest.options.body).version === '2026.10.0', 'the updater receives the reviewed version');
  assert(JSON.parse(installRequest.options.body).listNumber === 3, 'confirmation is pinned to the reviewed list');

  failNextPost = true;
  await checkButton.listeners.click({ currentTarget: checkButton });
  assert(elements.get('[data-update-offer]').hidden === true,
    'a failed explicit check must not keep presenting the previous offer');
  assert(elements.get('[data-update-message]').textContent.includes('network disconnected'),
    'a failed explicit check must explain that the latest check failed');

  payload = { installedVersion: '2026.10.0', lastChecked: '2026-10-13T13:00:00Z', listNumber: 4,
    state: 'withdrawn', offer: null, blockers: [], withdrawn: { version: '2026.10.0', installed: true } };
  await checkButton.listeners.click({ currentTarget: checkButton });
  assert(elements.get('[data-withdrawn-offer]').hidden === false,
    'a withdrawn installed release gets a visible withdrawn state');
  assert(elements.get('[data-withdrawn-message]').textContent.includes('cannot be installed again'),
    'withdrawn state never implies install consent or availability');
  assert(installButton.hidden === true, 'withdrawn releases never offer Install');
  process.stdout.write('Maintenance update page behavior passed.\n');
})().catch(error => { console.error(error); process.exitCode = 1; });

function assert(condition, message) { if (!condition) throw new Error(message); }
