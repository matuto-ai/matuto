'use strict';

document.addEventListener('DOMContentLoaded', () => {
  const root = document.getElementById('downloadOrigins');
  if (!root) return;
  const form = document.getElementById('origin-form');
  const list = document.getElementById('origins-list');
  const empty = document.getElementById('origins-empty');
  const message = document.getElementById('origins-message');
  let shownOrigins = [];

  function show(items) {
    shownOrigins = items;
    list.textContent = '';
    empty.hidden = items.length !== 0;
    for (const item of items) {
      const row = document.createElement('li');
      const label = document.createElement('span');
      label.textContent = item.kind === 'organisation'
        ? `Organisation: ${item.name}` : `Repository: ${item.name}`;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = 'Remove origin';
      remove.setAttribute('aria-label', `Remove origin ${item.name}`);
      remove.addEventListener('click', async () => {
        message.textContent = '';
        try {
          await api.request('/api/origins', {method: 'POST',
            body: {action: 'remove', origin_id: item.origin_id}});
          show(shownOrigins.filter(origin => origin.origin_id !== item.origin_id));
          if (await refresh())
            message.textContent = `Removed ${item.name}. Running downloads are not cancelled.`;
        } catch (error) {
          message.textContent = error.message;
          message.focus();
        }
      });
      row.append(label, ' ', remove);
      list.append(row);
    }
  }

  async function refresh() {
    try {
      show((await api.request('/api/origins')).origins);
      return true;
    } catch (error) {
      message.textContent = error.message;
      message.focus();
      return false;
    }
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    message.textContent = '';
    try {
      const data = new FormData(form);
      const added = await api.request('/api/origins', {method: 'POST', body: {
        action: 'add', kind: data.get('kind'), name: data.get('name')}});
      form.reset();
      show([...shownOrigins, added.origin]);
      if (await refresh()) message.textContent = `Added ${added.origin.name}.`;
    } catch (error) {
      message.textContent = error.message;
      message.focus();
    }
  });

  refresh();
});
