"""The first-use page for a machine with no stored login."""

import html as html_module

from matuto.shell import STYLE, THEME_SWITCH

PAGE = "auth"
NAME = "create-login"
TITLE = "Create login"
ORDER = 1


def html(_status):
    return ""


def render(csrf, next_path="/", *, message=""):
    """Render the first-login form with only non-empty/matching-field rules."""
    message = html_module.escape(message)
    next_path = html_module.escape(next_path, quote=True)
    csrf = html_module.escape(csrf, quote=True)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="csrf-token" content="{csrf}">
<title>Create login · Matuto</title>
<style nonce="{csrf}">{STYLE}
.auth{{max-width:31rem;margin:2rem auto;padding:0 1rem}}.auth h1{{font-size:2rem}}form{{display:grid;gap:.6rem}}input{{box-sizing:border-box;width:100%;min-width:0;font:inherit;min-height:44px;padding:.6rem;border:1px solid var(--matuto-muted);border-radius:.55rem;background:var(--panel);color:var(--matuto-text)}}.primary{{background:var(--accent);color:var(--ink)}}details{{margin-top:1.5rem}}summary{{min-height:44px;cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}[role=alert]{{color:var(--accent)}}
</style></head><body><a class="skip" href="#main">Skip to content</a><header class="top"><strong>Matuto</strong>{THEME_SWITCH}</header>
<main id="main" class="auth" tabindex="-1"><section class="panel"><h1>Create login</h1><p>Set up your Matuto login on this machine. Choose a username and a non-empty password; there are no strength rules.</p>
{f'<p role="status">{message}</p>' if message else ''}
<form id="login" action="/create-login" method="post"><input type="hidden" name="next" value="{next_path}">
<label for="username">Username</label><input id="username" name="username" autocomplete="username" required>
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="new-password" required>
<label for="confirm">Confirm password</label><input id="confirm" name="confirm" type="password" autocomplete="new-password" required>
<p id="error" role="alert" tabindex="-1"></p><button type="submit" class="primary">Create login and sign in</button></form></section>
<p>Signing in lasts seven days. It does not start serving a model.</p>
</main><script src="/auth.js" defer></script></body></html>'''
