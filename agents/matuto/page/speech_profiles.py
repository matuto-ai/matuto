"""Profiles › Speech profiles: synthesis and recognition records."""

PAGE = "profiles"
NAME = "speech-profiles"
TITLE = "Speech profiles"
ORDER = 2


def html(_status):
    return '''<div id="speechProfiles">
<div class="speech-profile-actions"><button type="button" id="speechProfileNew">New</button></div>
<div id="speechProfileError" role="alert" tabindex="-1" hidden></div>
<div id="speechProfileList" aria-live="polite"></div>
<form id="speechProfileForm" hidden>
<h4 id="speechProfileFormTitle"></h4>
<label>Kind <select id="speechProfileKind"><option value="synthesis">Synthesis</option><option value="recognition">Recognition</option></select></label>
<label>Stable ID <input id="speechProfileId" required autocomplete="off"></label>
<label>Name <input id="speechProfileName" required></label>
<label>Speech executable <select id="speechProfileExecutable" required></select></label><p id="speechProfileExecutableHelp"></p>
<label>Weights <select id="speechProfileWeights" required></select></label><p id="speechProfileWeightsHelp" role="status"></p>
<label id="speechProfileCardLabel">Card <select id="speechProfileCard" required></select></label>
<section><h5>Arguments</h5><div id="speechProfileArguments"></div><button type="button" id="speechProfileAddArgument">Add argument</button></section>
<label>Notes <textarea id="speechProfileNotes"></textarea></label>
<div class="speech-profile-actions"><button type="submit" id="speechProfileSave">Create profile</button><button type="button" id="speechProfileCancel">Cancel</button></div>
</form>
<script src="/speech-profiles.js" defer></script>
</div>'''
