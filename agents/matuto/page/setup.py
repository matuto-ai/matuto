"""The Overview setup tasks and completed records."""

PAGE = "overview"
NAME = "setup"
TITLE = "Setup"
ORDER = 10


def html(status):
    return '''<div id="overview-setup">
<ul id="overview-setup-tasks" aria-label="Setup tasks"></ul>
<section id="overview-configured" hidden>
<h4>Already configured</h4>
<ul id="overview-configured-list"></ul>
</section>
</div>
<script src="/overview.js" defer></script>'''
