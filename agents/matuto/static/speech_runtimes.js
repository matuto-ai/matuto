'use strict';

(() => {
  const list = document.getElementById('speech-runtimes-list');
  if (!list) return;

  function node(tag, text) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function appendBreakable(element, value) {
    const text = String(value || 'Not recorded');
    for (let offset = 0; offset < text.length; offset += 20) {
      element.append(node('span', text.slice(offset, offset + 20)));
      if (offset + 20 < text.length) element.append(node('wbr'));
    }
  }
  function field(details, label, value) {
    const detail = node('dd');
    appendBreakable(detail, value);
    details.append(node('dt', label), detail);
  }
  function render(entries) {
    list.textContent = '';
    if (!entries.length) {
      const empty = node('p', 'No speech runtimes are installed. Build Breeze or whisper from ');
      const install = node('a', 'Runtime › Install a version');
      install.href = '#/runtime/install';
      empty.append(' ', install);
      list.append(empty);
      return;
    }
    for (const entry of entries) {
      const item = node('article');
      const heading = node('h4');
      heading.append(node('span', `${entry.project} — `));
      appendBreakable(heading, entry.commit);
      item.append(heading);
      const identity = node('dl');
      field(identity, 'Project', entry.project);
      field(identity, 'Recipe', entry.recipe);
      field(identity, 'Backend', entry.backend);
      field(identity, 'Full commit', entry.commit);
      if (entry.project === 'Breeze-TTS-2.cpp')
        field(identity, 'third_party/ggml commit', entry.ggmlCommit);
      field(identity, 'Installation path', entry.installationPath);
      field(identity, 'Executable path', entry.executablePath);
      field(identity, 'Executable SHA-256', entry.sha256);
      item.append(identity);
      const links = node('p');
      const install = node('a', 'Install another version — Runtime › Install a version');
      install.href = entry.installHref;
      const profiles = node('a', 'Open Profiles › Speech profiles');
      profiles.href = '#/profiles/speech-profiles';
      links.append(install, ' · ', profiles);
      item.append(links);
      list.append(item);
    }
  }
  function refresh() {
    return fetch('/api/speech-runtimes', {cache: 'no-store'})
      .then(async response => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Speech runtimes could not be loaded.');
        render(data.executables || []);
        clearNotice('runtime', 'speech-runtimes-error');
      })
      .catch(cause => {
        list.textContent = '';
        notice('runtime', 'speech-runtimes-error', 'blocker',
          'Speech runtimes unavailable', cause.message);
      });
  }
  window.addEventListener('hashchange', () => {
    if (window.location.hash.split('?')[0] === '#/runtime/speech-runtimes') return refresh();
  });
  refresh();
})();
