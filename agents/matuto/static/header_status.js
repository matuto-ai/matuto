'use strict';

const headerStatus = (() => {
  const slot = document.querySelector('[data-slot="header-status"]');
  const timeoutMs = 1500;
  let pendingActions = 0;
  let actionFailed = false;
  let actionNoticePage = null;
  let lastItems = null;
  let lost = false;
  let noticePage = null;
  let renderedItems = new Map();
  let offlineMarker = null;

  function resultOnly() {
    return document.body?.dataset?.matutoResultOnly === 'true';
  }

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, options = {}) => {
    if (resultOnly())
      return new Response(JSON.stringify({error: 'result-only'}), {
        status: 409, headers: {'Content-Type': 'application/json'},
      });
    const method = (options.method || input.method || 'GET').toUpperCase();
    const mutating = method !== 'GET' && method !== 'HEAD';
    if (!mutating) return originalFetch(input, options);
    pendingActions++;
    if (!actionFailed && !resultOnly()) actionUnconfirmed();
    try {
      const response = await originalFetch(input, options);
      await response.clone().text();
      pendingActions--;
      clearActionNotice();
      return response;
    } catch (cause) {
      pendingActions--;
      actionFailed = true;
      if (!resultOnly()) actionUnconfirmed();
      throw cause;
    }
  };

  function currentPage() {
    const route = (window.location.hash || '').replace(/^#\/?/, '').split('?')[0].split('/')[0];
    return document.getElementById('page-' + (route || 'overview')) || document.getElementById('page-overview');
  }

  function notice(offline) {
    const page = currentPage();
    if (noticePage && noticePage !== page) {
      const previous = noticePage.querySelector('[data-notice="lost-contact"]');
      if (previous) previous.remove();
    }
    noticePage = page;
    const region = page && page.querySelector('.notices');
    if (!region) return;
    let message = region.querySelector('[data-notice="lost-contact"]');
    if (!offline) {
      if (message) message.remove();
      return;
    }
    if (!message) {
      message = document.createElement('div');
      message.setAttribute('data-notice', 'lost-contact');
      message.setAttribute('class', 'notice warning');
      message.setAttribute('data-kind', 'warning');
      region.append(message);
    }
    message.textContent = 'Matuto is not answering. The displayed status is last known; serving and jobs may still be running. Open Matuto from the app-menu entry.';
  }

  function actionUnconfirmed() {
    const page = currentPage();
    if (actionNoticePage && actionNoticePage !== page) {
      const previous = actionNoticePage.querySelector('[data-notice="action-unconfirmed"]');
      if (previous) previous.remove();
    }
    actionNoticePage = page;
    const region = page?.querySelector('.notices');
    if (!region) return;
    let message = region.querySelector('[data-notice="action-unconfirmed"]');
    if (!message) {
      message = document.createElement('div');
      message.setAttribute('data-notice', 'action-unconfirmed');
      message.setAttribute('class', 'notice warning');
      message.setAttribute('data-kind', 'warning');
      region.append(message);
    }
    message.textContent = actionFailed ? 'The action was not confirmed. Check current status before trying it again.'
      : 'The action is pending and not confirmed until a complete response arrives.';
  }

  function clearActionNotice() {
    if (pendingActions || actionFailed || !actionNoticePage) return;
    const message = actionNoticePage.querySelector('[data-notice="action-unconfirmed"]');
    if (message) message.remove();
    actionNoticePage = null;
  }

  function render(items, offline) {
    const current = new Map();
    for (const item of items || []) {
      const key = item.name || item.type;
      const tag = offline ? 'span' : 'a';
      let node = renderedItems.get(key);
      let restoreFocus = false;
      if (!node || node.tagName.toLowerCase() !== tag) {
        restoreFocus = node === document.activeElement;
        const replacement = document.createElement(tag);
        if (restoreFocus) replacement.setAttribute('tabindex', '-1');
        if (node) node.remove();
        node = replacement;
      }
      node.setAttribute('class', 'status-item');
      node.setAttribute('data-status-name', key);
      if (offline) node.removeAttribute('href');
      else if (node.getAttribute('href') !== item.href) node.setAttribute('href', item.href);
      if (!offline) node.removeAttribute('tabindex');
      const label = offline ? item.label + ' (last known)' : item.label;
      if (node.textContent !== label) node.textContent = label;
      const index = current.size;
      if (node.parentNode !== slot) slot.insertBefore(node, slot.children[index] || offlineMarker);
      else if (slot.children[index] !== node) slot.insertBefore(node, slot.children[index] || offlineMarker);
      if (restoreFocus) node.focus();
      current.set(key, node);
    }
    for (const [key, node] of renderedItems)
      if (current.get(key) !== node) node.remove();
    renderedItems = current;
    if (offline) {
      if (!offlineMarker) {
        offlineMarker = document.createElement('strong');
        offlineMarker.setAttribute('class', 'status-offline');
        offlineMarker.textContent = 'Matuto is not answering';
      }
      if (offlineMarker.parentNode !== slot) slot.append(offlineMarker);
    } else if (offlineMarker) {
      offlineMarker.remove();
      offlineMarker = null;
    }
    notice(offline);
  }

  async function check() {
    if (resultOnly()) return;
    let timer;
    try {
      const answer = await Promise.race([
        (async () => {
          const response = await window.fetch('/api/status', { cache: 'no-store' });
          let payload = null;
          try { payload = await response.json(); } catch (cause) {}
          return { response, payload };
        })(),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('timeout')), timeoutMs); }),
      ]);
      clearTimeout(timer);
      if (resultOnly()) return;
      const { response, payload } = answer;
      if (response.status === 401 && payload && payload.error === 'sign-in is needed') {
        const next = window.location.pathname + window.location.search + window.location.hash;
        const protectedPage = document.querySelector('#main');
        const popup = document.querySelector('[data-slot="popup"]');
        if (protectedPage) protectedPage.textContent = '';
        if (popup) popup.textContent = '';
        slot.textContent = '';
        window.location.replace('/sign-in?next=' + encodeURIComponent(next || '/') + '&ended=1');
        return;
      }
      if (!response.ok || !payload || !Array.isArray(payload.items)) throw new Error('status unavailable');
      lastItems = payload.items;
      lost = false;
      render(lastItems, false);
    } catch (cause) {
      if (resultOnly()) return;
      lost = true;
      render(lastItems || [], true);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  function refresh() { return check(); }
  async function poll() {
    if (resultOnly()) return;
    await check();
    if (!resultOnly()) setTimeout(poll, 500);
  }
  window.addEventListener('hashchange', () => {
    if (resultOnly()) return;
    if (lost) notice(true);
    if (pendingActions || actionFailed) actionUnconfirmed();
  });
  return { refresh, render, poll, get lastItems() { return lastItems; }, get lost() { return lost; } };
})();

headerStatus.poll();
