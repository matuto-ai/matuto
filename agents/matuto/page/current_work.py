"""Overview's current job or run; hidden when idle."""

PAGE = "overview"
NAME = "current-work"
TITLE = "Current work"
ORDER = 30
TABBED = False


def html(_status):
    return '''<div id="overview-current-work" hidden>
<p id="overview-current-work-detail"></p>
<a id="overview-current-work-link" href="#/runs">Watch in Runs &amp; jobs</a>
</div>'''
