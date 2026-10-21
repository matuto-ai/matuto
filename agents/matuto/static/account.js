'use strict';

const form = document.querySelector('#change-password');
const error = document.querySelector('#account-error');
const username = document.querySelector('#signed-in-as');
const signOut = document.querySelector('#sign-out');
let csrf = '';

function showError(message) {
  error.textContent = message;
  error.focus();
}

async function accountRequest(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    cache: 'no-store',
    headers: body === undefined ? {} : {
      'Content-Type': 'application/json',
      'X-CSRF-Token': csrf,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let message = 'The request was refused.';
    try { message = (await response.json()).error || message; } catch {}
    throw new Error(message);
  }
  return response;
}

accountRequest('/api/account').then(async response => {
  const data = await response.json();
  csrf = data.csrf;
  username.textContent = `Signed in as ${data.username}`;
}).catch(cause => showError(cause.message || 'Matuto is not answering.'));

form.addEventListener('submit', async event => {
  event.preventDefault();
  const fields = Object.fromEntries(new FormData(form));
  if (!fields.current_password) return showError('Enter your current password.');
  if (!fields.new_password) return showError('A new password is required.');
  if (fields.new_password !== fields.confirmation) return showError('The passwords do not match.');
  const button = form.querySelector('button[type="submit"]');
  if (button.disabled) return;
  button.disabled = true;
  error.textContent = '';
  try {
    const response = await accountRequest('/api/account/change-password', fields);
    if (response.redirected) location.assign(response.url);
  } catch (cause) {
    showError(cause.message || 'Matuto did not answer. Your changes were not confirmed.');
  } finally {
    button.disabled = false;
  }
});

signOut.addEventListener('click', async () => {
  if (signOut.disabled) return;
  signOut.disabled = true;
  try {
    await accountRequest('/sign-out', {});
    location.assign('/sign-in?signed_out=1');
  } catch (cause) {
    signOut.disabled = false;
    showError(cause.message || 'Matuto did not answer. Your changes were not confirmed.');
  }
});
