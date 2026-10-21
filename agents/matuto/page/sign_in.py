"""The signed-out page for an existing login."""

import html as html_module

from matuto.shell import STYLE, THEME_SWITCH

PAGE = "auth"
NAME = "sign-in"
TITLE = "Sign in"
ORDER = 0


def html(_status):
    return ""


def render(csrf, next_path="/", *, message="", username=""):
    """Render the sign-in form; its script posts only to this same manager."""
    message = html_module.escape(message)
    username = html_module.escape(username, quote=True)
    next_path = html_module.escape(next_path, quote=True)
    csrf = html_module.escape(csrf, quote=True)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="csrf-token" content="{csrf}">
<title>Sign in · Matuto</title>
<style nonce="{csrf}">{STYLE}
.auth{{max-width:31rem;margin:2rem auto;padding:0 1rem}}.auth h1{{font-size:2rem}}form{{display:grid;gap:.6rem}}input{{box-sizing:border-box;width:100%;min-width:0;font:inherit;min-height:44px;padding:.6rem;border:1px solid var(--matuto-muted);border-radius:.55rem;background:var(--panel);color:var(--matuto-text)}}.primary{{background:var(--accent);color:var(--ink)}}details{{margin-top:1.5rem}}summary{{min-height:44px;cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}[role=alert]{{color:var(--accent)}}
</style></head><body><a class="skip" href="#main">Skip to content</a><header class="top"><strong>Matuto</strong>{THEME_SWITCH}</header>
<main id="main" class="auth" tabindex="-1"><section class="panel"><h1>Sign in</h1><p>Sign in to continue to the page you requested.</p>
{f'<p role="status">{message}</p>' if message and message != 'The username or password did not match.' else ''}
<form id="login" action="/sign-in" method="post"><input type="hidden" name="next" value="{next_path}">
<label for="username">Username</label><input id="username" name="username" autocomplete="username" required value="{username}">
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<p id="error" role="alert" tabindex="-1">{message if message == 'The username or password did not match.' else ''}</p><button type="submit" class="primary">Sign in</button></form></section>
<p>Signing in lasts seven days. It does not start serving a model.</p>
<details><summary>Forgot your password?</summary><p>Open a terminal in your Linux account and run:</p><pre>matuto-manager reset-login</pre><p>This removes the stored login and ends every browser session. Your next visit shows Create login. There is no password-reset button or email recovery.</p></details>
</main><script src="/auth.js" defer></script></body></html>'''
