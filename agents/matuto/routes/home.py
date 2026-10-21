"""The signed-in app shell at the manager's root address."""

from matuto import serving_state
from matuto.shell import render
from matuto.status import console as console_status

PATH = "/"
CONSOLE_ORIGIN = "http://matuto.localhost:8889"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("the app shell is read-only")
    state = serving_state.read()
    status = {"csrf": request.csrf, "console": console_status.item(state)}
    html = render(status, request.csrf, nonce=request.csrf)
    html = html.replace("</head>", '<link rel="stylesheet" href="/header-status.css"></head>')
    html = html.replace("</body>", '<script src="/header-status.js" defer></script></body>')
    csp = (f"default-src 'self'; script-src 'self' 'nonce-{request.csrf}'; "
           f"style-src 'self' 'nonce-{request.csrf}'; connect-src 'self'; "
           f"frame-src {CONSOLE_ORIGIN}; base-uri 'none'; form-action 'none'; "
           "frame-ancestors 'none'")
    return 200, html, "text/html", {"Content-Security-Policy": csp}
