"""Runtime build activity: progress and history for every recorded build job."""

PAGE = "runtime"
NAME = "activity"
TITLE = "Build activity"
ORDER = 4
TABBED = True


def html(_status):
    return '''
<div id="build-activity-app">
  <div id="build-activity-list"><p>Loading build activity…</p></div>
</div>
<script src="/build-activity.js" defer></script>
'''
