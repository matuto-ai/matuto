# The `matuto` app package

The browser-first operator app and its `matuto-manager` CLI. Standard library only; runtime, benchmark and voice support used by the app lives in this package, while top-level compatibility entry points keep the lab and WQ working.

## Layout

| Path | Holds |
| --- | --- |
| `cli.py` | the CLI entry |
| `bootstrap.py` | the terminal trust step and verified first install |
| `discovery.py` | the name-based lookups every location uses |
| `shell.py` | the shell every page shares: page list, narrow menu, shared slots, the light/dark switch, notice helpers and the one popup component |
| `login.py` | the one stored login: create, check, change password, reset |
| `sessions.py` | the browser sessions: start after a password check, keep seven days, sign out one |
| `page/sign_in.py`, `page/create_login.py` | the signed-out forms |
| `static/auth.js` | the same-origin script used by both signed-out forms |
| `admission.py` | the pure slot, card-lease and serving rules; it starts, stops and records nothing |
| `runner.py` | the one runner: every run, build and download as a transient systemd user unit, recorded in the store, stopped as a whole unit, and kept in a private per-run directory whose evidence moves into the store when it ends |
| `redaction.py` | the one rule for a stored or shown log tail: bounded by its caller, with every credential-carrying line replaced whole |
| `context_search.py` | the pure context-search rules: the ladder, the offered rungs, the order and the verdict; it runs, records and changes nothing |
| `recipes.py` | the four fixed runtime build recipes: canonical origin, configure options and server target |
| `progress.py` | the phase and step a unit reports through the runner's one interface, and the bounded, redacted tail of its own log |
| `runtime_builds.py` | per-card HIP target lookup, detectable recipe prerequisites and the gated build-plan seam |
| `runtime_selection.py` | validate and atomically save the selected llama.cpp build, backend and card |
| `arguments.py` | the profile argument validator |
| `releases.py` | the signed release list and release-file checks: OpenSSH signature, rising number, withdrawn mark, size and SHA-256 |
| `store.py` | the one SQLite store: its schema, references and refusals |
| `serving.py` | the pure serving transitions: the controls per state, each refusal and each confirmation; it starts, stops and records nothing |
| `serving_state.py` | the serving-state reader: the router and voice units, the router's health and model list, and the voice record; it starts, stops and loads nothing |
| `console.py` | the separate loopback model-console proxy: session, Host, fetch-site, Origin, mutation, serving-state, request-size and router-key protections |
| `versions.py` | the pure official-version check: returned refs, resolution to a full commit and the recipe/target pin; it runs only when called |
| `installation.py` | the check of one runtime installation at its final path: loader paths before probes |
| `registry.py` | the llama.cpp build registry: register, forget, discover and inspect installation trees |
| `error_page.py` | the private error page the Matuto entry writes when the manager is down: bounded, redacted and escaped |
| `verification.py` | an acquisition record's file identity: the full-set Verify, its consent and replacement rules, and the launch-time check |
| `adoption.py` | adopt complete local model files from configured artifact locations without moving them |
| `commands/` | one CLI command per file |
| `procedures/` | one run procedure per file, discovered by its name and passed one stored record |
| `procedure_support.py` | shared store-backed setup and private evidence writing for run procedures |
| `routes/` | one HTTP route per file |
| `page/` | one page section per file |
| `status/` | one status provider per file |

## Conventions

- One item per file, and the file name is the item name: `routes/status.py` is the `/api/status` route and `commands/list_runs.py` is the `list_runs` command.
- An item file is found by name through `discovery.item_names(location)` and `discovery.load_item(location, name)`. Adding an item therefore never edits a shared list, registry or `validate.sh`.
- An item file is loaded by path rather than imported as a package submodule, so it imports the rest of the package by its absolute name (`from matuto.discovery import ...`) and never relatively.
- The CLI entry is installed under the `matuto-manager` name.
- A page section under `page/` declares the `PAGE` it belongs to, its `NAME` (its id and route), its `TITLE`, its `ORDER` in that page's tab row and `html(status)`. `shell.py` renders the page's sections as a tab row under the heading, each with the route `#/<page>/<name>`, in `ORDER` then name order, so adding a section edits neither the row nor the shell.
- A page's script puts problems in the two helpers the shell script already defines, instead of inventing a place for them: `notice(page, key, kind, title, detail, link)` writes one page-wide blocker, warning or repair notice into that page's single notice region (`kind` is `blocker`, `warning` or `repair`), replacing the notice that already carries `key`, and `clearNotice(page, key)` leaves it when the condition has cleared; `reason(control, text)` states one unavailable control's reason beside it as its `aria-describedby` description, marks it `aria-disabled` and drops a `disabled` attribute rather than leaving the control unfocusable, and `reason(control, '')` clears both.

## Tests

`./validate.sh` runs every Python test `unittest` discovery finds under `agents/tests/app/` and every `agents/tests/app/*.test.cjs` page test there. A new test file is picked up by being placed there; `validate.sh` names no individual test.

## Routes and requests

`matuto serve` binds the manager at 127.0.0.1:8890 and the model console at 127.0.0.1:8889. The console proxy in `matuto/console.py` forwards to the router only for a valid session during healthy agentic serving; the console's own response policy allows framing only by the manager origin. The manager serves every file in `routes/` behind the browser protections in `matuto/web.py`; a route file cannot opt out. Each route file declares its `PATH` and a `handle`:

```python
PATH = "/api/status"


def handle(request):
    return 200, {"state": "ready"}
```

`request` is a `matuto.web.Request` named tuple of `method`, `path`, `query`, `headers`, `body` and `csrf`. `handle` returns `(status, body, content_type)` or adds a response-header mapping as the fourth item; a fifth callable runs after the response body is written and is reserved for a short response-handoff acknowledgment. `content_type` defaults to JSON and `body` is a mapping, text or bytes. Raising `matuto.web.RefusedError` answers a JSON error. Every route is session-gated by default; only the fixed sign-in, Create login and form-script paths are public, and form POSTs still require the browser protections. Signed-out page requests show the appropriate form, while API requests get a JSON refusal. `matuto.web.session_cookie(value)` is the package's only way to set a session cookie, HttpOnly, SameSite=Strict and host-only.

`csrf` is the one hand-off of the CSRF token: a page reads it from the request and sends it back in the `X-CSRF-Token` header, which every method other than GET and HEAD needs beside an exact `Origin: http://matuto.localhost:8890`.