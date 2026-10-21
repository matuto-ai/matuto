'use strict';

function applyAuthTheme() {
  let theme = 'light';
  try { theme = localStorage.getItem('matuto-theme') === 'dark' ? 'dark' : 'light'; } catch {}
  document.documentElement.dataset.theme = theme;
  const toggle = document.querySelector('#themeToggle');
  if (toggle) {
    toggle.setAttribute('aria-checked', String(theme === 'dark'));
    toggle.addEventListener('click', () => {
      theme = theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = theme;
      toggle.setAttribute('aria-checked', String(theme === 'dark'));
      try { localStorage.setItem('matuto-theme', theme); } catch {}
    });
  }
}
applyAuthTheme();

document.addEventListener('submit', async event => {
  const form = event.target;
  if (form.id !== 'login') return;
  event.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  if (button.disabled) return;
  const data = Object.fromEntries(new FormData(form));
  const error = document.querySelector('#error');
  if (form.action.endsWith('/create-login')) {
    if (!data.username || !data.password || !data.confirm) {
      error.textContent = 'Enter a username, password and confirmation.';
      error.focus();
      return;
    }
    if (data.password !== data.confirm) {
      error.textContent = 'The passwords do not match.';
      error.focus();
      return;
    }
  }
  data.next = location.hash.startsWith('#/')
    ? location.pathname + location.search + location.hash
    : form.elements.next.value;
  button.disabled = true;
  try {
    const response = await fetch(form.action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content },
      body: JSON.stringify(data),
    });
    if (response.redirected) {
      const destination = new URL(response.url);
      destination.hash = new URL(data.next, response.url).hash;
      if (destination.href === location.href) location.reload();
      else location.assign(destination.href);
      return;
    }
    if (response.headers.get('Content-Type').startsWith('application/json')) {
      error.textContent = (await response.json()).error;
      error.focus();
      return;
    }
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    // Keep the document's submit listener: replacing it with document.write breaks retries.
    document.body.replaceWith(page.body);
    applyAuthTheme();
    const feedback = document.querySelector('#error');
    if (feedback.textContent) feedback.focus();
    else document.querySelector('#main').focus();
  } catch {
    error.textContent = 'Matuto did not answer. Your changes were not confirmed.';
    error.focus();
  } finally {
    button.disabled = false;
  }
});
