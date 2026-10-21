"""Settings › Maintenance: updates, version-preserving Repair and data-preserving removal."""

PAGE = "settings"
NAME = "maintenance"
TITLE = "Maintenance"
ORDER = 7


def html(_status):
    return '''<div id="matuto-updates" data-updates>
<p>Installed Matuto version: <strong data-installed-version>Loading…</strong></p>
<p>Last successful check: <span data-last-checked>None yet</span></p>
<p>Matuto updates never change llama.cpp or speech runtimes; those stay in <a href="#/runtime/builds">Runtime</a>.</p>
<button type="button" data-check-updates>Check for updates</button>
<p data-update-message role="status" aria-live="polite"></p>
<button type="button" data-install-update hidden>Review…</button>
<ul data-install-blockers hidden></ul>
<p data-install-message role="status" aria-live="polite"></p>
<div data-update-offer hidden>
<h4>Update offer: <span data-offered-version></span></h4>
<dl><dt>Installed version</dt><dd data-fact="installed"></dd>
<dt>Release notes</dt><dd data-fact="notes"></dd>
<dt>Download size</dt><dd data-fact="size"></dd>
<dt>Prerequisites</dt><dd data-fact="prerequisites"></dd>
<dt>Database migration</dt><dd data-fact="database"></dd></dl>
</div>
<div data-withdrawn-offer hidden><p data-withdrawn-message></p></div>
</div>
<div data-maintenance>
<section aria-labelledby="maintenance-repair-title">
<h4 id="maintenance-repair-title">Repair</h4>
<p>Repair restores this installed version, restarts only the manager and does not stop serving or jobs.</p>
<button type="button" data-repair>Review Repair Matuto…</button>
<p data-repair-message role="status" aria-live="polite"></p>
</section>
<section aria-labelledby="maintenance-remove-title">
<h4 id="maintenance-remove-title">Remove Matuto</h4>
<p>Removal keeps Matuto data and never changes consumer configuration.</p>
<button type="button" data-remove>Review removal…</button>
</section>
</div><script src="/updates.js" defer></script><script src="/maintenance.js" defer></script>'''
