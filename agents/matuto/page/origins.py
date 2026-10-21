"""Settings › Download origins: allow public Hugging Face sources."""

PAGE = "settings"
NAME = "origins"
TITLE = "Download origins"
ORDER = 2


def html(_status):
    return '''
<link rel="stylesheet" href="/origins.css">
<div id="downloadOrigins" aria-live="polite">
  <p>Allow public Hugging Face repositories or organisations to download from. This is not model endorsement. Private or gated repositories and other hosts are not supported.</p>
  <p id="origins-message" role="status" tabindex="-1"></p>
  <section aria-labelledby="origins-list-title">
    <h4 id="origins-list-title">Configured origins</h4>
    <p id="origins-empty" hidden>New downloads are disabled until an origin is added.</p>
    <ul id="origins-list"></ul>
  </section>
  <form id="origin-form">
    <label>Origin type
      <select name="kind" required>
        <option value="organisation">Hugging Face organisation</option>
        <option value="repository">Hugging Face repository</option>
      </select>
    </label>
    <label>Organisation or repository name
      <input name="name" required autocomplete="off" placeholder="organisation or organisation/repository">
    </label>
    <button type="submit">Add origin</button>
  </form>
</div>
<script src="/origins.js" defer></script>
'''
