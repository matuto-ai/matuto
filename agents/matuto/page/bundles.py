"""Profiles › Bundles: bundle records and their own qualification state."""

PAGE = "profiles"
NAME = "bundles"
TITLE = "Bundles"
ORDER = 3


def html(_status):
    return '''<div class="panel" id="bundles-app">
<p>A bundle is one language profile, one synthesis profile and one recognition profile. Its qualification belongs to the bundle, not its members.</p>
<div id="bundle-error" role="alert" tabindex="-1" hidden></div>
<div id="bundle-list" aria-live="polite"></div>
<form id="bundle-form" class="panel" hidden>
<h4 id="bundle-form-title">Create bundle</h4>
<input type="hidden" name="mode" value="create">
<label>Stable ID <input name="bundle_id" required autocomplete="off"></label>
<label>Name <input name="name" required></label>
<label>Language profile <select name="language" required></select></label>
<label>Synthesis profile <select name="synthesis" required></select></label>
<label>Recognition profile <select name="recognition" required></select></label>
<label>Stated load (tokens) <input name="load_tokens" type="number" min="1" required></label>
<label>Stated load prompt <textarea name="load_prompt" required></textarea></label>
<label>Notes <textarea name="notes"></textarea></label>
<div class="bundle-actions"><button type="submit" id="bundle-save">Save bundle</button><button type="button" id="bundle-cancel">Cancel</button></div>
</form>
<button type="button" id="bundle-create">Create bundle</button>
</div><script src="/bundles.js" defer></script>'''
