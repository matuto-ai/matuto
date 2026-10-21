"""Runs & jobs: current shared work and retained run history."""

PAGE = "runs"
NAME = "runs"
TITLE = "Runs & jobs"
ORDER = 0
TABBED = False


def html(_status):
    return '''
<div id="runs-app" aria-live="polite">
  <p id="runs-error" role="status" hidden></p>
  <section aria-labelledby="runs-current-title">
    <h4 id="runs-current-title">Current work</h4>
    <div id="runs-current"><p>Loading current work…</p></div>
  </section>
  <section aria-labelledby="runs-start-title">
    <h4 id="runs-start-title">Start a run</h4>
    <form id="runs-start">
      <label>Procedure <select name="procedure" required></select></label>
      <label>Record <select name="record" required></select></label>
      <fieldset id="runs-wq-limits" hidden>
        <legend>Worker qualification limits</legend>
        <div class="wq-limit"><label>First byte seconds <input id="wq-first_byte_seconds" type="number" step="any"></label><span id="wq-error-first_byte_seconds" role="status"></span></div>
        <div class="wq-limit"><label>Short total seconds <input id="wq-short_total_seconds" type="number" step="any"></label><span id="wq-error-short_total_seconds" role="status"></span></div>
        <div class="wq-limit"><label>Generation tokens per second <input id="wq-generation_tokens_per_second" type="number" step="any"></label><span id="wq-error-generation_tokens_per_second" role="status"></span></div>
        <div class="wq-limit"><label>VRAM limit (MB) <input id="wq-vram_mb" type="number" step="any"></label><span id="wq-error-vram_mb" role="status"></span></div>
        <div class="wq-limit"><label>RSS limit (KiB) <input id="wq-rss_kib" type="number" step="any"></label><span id="wq-error-rss_kib" role="status"></span></div>
        <div class="wq-limit"><label>Cancel recovery seconds <input id="wq-cancel_recovery_seconds" type="number" step="any"></label><span id="wq-error-cancel_recovery_seconds" role="status"></span></div>
        <div class="wq-limit"><label>Restart seconds <input id="wq-restart_seconds" type="number" step="any"></label><span id="wq-error-restart_seconds" role="status"></span></div>
        <div class="wq-limit"><label>Long execution seconds <input id="wq-long_execution_seconds" type="number" step="any"></label><span id="wq-error-long_execution_seconds" role="status"></span></div>
        <div class="wq-limit"><label>Retrieval samples <input id="wq-retrieval_samples" type="number" step="any"></label><span id="wq-error-retrieval_samples" role="status"></span></div>
        <div class="wq-limit"><label>Retrieval generation tokens <input id="wq-retrieval_generation_tokens" type="number" step="any"></label><span id="wq-error-retrieval_generation_tokens" role="status"></span></div>
      </fieldset>
      <button type="submit">Start</button>
      <p id="runs-start-reason" role="status"></p>
    </form>
  </section>
  <section aria-labelledby="runs-history-title">
    <h4 id="runs-history-title">History</h4>
    <div id="runs-history"><p>Loading history…</p></div>
  </section>
  <section aria-labelledby="acceptance-title">
    <h4 id="acceptance-title">Qualification acceptance</h4>
    <label for="acceptance-record">Language profile or bundle</label>
    <select id="acceptance-record"></select>
    <button id="acceptance-review" type="button">Review acceptance</button>
    <div id="acceptance-content" class="runs-acceptance"></div>
  </section>
</div>
<link rel="stylesheet" href="/runs.css">
<script src="/runs.js" defer></script>
'''
