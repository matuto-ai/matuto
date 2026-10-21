# Embed the model console with manager-only framing

- Status: Accepted
- Extends: [0002](0002-manage-the-native-router-in-a-browser.md)

> The fixed-address local login and recovery decision amends this one: the primary protection is now the login plus the Host, Origin and Sec-Fetch-Site guards, not a secret capability-derived hostname, which goes with `/auth?cap=` and the capability cookie (AUTH-7). The manager-only console framing decided here stays.

## Question

Should the manager embed llama.cpp's native model console, which requires narrowing the console proxy's clickjacking protection?

## Decision

Embed the console from its existing origin in the manager's Model console tab. The console proxy permits only the capability-derived manager origin on port 8890 through `Content-Security-Policy: frame-ancestors`; it sends no `X-Frame-Options`. The manager remains unframeable and permits only the capability-derived console origin on port 8889 as a frame source.

Sandbox the frame with scripts, forms, downloads, and same-origin behavior, but without top-level navigation. Keep a visible route that opens the console separately when framing is unavailable.

## Reasoning

Loopback binding does not stop clickjacking. A hostile page runs in the operator's browser and can attempt to frame a loopback origin even though another machine cannot connect to it.

The capability-derived hostname is the primary protection because an attacker cannot target an origin they cannot name. The framing policy remains defence in depth if that hostname leaks through browser history, a screenshot, a pasted link, or an extension that can read tab URLs.

Manager and console stay on separate origins. The console runs upstream code, while manager mutation routes retain their own origin and CSRF checks. The proxy still injects the router API key server-side, so neither the frame nor its parent receives that credential.

## Accepted risk

Someone who learns the capability-derived hostname can frame the console from the permitted manager origin or try to trick the operator through a leaked console link. Same-origin policy prevents an unrelated origin from reading the frame, and the manager-only framing policy blocks it from presenting the frame. If that defence fails, the damage is bounded by the console's existing abilities: load, unload, download, or delete a model. A deleted GGUF can be downloaded again; the router API key remains outside the browser.

## Rejected alternative

Serving the console below a manager path would avoid cross-origin framing policy, but it would merge upstream console code into the manager origin and expose manager mutation routes to that code's origin. Preserving origin separation is worth the explicit framing relaxation.

## What would change the answer

Revisit this decision if the console gains access beyond model and chat operations, if the capability-derived hostname stops being secret, or if browser framing behavior prevents the sandboxed console from supporting its required routes.
