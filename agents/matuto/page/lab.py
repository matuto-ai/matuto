"""Settings › Lab port: one stored port, with loopback fixed."""

PAGE = "settings"
NAME = "lab"
TITLE = "Lab port"
ORDER = 5

SCRIPT = r"""
document.addEventListener('DOMContentLoaded', () => {
  const root = document.getElementById('labPort');
  if (!root) return;
  const form = document.getElementById('labPortForm');
  const input = document.getElementById('labPortNumber');
  const saved = document.getElementById('labPortSaved');
  const error = document.getElementById('labPortError');
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';

  function showSaved(port) {
    saved.textContent = port === null ? 'No lab port is set.' : `Saved lab port: ${port}.`;
  }
  function showError(message) {
    error.textContent = '';
    const link = document.createElement('a');
    link.href = '#/runs';
    link.textContent = 'Runs & jobs';
    const marker = 'Runs & jobs.';
    const position = message.lastIndexOf(marker);
    if (position < 0) {
      error.textContent = message;
      return;
    }
    error.append(document.createTextNode(message.slice(0, position)), link,
      document.createTextNode(message.slice(position + marker.length)));
  }
  async function request(method, body) {
    const headers = {};
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      headers['X-CSRF-Token'] = csrf;
    }
    const response = await fetch('/api/lab-port', {
      method, headers, cache: 'no-store',
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'The request was refused.');
    return data;
  }
  request('GET').then(data => showSaved(data.port)).catch(cause => showError(cause.message));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    error.textContent = '';
    if (!form.reportValidity()) return;
    const port = Number(input.value);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      showError('The lab port must be a whole number from 1 to 65535.');
      return;
    }
    try {
      const data = await request('POST', {port});
      showSaved(data.port);
    } catch (cause) {
      showError(cause.message);
    }
  });
});
"""


def html(_status):
    return '''<div id="labPort">
  <p>The temporary lab server always binds to loopback. Only its port can be changed.</p>
  <p id="labPortSaved" role="status" aria-live="polite">Loading saved lab port…</p>
  <form id="labPortForm" class="lab-port-form">
    <label for="labPortNumber">Lab port</label>
    <input id="labPortNumber" name="port" type="number" min="1" max="65535" step="1" required>
    <button type="submit">Save lab port</button>
  </form>
  <p id="labPortError" role="alert" aria-live="assertive"></p>
</div>
<link rel="stylesheet" href="/lab-port.css">
<script src="/lab-port.js" defer></script>'''
