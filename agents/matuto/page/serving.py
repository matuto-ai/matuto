"""Overview's serving state, read-only details and state-dependent controls."""

PAGE = "overview"
NAME = "serving"
TITLE = "Serving"
ORDER = 20
TABBED = False


def html(_status):
    return '''<link rel="stylesheet" href="/overview-serving.css">
<div id="overview-serving" aria-live="polite">
<p id="serving-headline">Reading serving state…</p>
<p id="serving-error" role="status" hidden></p>
<div id="serving-details"></div>
<div id="serving-controls"></div>
</div>
<script src="/overview-serving.js" defer></script>'''
