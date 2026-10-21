"""Runtime › Speech runtimes: installed speech executable inventory."""

PAGE = "runtime"
NAME = "speech-runtimes"
TITLE = "Speech runtimes"
ORDER = 3


def html(_status):
    return '''
<div id="speech-runtimes-app">
  <p>Installing or updating a speech runtime adds an executable to the Speech profiles picker. It changes no profile or bundle and grants no qualification. Pick another executable in Profiles › Speech profiles under its record-edit rules. Speech weights are a separate choice in Model files.</p>
  <div id="speech-runtimes-list" aria-live="polite"><p>Loading installed speech runtimes…</p></div>
</div>
<link rel="stylesheet" href="/speech-runtimes.css">
<script src="/speech-runtimes.js" defer></script>
'''
