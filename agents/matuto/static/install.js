'use strict';

(() => {
  const form = document.getElementById('runtime-install-form');
  const recipe = form.elements.recipe;
  const ref = form.elements.ref;
  const target = form.elements.target;
  const targetLabel = document.getElementById('runtime-target-label');
  const checkButton = document.getElementById('runtime-check');
  const resolveButton = document.getElementById('runtime-resolve');
  const buildButton = document.getElementById('runtime-build');
  const pinText = document.getElementById('runtime-pin');
  const refs = document.getElementById('runtime-refs');
  const error = document.getElementById('runtime-error');
  let cards = [];
  let recipes = [];
  let pin = null;
  let buildRefusal = '';
  let invalidation = Promise.resolve();
  let checkSequence = 0;
  let resolveSequence = 0;
  let servingSequence = 0;
  let servingFresh = false;
  const node = (tag, text, attrs = {}) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
    return element;
  };
  const api = async body => {
    const response = await fetch('/api/runtime-install', {
      method: body ? 'POST' : 'GET', cache: 'no-store',
      headers: body ? {'Content-Type': 'application/json', 'X-CSRF-Token': window.matutoCsrf || ''} : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'The request was refused.');
    return value;
  };
  const currentRecipe = () => recipes.find(item => item.name === recipe.value);
  const hip = () => Boolean(currentRecipe()?.takesGfxTarget);
  const card = () => cards.find(item => item.pciAddress === target.value);
  const request = action => ({action, recipe: recipe.value, ref: ref.value,
    target: hip() ? card()?.gfxTarget || null : null,
    card: hip() ? target.value : null,
    pinId: action === 'build' ? pin?.pinId || null : null});

  function updateBuild() {
    const choice = currentRecipe();
    const missing = choice?.missingPrerequisites || [];
    const unavailable = !choice ? 'Choose one of the four fixed recipes.'
      : missing.length ? `Missing prerequisites: ${missing.join(', ')}.`
      : hip() && (!card() || !card().canBuildHIP) ? (card()?.targetReason || 'Choose a detected HIP target.')
      : !servingFresh ? 'Refresh serving status before Build.'
      : !pin ? 'Resolve an official version for the current recipe, ref and target.' : '';
    window.matutoReason(buildButton, buildRefusal || unavailable);
    if (missing.length) showNotice(`${choice.name} cannot build yet`, unavailable, 'blocker', 'runtime-prerequisites');
    else window.clearNotice('runtime', 'runtime-prerequisites');
    pinText.textContent = pin ? `Resolved ${pin.commit} from ${pin.ref} for ${pin.recipe}${pin.target ? ` (${pin.target})` : ''}.` : '';
  }
  function clearPin() {
    resolveSequence++;
    pin = null;
    buildRefusal = '';
    pinText.textContent = '';
    updateBuild();
    invalidation = invalidation.then(() => api({action: 'invalidate'})).catch(() => {});
  }
  function showNotice(title, detail, kind = 'blocker', key = 'runtime-install') {
    notice('runtime', key, kind, title, detail);
  }
  function renderRefs(items) {
    refs.textContent = '';
    for (const item of items) {
      const button = node('button', `${item.name} — ${item.commit}`, {type: 'button'});
      button.addEventListener('click', () => { ref.value = item.name; clearPin(); });
      refs.append(button);
    }
  }
  function servingNotice(state) {
    if (state === 'agentic') showNotice('Serving remains up during a build',
      'CPU, RAM and disk contention may slow serving. Matuto stops and restarts nothing.',
      'warning', 'runtime-contention');
    else window.clearNotice('runtime', 'runtime-contention');
  }
  async function refreshServingState() {
    const sequence = ++servingSequence;
    servingFresh = false;
    updateBuild();
    try {
      const result = await api({action: 'serving-state'});
      if (sequence !== servingSequence) return;
      servingNotice(result.servingState);
      servingFresh = true;
    } catch (cause) {
      if (sequence !== servingSequence) return;
      showNotice('Serving state is unknown', cause.message, 'warning', 'runtime-contention');
      servingFresh = true;
    }
    updateBuild();
  }
  function isInstallRoute() {
    const page = document.getElementById('page-runtime');
    const panel = document.querySelector('#page-runtime section[data-section="install"]');
    return Boolean(page && !page.hidden && panel && !panel.hidden);
  }
  function renderCards() {
    target.textContent = '';
    target.append(node('option', 'Choose a physical card', {value: ''}));
    for (const item of cards) {
      const label = `${item.displayName || item.pciAddress} (${item.gfxTarget || 'HIP target unavailable'})`;
      const option = node('option', label, {value: item.pciAddress});
      if (!item.canBuildHIP) option.setAttribute('disabled', '');
      target.append(option);
    }
    const preferred = cards.find(item => item.default && item.canBuildHIP);
    target.value = preferred ? preferred.pciAddress : '';
    targetLabel.hidden = !hip();
  }
  function clearInstallNotice() { window.clearNotice('runtime', 'runtime-install'); }
  function selectRouteRecipe() {
    const query = window.location.hash.split('?')[1] || '';
    const requested = new URLSearchParams(query).get('recipe');
    if (!requested || !recipes.some(item => item.name === requested) || recipe.value === requested) return;
    recipe.value = requested;
    refs.textContent = '';
    clearPin();
    renderCards();
    updateBuild();
  }

  recipe.addEventListener('change', () => {
    checkSequence++;
    refs.textContent = '';
    clearPin();
    renderCards();
    updateBuild();
  });
  ref.addEventListener('input', clearPin);
  target.addEventListener('change', clearPin);
  checkButton.addEventListener('click', async () => {
    clearPin(); refs.textContent = '';
    const sequence = ++checkSequence;
    const selectedRecipe = recipe.value;
    try {
      await invalidation;
      const result = await api(request('check'));
      if (sequence !== checkSequence || recipe.value !== selectedRecipe) return;
      renderRefs(result.refs || []);
      if (result.unknown) showNotice('Version check is unknown', result.reason || 'The official origin did not answer.');
      else clearInstallNotice();
    } catch (cause) {
      if (sequence === checkSequence && recipe.value === selectedRecipe)
        showNotice('Version check is unknown', cause.message);
    }
  });
  resolveButton.addEventListener('click', async () => {
    clearPin();
    const sequence = ++resolveSequence;
    const inputs = request('resolve');
    try {
      await invalidation;
      const result = await api(inputs);
      if (sequence !== resolveSequence || JSON.stringify(inputs) !== JSON.stringify(request('resolve'))) return;
      if (!result.commit) {
        showNotice(result.unknown ? 'Version check is unknown' : 'Version was not resolved', result.reason);
        return;
      }
      pin = result;
      clearInstallNotice();
      updateBuild();
    } catch (cause) {
      if (sequence === resolveSequence) showNotice('Version was not resolved', cause.message);
    }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const choice = currentRecipe();
    if (!servingFresh || !choice || choice.missingPrerequisites?.length || !pin) {
      updateBuild();
      return;
    }
    try {
      await invalidation;
      buildRefusal = '';
      const result = await api(request('build'));
      pin = null;
      clearInstallNotice();
      showNotice('Build started', `Build ${result.work} was admitted. ${result.warning || ''}`.trim(), 'warning');
      const link = node('a', 'Open Runtime › Build activity', {href: result.href});
      document.querySelector('#page-runtime .notices [data-notice="runtime-install"]')?.append(link);
      updateBuild();
    } catch (cause) {
      clearPin();
      buildRefusal = cause.message;
      updateBuild();
      showNotice('Build was refused', cause.message);
    }
  });
  window.addEventListener('hashchange', () => {
    servingSequence++;
    servingFresh = false;
    updateBuild();
    if (isInstallRoute()) {
      selectRouteRecipe();
      refreshServingState();
    }
  });
  window.addEventListener('focus', () => { if (isInstallRoute()) refreshServingState(); });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && isInstallRoute()) refreshServingState();
  });
  async function init() {
    const sequence = servingSequence;
    try {
      const data = await api();
      if (typeof data.csrf === 'string') window.matutoCsrf = data.csrf;
      recipes = data.recipes || [];
      cards = data.cards || [];
      for (const item of recipes) recipe.append(node('option', item.name, {value: item.name}));
      selectRouteRecipe();
      renderCards();
      if (data.reason) showNotice('Host information is unavailable', data.reason);
      if (sequence === servingSequence) {
        servingNotice(data.servingState);
        servingFresh = true;
      }
      updateBuild();
    } catch (cause) { error.hidden = false; error.textContent = cause.message; }
  }
  init();
})();
