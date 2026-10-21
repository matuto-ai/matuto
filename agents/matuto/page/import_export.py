"""Profiles › Import / export portable record documents."""

PAGE = "profiles"
NAME = "import-export"
TITLE = "Import / export"
ORDER = 4


def html(_status):
    return '''<div id="importExport">
<p>Move records between Matuto instances. Model files and speech executables must already be acquired or adopted here; imported records bring no qualification or publication.</p>
<div id="importExportError" role="alert" tabindex="-1" hidden></div>
<p id="importExportStatus" role="status" aria-live="polite"></p>
<form id="importExportForm">
<label>JSON document <input id="importExportFile" type="file" accept="application/json,.json" required></label>
<button type="submit">Import document</button>
</form>
<h4>Export records</h4>
<div id="exportRecordList" role="group" aria-label="Records to export"></div>
<button type="button" id="exportSelected">Download selected records as JSON file</button>
<a href="/api/import-export?download=1">Download all records as JSON file</a>
<script src="/import-export.js" defer></script>
</div>'''
