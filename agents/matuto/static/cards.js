'use strict';

(() => {
  const root = document.getElementById('cards-page');
  if (!root) return;
  const form = document.getElementById('cards-form');
  const byName = name => form.elements.namedItem(name);
  const list = document.getElementById('cards-list');
  const error = document.getElementById('cards-error');
  const formError = document.getElementById('cards-form-error');
  const save = document.getElementById('cards-save');
  const cancel = document.getElementById('cards-cancel');
  const add = document.getElementById('cards-add');
  let inventory;
  let selected = null;
  let original = null;
  let popupHome = null;
  let popupNext = null;
  let popupDialog = null;

  async function request(body) {
    const response = await fetch('/api/cards', {
      method: body ? 'POST' : 'GET',
      headers: body ? {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || ''} : {},
      body: body ? JSON.stringify(body) : undefined, cache: 'no-store'
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'The request was refused.');
    return payload;
  }
  function text(parent, tag, value) {
    const node = document.createElement(tag); node.textContent = value; parent.append(node); return node;
  }
  function showError(message, target = error) {
    target.textContent = message; target.hidden = false; target.focus();
  }
  function clearError(target = error) { target.textContent = ''; target.hidden = true; }
  function stateText(value) { return value === 'all' || value === -1 ? 'all' : String(value); }
  function details(card) {
    const article = document.createElement('article'); article.className = 'panel';
    text(article, 'h4', card.name);
    text(article, 'p', `GPU: ${card.gpu_name} (${card.gpu_identity})`);
    text(article, 'p', card.detected === null ? 'GPU detection unavailable' :
      card.detected ? 'Detected now' : 'Unavailable — GPU not detected');
    text(article, 'p', `VRAM ceiling: ${card.vram_ceiling_bytes} bytes`);
    text(article, 'p', `Default GPU layers: ${stateText(card.default_gpu_layers)}`);
    text(article, 'p', `Smoke output: ${card.smoke_output_tokens} tokens`);
    text(article, 'p', `Bench: ${card.bench_prompt_tokens} prompt, ${card.bench_generation_tokens} generation tokens, ${card.bench_repetitions} repetitions`);
    if (card.notes) text(article, 'p', `Notes: ${card.notes}`);
    for (const holder of inventory.holders[card.card_id] || [])
      text(article, 'p', `Used by ${holder.name}${holder.id === 'runtime-selection' ? '' : ` (${holder.id})`}`);
    const actions = document.createElement('div'); actions.className = 'profile-actions';
    const edit = document.createElement('button'); edit.type = 'button'; edit.textContent = 'Edit';
    edit.addEventListener('click', () => openForm('edit', card));
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Remove';
    remove.addEventListener('click', () => removeCard(card));
    actions.append(edit, remove); article.append(actions); return article;
  }
  function draw() {
    list.replaceChildren();
    for (const card of inventory.cards) list.append(details(card));
    const detection = document.getElementById('cards-detection');
    detection.textContent = inventory.state === 'completed'
      ? inventory.devices.length ? '' : 'No GPUs are currently detected.'
      : `GPU detection is unavailable: ${inventory.reason}`;
    const canAdd = inventory.state === 'completed' && inventory.devices.some(device => device.offered && device.vramBytes);
    window.matutoReason(add, canAdd ? '' : inventory.state !== 'completed' ? inventory.reason :
      'No detected GPU with readable VRAM is available for a new card.');
  }
  function fillGpu(value = '') {
    const select = byName('gpu_identity'); select.replaceChildren();
    const choices = inventory.devices.filter(device => device.offered || device.pciAddress === value);
    for (const device of choices) {
      const option = document.createElement('option'); option.value = device.pciAddress;
      option.textContent = `${device.displayName || device.pciAddress} (${device.pciAddress})`;
      select.append(option);
    }
    if (value && !choices.some(device => device.pciAddress === value)) {
      const option = document.createElement('option'); option.value = value;
      option.textContent = `Unavailable — ${value}`; select.append(option);
    }
    select.value = value || choices[0]?.pciAddress || '';
  }
  function openForm(mode, card = null) {
    selected = card?.card_id || null; original = card ? {...card,
      default_gpu_layers: card.default_gpu_layers === -1 ? 'all' : card.default_gpu_layers} : null;
    form.dataset.mode = mode; clearError(formError);
    document.getElementById('cards-form-title').textContent = mode === 'edit' ? `Edit ${card.name}` : 'Add card';
    for (const [name, value] of Object.entries({
      card_id: card?.card_id || '', name: card?.name || '',
      vram_ceiling_bytes: card?.vram_ceiling_bytes || '',
      default_gpu_layers: typeof card?.default_gpu_layers === 'number' && card.default_gpu_layers > 0 ? 'number' : 'all',
      layer_count: typeof card?.default_gpu_layers === 'number' && card.default_gpu_layers > 0 ? card.default_gpu_layers : '',
      smoke_output_tokens: card?.smoke_output_tokens || 16,
      bench_prompt_tokens: card?.bench_prompt_tokens || 4096,
      bench_generation_tokens: card?.bench_generation_tokens || 512,
      bench_repetitions: card?.bench_repetitions || 5, notes: card?.notes || ''
    })) byName(name).value = value;
    byName('card_id').readOnly = mode === 'edit';
    fillGpu(card?.gpu_identity || '');
    if (mode === 'add') {
      const device = inventory.devices.find(item => item.pciAddress === byName('gpu_identity').value);
      byName('vram_ceiling_bytes').value = device?.vramBytes || '';
    }
    byName('default_gpu_layers').dispatchEvent(new Event('change'));
    save.textContent = mode === 'edit' ? 'Save card' : 'Add card';
    save.hidden = mode === 'edit';
    form.hidden = false; form.scrollIntoView({block: 'start'});
    byName(mode === 'edit' ? 'name' : 'card_id').focus();
    updateSave();
  }
  function values() {
    return {card_id: byName('card_id').value, name: byName('name').value,
      gpu_identity: byName('gpu_identity').value,
      vram_ceiling_bytes: Number(byName('vram_ceiling_bytes').value),
      default_gpu_layers: byName('default_gpu_layers').value === 'all' ? 'all' : Number(byName('layer_count').value),
      smoke_output_tokens: Number(byName('smoke_output_tokens').value),
      bench_prompt_tokens: Number(byName('bench_prompt_tokens').value),
      bench_generation_tokens: Number(byName('bench_generation_tokens').value),
      bench_repetitions: Number(byName('bench_repetitions').value), notes: byName('notes').value};
  }
  const servedChanged = record => original &&
    (record.gpu_identity !== original.gpu_identity || record.default_gpu_layers !== original.default_gpu_layers);
  function updateSave() {
    if (form.dataset.mode !== 'edit') return;
    const record = values();
    const dirty = Object.keys(record).some(key => record[key] !== original[key]);
    save.hidden = !dirty;
    save.textContent = servedChanged(record) ? 'Review…' : 'Save card';
    if (form.dataset.inPopup && !dirty) document.querySelector('dialog.popup').close();
  }
  function restorePopupForm() {
    if (!popupHome) return;
    popupHome.insertBefore(form, popupNext?.parentNode === popupHome ? popupNext : null);
    popupHome = popupNext = null;
    delete form.dataset.inPopup;
    save.hidden = false;
    cancel.hidden = false;
    updateSave();
    if (!form.hidden) save.focus();
  }
  function mountForm(content) {
    if (!popupHome) {
      popupHome = form.parentNode;
      popupNext = form.nextSibling;
    }
    content.append(form);
    form.hidden = false;
    form.dataset.inPopup = 'true';
    save.hidden = true;
    cancel.hidden = true;
  }
  function openEditPopup(options) {
    window.matutoPopup.open(options);
    const dialog = document.querySelector('dialog.popup');
    if (popupDialog !== dialog) {
      dialog.addEventListener('close', restorePopupForm);
      popupDialog = dialog;
    }
  }
  function reviewContent(preview) {
    const content = document.createElement('div');
    for (const change of preview.changed_values)
      text(content, 'p', `${change.label}: ${change.old} → ${change.new}`);
    for (const affected of preview.affected) {
      const state = affected.state?.state === 'candidate' && affected.state.changed_since_qualification
        ? 'candidate — changed since qualification' : affected.state?.state || 'no qualification state';
      text(content, 'p', `${affected.kind}: ${affected.name} (${affected.id}) — ${state}`);
    }
    if (preview.reason) text(content, 'p', `Unavailable: ${preview.reason}`);
    const values = document.createElement('details');
    text(values, 'summary', 'Card form values');
    content.append(values);
    return content;
  }
  async function reviewEdit(record) {
    const reviewed = await request({action: 'preview', ...record});
    const content = reviewContent(reviewed.preview);
    const formValues = content.lastChild;
    mountForm(formValues);
    openEditPopup({title: `Review changes to ${record.name}`, content,
      safe: 'Cancel', confirm: 'Save card', confirmAction: async () => {
        if (JSON.stringify(values()) !== JSON.stringify(record))
          throw new Error('The card form changed; cancel and review the current values again.');
        formValues.open = true;
        await request({action: 'save', ...record, confirm: true,
          review_token: reviewed.review_token});
        form.hidden = true; await refresh();
      }});
  }
  function refusedEdit(record, message) {
    const content = document.createElement('div');
    const notice = text(content, 'p', message); notice.className = 'popup-alert';
    notice.setAttribute('role', 'alert'); notice.tabIndex = -1;
    mountForm(content);
    openEditPopup({title: `Edit ${record.name}`, content, safe: 'Cancel',
      confirm: 'Save card', confirmAction: async () => {
        notice.remove();
        const current = values();
        if (servedChanged(current))
          throw new Error('The served card values changed; cancel and review the current values again.');
        await request({action: 'save', ...current});
        form.hidden = true; await refresh();
      }});
    notice.focus();
  }
  async function removeCard(card) {
    const content = document.createElement('p');
    content.textContent = `Remove ${card.name}? Its qualification records and runs remain browsable.`;
    window.matutoPopup.open({title: 'Remove card', content, safe: 'Cancel', confirm: 'Remove card',
      confirmAction: async () => { await request({action: 'remove', card_id: card.card_id});
        form.hidden = true; await refresh(); }});
  }
  async function submit(event) {
    event.preventDefault(); clearError(formError);
    const record = values();
    try {
      if (form.dataset.mode === 'add') {
        const gpu = inventory.devices.find(device => device.pciAddress === record.gpu_identity);
        await request({action: 'add', ...record, gpu});
        form.hidden = true; await refresh(); return;
      }
      if (servedChanged(record)) { await reviewEdit(record); return; }
      await request({action: 'save', ...record}); form.hidden = true; await refresh();
    } catch (cause) {
      if (form.dataset.mode === 'edit') refusedEdit(record, cause.message);
      else showError(cause.message, formError);
    }
  }
  async function refresh() {
    try { inventory = await request(); draw(); clearError(); if (!inventory.cards.length && form.hidden) openForm('add'); }
    catch (cause) { showError(cause.message); }
  }
  add.addEventListener('click', () => { if (add.getAttribute('aria-disabled') !== 'true') openForm('add'); });
  document.getElementById('cards-cancel').addEventListener('click', () => { form.hidden = true; });
  byName('gpu_identity').addEventListener('change', () => {
    if (form.dataset.mode === 'add') {
      const device = inventory.devices.find(item => item.pciAddress === byName('gpu_identity').value);
      byName('vram_ceiling_bytes').value = device?.vramBytes || '';
    }
    updateSave();
  });
  byName('default_gpu_layers').addEventListener('change', () => {
    const numeric = byName('default_gpu_layers').value === 'number';
    document.getElementById('cards-layer-count-label').hidden = !numeric;
    byName('layer_count').required = numeric;
    updateSave();
  });
  form.addEventListener('input', updateSave); form.addEventListener('change', updateSave);
  form.addEventListener('submit', event => {
    if (form.dataset.inPopup) event.preventDefault();
    else return submit(event);
  });
  refresh();
})();
