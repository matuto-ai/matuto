"""The password-only Settings account section."""

PAGE = "settings"
NAME = "account"
TITLE = "Account"
ORDER = 6


def html(_status):
    return '''<div class="account" data-account>
<p id="signed-in-as" class="account-username" role="status">Loading account…</p>
<p>Signing out ends this browser's session only. Changing your password ends every session.</p>
<form id="change-password" novalidate>
<h4>Change password</h4>
<label for="current-password">Current password</label>
<input id="current-password" name="current_password" type="password" autocomplete="current-password">
<label for="new-password">New password</label>
<input id="new-password" name="new_password" type="password" autocomplete="new-password">
<label for="confirm-password">Confirm new password</label>
<input id="confirm-password" name="confirmation" type="password" autocomplete="new-password">
<p id="account-error" aria-live="assertive" tabindex="-1"></p>
<button type="submit">Change password</button>
</form>
<button type="button" id="sign-out">Sign out</button>
</div><script src="/account.js" defer></script>'''
