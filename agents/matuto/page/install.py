"""Runtime › Install a version: choose, pin and submit one fixed recipe."""

PAGE = "runtime"
NAME = "install"
TITLE = "Install a version"
ORDER = 2


def html(_status):
    return '''
<div id="runtime-install-app">
  <form id="runtime-install-form">
    <label>Recipe <select name="recipe" required></select></label>
    <label>Official branch, tag or commit <input name="ref" required autocomplete="off"></label>
    <button id="runtime-check" type="button">Check versions</button>
    <label id="runtime-target-label" hidden>HIP target <select name="target"></select></label>
    <p id="runtime-pin"></p>
    <button id="runtime-resolve" type="button">Resolve version</button>
    <button id="runtime-build" type="submit">Build</button>
  </form>
  <div id="runtime-refs"></div>
  <p id="runtime-error" role="status" hidden></p>
</div>
<script src="/install.js" defer></script>
'''
