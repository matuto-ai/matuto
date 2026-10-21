"""Runs & jobs: start and inspect stored context searches."""

PAGE = "runs"
NAME = "context-search"
TITLE = "Context search"
ORDER = 2


def html(_status):
    return '''<link rel="stylesheet" href="/context-search.css">
<div id="context-search-app" aria-live="polite">
  <p id="context-search-error" role="status" hidden></p>
  <section aria-labelledby="context-search-start-title">
    <h4 id="context-search-start-title">Start a context search</h4>
    <form id="context-search-start">
      <label>Language profile or bundle <select name="record" required></select></label>
      <p id="context-search-floor" role="status"></p>
      <label id="context-search-rung-label">Start rung <select name="start_rung" required></select></label>
      <button type="submit">Start</button>
      <p id="context-search-reason" role="status"></p>
    </form>
  </section>
  <section aria-labelledby="context-search-current-title">
    <h4 id="context-search-current-title">Current context searches</h4>
    <div id="context-search-current"></div>
  </section>
  <section aria-labelledby="context-search-history-title">
    <h4 id="context-search-history-title">Context-search history</h4>
    <div id="context-search-history"></div>
  </section>
</div>
<script src="/context-search.js" defer></script>'''
