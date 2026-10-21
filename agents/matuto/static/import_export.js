document.addEventListener('DOMContentLoaded', () => {
  const $ = id => document.getElementById(id);
  const form = $('importExportForm'), file = $('importExportFile');
  const list = $('exportRecordList'), error = $('importExportError'), status = $('importExportStatus');
  function fail(message) {
    error.textContent = message; error.hidden = false; error.focus();
  }
  function render(records) {
    list.textContent = '';
    for (const record of records) {
      const label = document.createElement('label');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.value = `${record.kind}:${record.id}`;
      checkbox.setAttribute('aria-label', `Export ${record.kind} ${record.name}`);
      label.append(checkbox, document.createTextNode(`${record.name} — ${record.kind}`));
      list.append(label);
    }
  }
  async function refresh() {
    try { render((await api.request('/api/import-export')).records); }
    catch (failure) { notice('profiles', 'import-export-load', 'blocker',
      'Records could not be loaded for export.', failure.message); }
  }
  $('exportSelected').addEventListener('click', () => {
    const selected = [...list.querySelectorAll('input:checked')].map(input => input.value);
    if (!selected.length) { fail('Choose at least one record to export.'); return; }
    const query = selected.map(value => `record=${encodeURIComponent(value)}`).join('&');
    window.location.assign(`/api/import-export?download=1&${query}`);
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); error.hidden = true; error.textContent = ''; status.textContent = '';
    const chosen = file.files && file.files[0];
    if (!chosen) { fail('Choose one JSON document to import.'); return; }
    if (chosen.size > 1048576) { fail('The document is malformed or exceeds the 1 MiB limit.'); return; }
    try {
      const result = await api.request('/api/import-export', {method: 'POST', rawBody: chosen});
      status.textContent = result.results.map(record =>
        `${record.kind} ${record.id}: ${record.action}${record.warnings.length ? ` — ${record.warnings.join(' ')}` : ''}`).join('; ');
      clearNotice('profiles', 'import-export-load');
      await refresh();
    } catch (failure) { fail(failure.message); }
  });
  refresh();
});
