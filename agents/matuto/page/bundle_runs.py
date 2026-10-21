"""Runs & jobs: start and inspect stored bundle runs."""

PAGE = "runs"
NAME = "bundle-runs"
TITLE = "Bundle runs"
ORDER = 1


def html(_status):
    return '''<link rel="stylesheet" href="/bundle-runs.css">
<div id="bundle-runs-app" class="bundle-runs" aria-live="polite">
  <p id="bundle-runs-error" role="status" hidden></p>
  <section aria-labelledby="bundle-runs-start-title">
    <h4 id="bundle-runs-start-title">Start a bundle run</h4>
    <form id="bundle-runs-start">
      <label>Bundle <select name="bundle" required></select></label>
      <button type="submit">Start</button>
      <p id="bundle-runs-reason" role="status"></p>
    </form>
  </section>
  <section aria-labelledby="bundle-runs-current-title">
    <h4 id="bundle-runs-current-title">Current bundle runs</h4>
    <div id="bundle-runs-current"></div>
  </section>
  <section aria-labelledby="bundle-runs-history-title">
    <h4 id="bundle-runs-history-title">Bundle run history</h4>
    <div id="bundle-runs-history"></div>
  </section>
</div>
<script src="/bundle-runs.js" defer></script>'''
