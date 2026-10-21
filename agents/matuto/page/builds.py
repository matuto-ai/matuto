"""Runtime › llama.cpp builds: registry, trust and explicit runtime selection."""

PAGE = "runtime"
NAME = "builds"
TITLE = "llama.cpp builds"
ORDER = 1

SCRIPT = r"""
document.addEventListener('DOMContentLoaded', () => {
  const root = document.getElementById('runtimeBuilds');
  if (!root) return;
  const list = document.getElementById('runtimeBuildList');
  const selection = document.getElementById('runtimeBuildSelection');
  const esc = value => String(value || 'Unknown');
  let data;

  function element(tag, text, cls) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }
  function button(label, handler) {
    const node = element('button', label);
    node.type = 'button';
    node.addEventListener('click', handler);
    return node;
  }
  function identity(entry) {
    const details = element('details');
    const summary = element('summary', 'Build identity');
    const fields = [
      ['Path', entry.location], ['Full commit', entry.source_revision],
      ['Provenance', entry.source_revision ? 'Matuto build' : 'External; origin and revision unknown'],
      ['Backend', entry.backend], ['Target', entry.target], ['Recipe', entry.recipe],
      ['Toolchain', entry.toolchain], ['Check result', entry.check && entry.check.reason || entry.check && entry.check.state],
    ];
    details.append(summary);
    const values = element('dl');
    for (const [label, value] of fields) {
      const detail = element('dd');
      const text = esc(value);
      for (let index = 0; index < text.length; index += 16) {
        detail.append(element('span', text.slice(index, index + 16)));
        if (index + 16 < text.length) detail.append(element('wbr'));
      }
      values.append(element('dt', label), detail);
    }
    details.append(values);
    return details;
  }
  function refresh() {
    return api.request('/api/builds').then(next => {
      data = next;
      render();
      clearNotice('runtime', 'builds-load');
    }).catch(failure => notice('runtime', 'builds-load', 'blocker',
      'Runtime builds are unavailable.', failure.message));
  }
  function update(body) {
    return api.request('/api/builds', {method: 'POST', body}).then(async result => {
      data = result;
      render();
      clearNotice('runtime', 'builds-action');
      return result;
    });
  }
  function entryName(entry) { return entry.label || entry.build_id || entry.location; }
  function render() {
    list.textContent = '';
    selection.textContent = '';
    const current = data.selection;
    const selectedEntry = current && data.entries.find(entry => entry.build_id === current.build_id);
    if (current && (!selectedEntry || !selectedEntry.available))
      notice('runtime','builds-selected','blocker','Selected runtime is unavailable.',
        selectedEntry && selectedEntry.reason || `Build ${current.build_id} is no longer registered.`);
    else clearNotice('runtime','builds-selected');
    if (!data.entries.length) {
      const empty = element('p', 'No llama.cpp builds found.');
      const install = element('a', 'Install a version');
      install.href = '#/runtime/install';
      empty.append(' ', install); list.append(empty);
    }
    for (const entry of data.entries) {
      const item = element('article', undefined, 'runtime-build');
      item.append(element('h4', entryName(entry)));
      if (entry.build_id) item.append(element('p', `Registry ID: ${entry.build_id}`));
      item.append(element('p', `Status: ${entry.status}${entry.reason ? ` — ${entry.reason}` : ''}`));
      item.append(identity(entry));
      const controls = element('div');
      const explanations = [];
      const explain = (control, text) => { if (text) explanations.push([control, text]); };
      const buildLink = element('a', 'Build'); buildLink.href = '#/runtime/install'; controls.append(buildLink);
      controls.append(button('Inspect', () => inspect(entry)));
      if (entry.build_id && entry.source_revision == null) {
        const findings = (entry.loader_findings || []).map(item =>
          `${item.file || 'Tree'}: ${item.reason}${item.entry ? ` (${item.entry})` : ''}`);
        const refusal = findings.join('\n');
        const trust = button('Trust and inspect', () => {
          if (trust.getAttribute('aria-disabled') === 'true') {
            notice('runtime','builds-inspect','blocker','Inspection refused.',refusal);
            return;
          }
          inspect(entry);
        });
        controls.append(trust);
        explain(trust, refusal);
      }
      if (!entry.build_id) controls.append(button('Register', () => register(entry)));
      else {
        controls.append(button('Rename label', () => rename(entry)));
        const forgetReason = entry.selected ? 'Select another build first.' : '';
        const forget = button('Forget…', () => {
          if (forget.getAttribute('aria-disabled') === 'true') {
            notice('runtime','builds-action','blocker','Forget refused.',forgetReason);
            return;
          }
          forgetBuild(entry);
        });
        controls.append(forget);
        explain(forget, forgetReason);
        const useReason = !entry.available ? entry.reason || 'This build is unavailable.' :
          !current ? 'Select a runtime build and card in the controls below first.' :
          current.build_id === entry.build_id ? 'This build is already selected.' : '';
        const use = button('Use', () => {
          if (use.getAttribute('aria-disabled') === 'true') {
            notice('runtime','builds-action','blocker','Use refused.',useReason);
            return;
          }
          useBuild(entry);
        });
        controls.append(use);
        explain(use, useReason);
        const selectReason = !entry.available ? entry.reason || 'This build is unavailable.' : '';
        const select = button('Select runtime', () => {
          if (select.getAttribute('aria-disabled') === 'true') {
            notice('runtime','builds-action','blocker','Runtime selection refused.',selectReason);
            return;
          }
          const picker = selection.children[selection.children.length - 1];
          const buildChoice = picker.children[0]; buildChoice.value = entry.build_id;
          applySelection(buildChoice.value,picker.children[1].value,picker.children[2].value);
        });
        controls.append(select);
        explain(select, selectReason);
      }
      item.append(controls); list.append(item);
      for (const [control, text] of explanations) reason(control, text);
    }
    if (!current) selection.append(element('p', 'No runtime build selected.'));
    else {
      const found = data.entries.find(entry => entry.build_id === current.build_id);
      selection.append(element('p', `Current runtime: ${current.label || current.build_id}; backend ${current.backend}; card ${current.card || current.card_id}${found && !found.available ? ` — unavailable: ${found.reason}` : ''}`));
    }
    const picker = element('div');
    const build = element('select'); build.setAttribute('aria-label','Runtime build');
    for (const entry of data.entries.filter(item => item.build_id && item.available)) {
      const option = element('option',entryName(entry)); option.value=entry.build_id; build.append(option);
    }
    build.value = current && data.entries.some(item => item.build_id === current.build_id && item.available)
      ? current.build_id : data.entries.find(item => item.build_id && item.available)?.build_id || '';
    const backend = element('select'); backend.setAttribute('aria-label','Backend');
    for (const value of ['hip','vulkan']) { const option=element('option',value); option.value=value; backend.append(option); }
    backend.value=current ? current.backend : 'hip';
    const card = element('select'); card.setAttribute('aria-label','Card');
    for (const item of data.cards) { const option=element('option',item.name); option.value=item.card_id; option.disabled=!item.detected; card.append(option); }
    card.value=current ? current.card_id : data.cards.find(item=>item.detected)?.card_id || '';
    const choose = button('Select runtime', () => {
      if (choose.getAttribute('aria-disabled') === 'true') {
        notice('runtime','builds-action','blocker','Runtime selection refused.',
          'Register or inspect a build until one is available.');
        return;
      }
      applySelection(build.value,backend.value,card.value);
    });
    picker.append(build,backend,card,choose); selection.append(picker);
    if (!data.entries.some(item => item.build_id && item.available))
      reason(choose, 'Register or inspect a build until one is available.');
  }
  function popup({title, content, label, action}) {
    window.matutoPopup.open({title, content, safe: 'Cancel', confirm: label,
      confirmAction: async () => { await action(); await refresh(); }});
  }
  function form(fields) {
    const node = element('form');
    const inputs = {};
    for (const [key, label, value, choices] of fields) {
      const wrap = element('label', label);
      const input = choices ? element('select') : element('input');
      input.name = key; input.required = true;
      if (choices) for (const choice of choices) {
        const option=element('option',choice); option.value=choice; input.append(option);
      }
      input.value = value || '';
      wrap.append(input); node.append(wrap); inputs[key] = input;
    }
    node.addEventListener('submit', event => event.preventDefault());
    return {node, inputs};
  }
  function register(entry) {
    const fields = [['build_id','Stable ID',''], ['label','Label',''],
      ['recipe','Recipe','llama.cpp HIP',['llama.cpp HIP','llama.cpp Vulkan']]];
    if (!entry) fields.unshift(['location','Absolute path to the existing tree','']);
    const {node, inputs} = form(fields);
    if (!entry) node.append(element('p',
      'This registers unavailable metadata only. It runs nothing and does not establish provenance.'));
    popup({title: entry ? 'Register build' : 'Register external build', content: node,
      label: 'Register', action: async () => {
        await update({action:'register', external:!entry || entry.source_revision == null,
          location:entry ? entry.location : inputs.location.value,
          build_id:inputs.build_id.value, label:inputs.label.value, recipe:inputs.recipe.value});
      }});
  }
  function rename(entry) {
    const {node, inputs} = form([['label','Label',entry.label]]);
    popup({title:'Rename label', content:node, label:'Save label', action:async () => {
      await update({action:'rename', build_id:entry.build_id, location:entry.location,
        label:inputs.label.value});
    }});
  }
  function forgetBuild(entry) {
    if (entry.selected) { notice('runtime','builds-action','blocker','Cannot forget selected build.',
      'Select another build first.'); return; }
    popup({title:`Forget ${entryName(entry)}?`, content:element('p',
      'Only the registration will be removed. The files and evidence will stay.'), label:'Forget',
      action:() => update({action:'forget',build_id:entry.build_id})});
  }
  async function inspect(entry) {
    try {
      const result = await update({action:'inspect', build_id:entry.build_id, location:entry.location});
      const inspection = result.inspection;
      const findings = (inspection.findings || []).map(item =>
        `${item.file || 'Tree'}: ${item.reason}${item.entry ? ` (${item.entry})` : ''}`).join('\n');
      if (!inspection.needs_consent) {
        notice('runtime','builds-inspect',inspection.state === 'checked' ? 'warning' : 'blocker',
          'Inspection result.', findings || inspection.reason || inspection.state);
        return;
      }
      const text = element('div');
      text.append(element('p', findings || inspection.reason));
      text.append(element('p','Trust consent will run this tree’s executables as your operator account. An official origin or hashes identify code; they do not prove it is safe.'));
      window.matutoPopup.open({title:'Trust external build?', content:text, safe:'Do not trust', confirm:'Trust and inspect',
        confirmAction:async () => {
          const checked=await update({action:'trust',build_id:entry.build_id,
            trust_digest:inspection.consent_digest});
          if (checked.inspection.state !== 'checked') {
            const detail=(checked.inspection.findings || []).map(item =>
              `${item.file || 'Tree'}: ${item.reason}${item.entry ? ` (${item.entry})` : ''}`).join('\n');
            throw new Error(detail || checked.inspection.reason || 'The tree did not pass all required checks.');
          }
          await refresh();
          const report=checked.inspection.observations && checked.inspection.observations.build;
          notice('runtime','builds-inspect','warning','Inspection observations.',
            report ? `Reported version: ${report.version || report.revision || 'unknown'}; this is an observation, not provenance.` :
              'Inspection passed; the build did not report a version.');
        }});
    } catch (failure) {
      notice('runtime','builds-inspect','blocker','Inspection could not complete.',failure.message);
    }
  }
  function applySelection(buildId, backend, cardId) {
    if (!data.cards.length || !data.cards.some(item=>item.detected)) {
      notice('runtime','builds-card','blocker','A card record is required.',
        'Add a detected card before selecting a runtime build.',{href:'#/settings/cards',label:'Settings › Cards'}); return;
    }
    const entry=data.entries.find(item=>item.build_id===buildId);
    if (!entry) return;
    const change=()=>update({action:'select',build_id:buildId,backend,card_id:cardId});
    const current=data.selection;
    if (!current) { change().catch(failure=>notice('runtime','builds-action','blocker',
      'Runtime selection was refused.',failure.message)); return; }
    const oldCard=data.cards.find(item=>item.card_id===current.card_id);
    const newCard=data.cards.find(item=>item.card_id===cardId);
    const oldName=`${current.label||current.build_id} (${current.backend}; ${oldCard?.name||current.card_id})`;
    const newName=`${entryName(entry)} (${backend}; ${newCard?.name||cardId})`;
    const affected=data.profiles.filter(profile=>profile.card_id!==cardId).map(profile=>profile.name);
    const stays=['Serving stays stopped. Profile configurations and qualifications stay stored.',
      'A different build may produce different results (PROF-5).',`Old: ${oldName}`,`New: ${newName}`];
    if(current.card_id!==cardId&&affected.length)stays.push(`Profiles that will read unavailable: ${affected.join(', ')}`);
    window.matutoPopup.preview({title:'Review runtime selection',stops:[],starts:[],stays,
      safe:'Keep current selection',confirm:'Change selection',confirmAction:change});
  }
  function useBuild(entry) {
    const current=data.selection;
    if(!current)return;
    const oldName=`${current.label||current.build_id} (${current.backend}; ${current.card||current.card_id})`;
    const stays=['Serving stays stopped. Profile configurations and qualifications stay stored.',
      'A different build may produce different results (PROF-5).',`Old: ${oldName}`,
      `New: ${entryName(entry)} (${current.backend}; ${current.card||current.card_id})`];
    window.matutoPopup.preview({title:'Review runtime selection',stops:[],starts:[],stays,
      safe:'Keep current selection',confirm:'Use build',confirmAction:()=>update({action:'use',build_id:entry.build_id})});
  }
  root.addEventListener('click', event => {
    const target = event.target;
    if (target && target.id === 'runtimeBuildRefresh') refresh();
    if (target && target.id === 'runtimeBuildRegisterExternal') register();
  });
  refresh();
});
"""


def html(_status):
    return '''<div id="runtimeBuilds">
<div id="runtimeBuildSelection" aria-live="polite"></div>
<div class="runtime-build-actions"><button id="runtimeBuildRefresh" type="button">Refresh builds</button><button id="runtimeBuildRegisterExternal" type="button">Register external tree</button></div>
<div id="runtimeBuildList" aria-live="polite"><p>Loading llama.cpp builds…</p></div>
<script src="/builds.js" defer></script>
</div>'''
