"""The Storage & artifacts section in Settings."""

PAGE = "settings"
NAME = "storage-artifacts"
TITLE = "Storage & artifacts"
ORDER = 1


def html(_status):
    return '''<div class="storage-artifacts" id="storage-artifacts">
<p>Model storage is the download destination, native discovery directory and its linked artifact location. Matuto never guesses a directory or moves files.</p>
<div id="storage-message" role="status" aria-live="polite"></div>
<section class="storage-root" aria-labelledby="model-storage-title">
<h4 id="model-storage-title">Model storage</h4><div id="model-storage"></div>
</section>
<section aria-labelledby="artifact-locations-title">
<h4 id="artifact-locations-title">Artifact locations</h4><div id="artifact-locations"></div>
<form id="add-artifact-form"><label for="new-artifact-path">Add artifact location</label>
<input id="new-artifact-path" name="path" type="text" required autocomplete="off">
<button type="submit">Add artifact location</button></form>
</section></div><link rel="stylesheet" href="/storage.css"><script src="/storage.js" defer></script>'''
