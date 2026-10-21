"""Qualification acceptance and withdrawal through the protected browser API."""

import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
APP_TESTS = AGENTS / "tests" / "app"
for path in (AGENTS, APP_TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from matuto import login, sessions, store
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server
from test_store import StoreTestCase


class AcceptanceHttpTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        fake_bin = self.home / "bin"
        fake_bin.mkdir()
        systemctl = fake_bin / "systemctl"
        systemctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n", encoding="utf-8")
        systemctl.chmod(0o700)
        self.systemctl_log = self.home / "systemctl.log"
        self.systemctl_environment = mock.patch.dict(os.environ, {
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            "SYSTEMCTL_LOG": str(self.systemctl_log),
        })
        self.systemctl_environment.start()
        self.addCleanup(self.systemctl_environment.stop)
        self.opened = self.open_store()
        connection = self.opened.connection()
        self.fill(connection)
        record = store.read_record(self.opened, "language", "prof-coder")
        fingerprint = store.fingerprint(self.opened, record)
        connection.execute("UPDATE runs SET fingerprint = ? WHERE run_id = 'run-smoke-1'", (fingerprint,))
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("run-bench-1", "language", "prof-coder", "Qwen3.6 Coder 192K", fingerprint,
             "build-hip", "bench", "2026.10.1", "matuto-lab-bench.service", "completed", 0,
             "bench complete", "", 0, "2026-10-06T01:00:00+00:00", "2026-10-06T01:05:00+00:00"))
        connection.execute(
            "INSERT INTO checks (run_id, sequence, name, status, detail)"
            " VALUES ('run-bench-1', 0, 'measurement', 'FAIL', 'slow')")
        connection.execute(
            "INSERT INTO builds VALUES ('build-vk', 'Vulkan test build', 'llama-vulkan',"
            " '/tmp/build-vk', 'vulkan', 'revision', NULL, 'llama-vulkan', '', 'registered', '')")
        connection.execute("UPDATE runs SET build_id = 'build-vk' WHERE run_id = 'run-bench-1'")
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("run-interrupted", "language", "prof-coder", "Qwen3.6 Coder 192K", fingerprint,
             "build-hip", "smoke", "2026.10.1", "matuto-lab-smoke.service", "interrupted", 0,
             "", "", 0, "2026-10-06T02:00:00+00:00", "2026-10-06T02:01:00+00:00"))
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("run-stale", "language", "prof-coder", "Qwen3.6 Coder 192K", "sha256:" + "0" * 64,
             "build-hip", "bench", "2026.10.1", "matuto-lab-bench.service", "completed", 0,
             "", "", 0, "2026-10-06T03:00:00+00:00", "2026-10-06T03:01:00+00:00"))
        self.add_run("run-errored", "language", "prof-coder", "Qwen3.6 Coder 192K",
                     fingerprint, "bench", outcome="errored")
        self.add_run("run-cancelled", "language", "prof-coder", "Qwen3.6 Coder 192K",
                     fingerprint, "bench", outcome="cancelled")
        self.add_run("run-legacy", "language", "prof-coder", "Qwen3.6 Coder 192K",
                     "", "bench", build_id="")
        login.create_login("operator", "secret", "secret")
        self.token, _ = sessions.start_session("operator", "secret")
        self.server = build_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf

    def request(self, path, *, method="GET", token=None, csrf=True, origin=True, body=None):
        headers = {"Host": ALLOWED_HOST}
        if token:
            headers["Cookie"] = f"{SESSION_COOKIE}={token}"
        data = None
        if method != "GET":
            if origin:
                headers["Origin"] = MANAGER_ORIGIN
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
            headers["Content-Type"] = "application/json"
            data = json.dumps(body or {}).encode()
        request = urllib.request.Request(self.address + path, method=method,
                                         headers=headers, data=data)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def make_bundle_citable(self):
        connection = self.opened.connection()
        connection.execute(
            "INSERT INTO profile_artifacts VALUES ('prof-breeze', 'weights', 'acq-qwen36',"
            " 'Qwen3.6-35B-A3B-Q4_K_M.gguf')")
        connection.execute(
            "INSERT INTO speech_executables VALUES ('exe-whisper', 'recognition', 'loc-models',"
            " '/var/lib/matuto/models/whisper/bin/whisper', ?, '1.0.0', '',"
            " '2026-10-01T00:00:00+00:00')", ("b" * 64,))
        connection.execute("UPDATE model_profiles SET speech_executable_id = 'exe-whisper'"
                           " WHERE profile_id = 'prof-whisper'")
        connection.execute(
            "INSERT INTO profile_artifacts VALUES ('prof-whisper', 'weights', 'acq-qwen36',"
            " 'Qwen3.6-35B-A3B-Q4_K_M.gguf')")
        return store.read_record(self.opened, "bundle", "bundle-voice")

    def add_run(self, run_id, kind, record_id, label, fingerprint, procedure, *, outcome="completed",
                build_id="build-hip", checks=()):
        self.opened.connection().execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, kind, record_id, label, fingerprint, build_id, procedure, "2026.10.1",
             "matuto-lab-" + procedure + ".service", outcome, 0, "", "", 0,
             "2026-10-06T04:00:00+00:00", "2026-10-06T04:01:00+00:00"))
        for sequence, (name, status, detail) in enumerate(checks):
            self.opened.connection().execute(
                "INSERT INTO checks (run_id, sequence, name, status, detail) VALUES (?, ?, ?, ?, ?)",
                (run_id, sequence, name, status, detail))

    def test_joined_profile_journey_from_overview_through_publication_to_acceptance(self):
        from http.cookies import SimpleCookie
        from types import SimpleNamespace
        from matuto import admission, serving_state

        config = self.home / "joined-home"
        fake_bin = self.home / "joined-bin"
        units = self.home / "fake-systemd-units"
        runtime_root = self.home / "runtime-fixture"
        fake_bin.mkdir()
        units.mkdir()
        subprocess.run([sys.executable, "-B", str(AGENTS / "tests" / "runtime_fixture.py"),
                        str(runtime_root)], check=True)
        runtime_bin = runtime_root / "build" / "bin"
        systemctl = fake_bin / "systemctl"
        systemctl.write_text(f'''#!{sys.executable}
import os, sys
args = sys.argv[1:]
if args and args[0] == "--user": args = args[1:]
command = args[0] if args else ""
unit = args[1] if len(args) > 1 else ""
path = os.path.join(os.environ["FAKE_UNIT_DIR"], unit)
if command == "list-units": raise SystemExit(0)
if command in ("start", "stop"):
    for name in args[1:]:
        with open(os.path.join(os.environ["FAKE_UNIT_DIR"], name), "w") as state:
            state.write("active" if command == "start" else "inactive")
    raise SystemExit(0)
if command == "show":
    state = open(path).read().strip() if os.path.exists(path) else "inactive"
    print("LoadState=loaded")
    print("ActiveState=" + state)
    print("SubState=" + ("running" if state == "active" else "dead"))
    print("Result=success")
    print("ExecMainStatus=0")
    print("EndMonotonicUSec=1")
    raise SystemExit(0)
raise SystemExit(2)
''', encoding="utf-8")
        systemctl.chmod(0o700)
        systemd_run = fake_bin / "systemd-run"
        systemd_run.write_text(f'''#!{sys.executable}
import hashlib, os, shutil, subprocess, sys
from pathlib import Path
from matuto import store
args = sys.argv[1:]
unit = next(item.split("=", 1)[1] for item in args if item.startswith("--unit="))
procedure = unit.split("-")[1].split(".")[0]
assert shutil.which("llama-cli")
subprocess.run(["llama-cli", "--version"], check=True, capture_output=True, timeout=5)
opened = store.open_store()
try:
    connection = opened.connection()
    run = connection.execute("SELECT run_id, procedure, outcome FROM runs"
                             " WHERE procedure = ? AND outcome IS NULL"
                             " ORDER BY started_at DESC LIMIT 1", (procedure,)).fetchone()
    assert run is not None
    body = b"fixture work passed\\n"
    check = "measurement" if run["procedure"] == "bench" else "exact reply"
    store.write_run_evidence(connection, run["run_id"], outcome="completed",
        ended_at="2026-10-09T17:00:00+00:00", summary="fixture work passed",
        log="fixture runtime completed", log_truncated=False,
        checks=[(0, check, "PASS", "")],
        artifacts=[("report.md", "text/markdown", len(body), hashlib.sha256(body).hexdigest(), False, body)])
finally:
    opened.close()
Path(os.path.join(os.environ["FAKE_UNIT_DIR"], unit)).write_text("inactive")
''', encoding="utf-8")
        systemd_run.chmod(0o700)
        environment = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(config),
            "PATH": os.pathsep.join((str(fake_bin), str(runtime_bin), os.environ.get("PATH", ""))),
            "PYTHONPATH": str(AGENTS),
            "FAKE_UNIT_DIR": str(units),
        })
        environment.start()
        self.addCleanup(environment.stop)
        opened = store.open_store()
        opened.close()
        server = build_server(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        address = f"http://127.0.0.1:{server.server_address[1]}"
        csrf = server.RequestHandlerClass.csrf

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, file, code, message, headers, new_url):
                return None

        opener = urllib.request.build_opener(NoRedirect())

        def request(path, *, method="GET", token=None, body=None):
            headers = {"Host": ALLOWED_HOST}
            if token:
                headers["Cookie"] = f"{SESSION_COOKIE}={token}"
            data = None
            if body is not None:
                headers.update({"Origin": MANAGER_ORIGIN, "X-CSRF-Token": csrf,
                               "Content-Type": "application/json"})
                data = json.dumps(body).encode()
            outgoing = urllib.request.Request(address + path, method=method, headers=headers, data=data)
            try:
                response = opener.open(outgoing, timeout=10)
            except urllib.error.HTTPError as error:
                response = error
            raw = response.read()
            payload = (json.loads(raw) if response.headers.get_content_type() == "application/json"
                       else raw.decode("utf-8", "replace"))
            return response.status, payload, response.headers

        def resolve_links(value, shell, known_profiles=()):
            routes = set()
            def visit(item):
                if isinstance(item, dict):
                    for key, child in item.items():
                        if key == "href": routes.add(child)
                        else: visit(child)
                elif isinstance(item, list):
                    for child in item: visit(child)
            visit(value)
            pages = set(re.findall(r'id="page-([^"]+)"', shell))
            route_files = [path.read_text(encoding="utf-8") for path in
                           (AGENTS / "matuto" / "routes").glob("*.py")]
            api_paths = {match for source in route_files
                         for match in re.findall(r'^PATH = ["\']([^"\']+)', source, re.M)}
            for href in routes:
                if href.startswith("#/"):
                    target = urllib.parse.urlsplit(href[1:])
                    parts = target.path.strip("/").split("/")
                    self.assertIn(parts[0], pages, href)
                    if len(parts) > 1:
                        start = shell.index(f'id="page-{parts[0]}"')
                        end = shell.find('<div class="page"', start + 1)
                        block = shell[start:end if end >= 0 else None]
                        self.assertIn(f'data-section="{parts[1]}"', block, href)
                    profile_id = urllib.parse.parse_qs(target.query).get("profile", [None])[0]
                    if profile_id:
                        self.assertIn(profile_id, known_profiles, href)
                elif href.startswith("/api/"):
                    self.assertIn(href.split("?", 1)[0], api_paths, href)
                else:
                    self.fail(f"unresolved in-app link: {href}")
            return routes

        try:
            status, _body, headers = request("/create-login", method="POST", body={
                "username": "journey", "password": "secret", "confirm": "secret", "next": "/",
            })
            self.assertEqual(status, 303)
            cookie = SimpleCookie()
            cookie.load(headers["Set-Cookie"])
            token = cookie[SESSION_COOKIE].value
            status, shell, _ = request("/", token=token)
            self.assertEqual(status, 200)
            status, first_use, _ = request("/api/overview", token=token)
            self.assertEqual(status, 200, first_use)
            destinations = {item["label"]: item["href"] for item in first_use["tasks"]}
            self.assertEqual(destinations, {
                "Set model storage": "#/settings/storage-artifacts",
                "Get model files": "#/model-files/acquisition",
                "Prepare the runtime": "#/runtime/install",
                "Create or import a language profile": "#/profiles/language",
            })
            acquisition, profiles = request("/api/acquisition", token=token)[1], \
                request("/api/language-profiles", token=token)[1]
            resolve_links(first_use, shell)
            resolve_links(acquisition, shell)
            resolve_links(profiles, shell)
            journey_payloads = [first_use, acquisition, profiles]
            for path in ("/api/storage", "/api/runtime-install", "/api/builds"):
                status, page, _ = request(path, token=token)
                self.assertEqual(status, 200, page)
                resolve_links(page, shell)
                journey_payloads.append(page)
            self.assertNotIn("Stop voice", shell)
            self.assertNotIn("Change login", shell)
            self.assertNotIn("Log out", shell)
            self.assertNotIn("No job running", shell)

            model_root = self.home / "joined-models"
            model_root.mkdir()
            model_name = "Qwen3.6-35B-A3B-Q4_K_M.gguf"
            (model_root / model_name).write_bytes(b"issue-283 fixture model")
            status, storage_result, _ = request("/api/storage", method="POST", token=token, body={
                "action": "save-model-storage", "path": str(model_root),
            })
            self.assertEqual(status, 200, storage_result)
            self.assertTrue(storage_result["available"])
            journey_payloads.append(storage_result)
            status, storage_page, _ = request("/api/storage", token=token)
            self.assertEqual(status, 200, storage_page)
            resolve_links(storage_page, shell)
            journey_payloads.append(storage_page)
            status, after_storage, _ = request("/api/overview", token=token)
            self.assertEqual(status, 200, after_storage)
            self.assertEqual({item["label"] for item in after_storage["tasks"]}, {
                "Get model files", "Prepare the runtime", "Create or import a language profile",
            })
            resolve_links(after_storage, shell)
            journey_payloads.append(after_storage)
            status, adopted, _ = request("/api/acquisition", method="POST", token=token, body={
                "action": "adopt", "location_id": storage_result["location_id"],
                "file_name": model_name, "name": "Qwen3.6 fixture", "consent": True,
            })
            self.assertEqual(status, 200, adopted)
            self.assertTrue(adopted["complete"])
            journey_payloads.append(adopted)
            status, after_model, _ = request("/api/overview", token=token)
            self.assertEqual(status, 200, after_model)
            self.assertEqual({item["label"] for item in after_model["tasks"]}, {
                "Prepare the runtime", "Create or import a language profile",
            })
            resolve_links(after_model, shell)
            journey_payloads.append(after_model)

            gpu = {"pciAddress": "0000:05:00.0", "vendor": "0x1002", "device": "0x7550",
                   "class": "0x030000", "subsystem_vendor": "0x1002",
                   "subsystem_device": "0x0001", "vramBytes": 34359738368,
                   "displayName": "Fixture GPU"}
            def detect_fixture(opened, **_kwargs):
                devices = [dict(gpu, offered=not opened.connection().execute(
                    "SELECT 1 FROM cards WHERE gpu_identity = ?", (gpu["pciAddress"],)).fetchone(),
                    gfxTarget="gfx1201")]
                cards = [dict(row, detected=True) for row in opened.connection().execute(
                    "SELECT * FROM cards ORDER BY card_id")]
                return {"state": "completed", "devices": devices, "cards": cards}

            with mock.patch("matuto.cards.detect", side_effect=detect_fixture), \
                    mock.patch("matuto.runtime_builds._host_targets",
                               return_value=("completed", {gpu["pciAddress"]: "gfx1201"}, None)), \
                    mock.patch("matuto.runtime_selection._validate_registered_device",
                               return_value=True), \
                    mock.patch("matuto.serving_state.read",
                               return_value=serving_state.State(admission.STOPPED)):
                status, card_result, _ = request("/api/cards", method="POST", token=token, body={
                    "action": "add", "card_id": "journey-card", "name": "Fixture GPU",
                    "gpu_identity": gpu["pciAddress"], "gpu": gpu,
                    "vram_ceiling_bytes": gpu["vramBytes"], "default_gpu_layers": "all",
                    "smoke_output_tokens": 16, "bench_prompt_tokens": 4096,
                    "bench_generation_tokens": 512, "bench_repetitions": 5,
                })
                self.assertEqual(status, 201, card_result)

                build_tree = runtime_root / "build"
                opened = store.open_store()
                try:
                    connection = opened.connection()
                    connection.execute(
                        "INSERT INTO builds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("build-hip", "Fixture llama.cpp", "llama-hip", str(build_tree), "hip",
                         "fixture-revision", "gfx1201", "llama-hip", "fixture toolchain",
                         "registered", ""))
                    connection.execute(
                        "INSERT INTO installations VALUES (?, ?, ?, ?, ?, 'checked', '', '{}', ?)",
                        (str(build_tree), "llama.cpp HIP", "fixture-revision", "gfx1201",
                         "fixture toolchain", "2026-10-05T00:00:00+00:00"))
                finally:
                    opened.close()
                status, selected, _ = request("/api/builds", method="POST", token=token, body={
                    "action": "select", "build_id": "build-hip", "backend": "hip",
                    "card_id": "journey-card",
                })
                self.assertEqual(status, 200, selected)
                journey_payloads.append(selected)
                status, after_runtime, _ = request("/api/overview", token=token)
                self.assertEqual(status, 200, after_runtime)
                self.assertEqual({item["label"] for item in after_runtime["tasks"]}, {
                    "Create or import a language profile",
                })
                resolve_links(after_runtime, shell)
                journey_payloads.append(after_runtime)

                profile_record = {
                    "kind": "language", "profile_id": "prof-coder",
                    "name": "Qwen3.6 Coder 192K", "alias": "qwen3-coder",
                    "context_size": 131072, "thinking": "high", "card_id": "journey-card",
                    "notes": "fixture-backed joined journey",
                    "model": {"acquisition_id": adopted["acquisition_id"], "file_name": model_name},
                    "mmproj": None, "mtp": None, "arguments": [],
                }
                status, created_profile, _ = request("/api/language-profiles", method="POST",
                                                      token=token, body=profile_record)
                self.assertEqual(status, 201, created_profile)
                self.assertEqual(created_profile["profile_id"], "prof-coder")
                journey_payloads.append(created_profile)
            status, configured, _ = request("/api/overview", token=token)
            self.assertEqual(status, 200, configured)
            self.assertEqual(configured["tasks"], [])
            self.assertEqual(configured["configured"], [])
            journey_payloads.append(configured)
            status, profiles, _ = request("/api/language-profiles", token=token)
            self.assertEqual(status, 200, profiles)
            profile_ids = {item["profile_id"] for item in profiles["profiles"]}
            candidate = next(item for item in profiles["profiles"] if item["profile_id"] == "prof-coder")
            self.assertEqual(candidate["qualification"]["state"], "candidate")
            journey_payloads.append(profiles)
            status, published, _ = request("/api/language-profile-actions", method="POST", token=token,
                body={"action": "publish", "profile_id": "prof-coder",
                      "confirmation": "Publish Qwen3.6 Coder 192K as not qualified"})
            self.assertEqual(status, 200, published)
            journey_payloads.append(published)

            current = [serving_state.State(admission.STOPPED)]
            def start_router(*_args):
                subprocess.run(["systemctl", "--user", "start", serving_state.ROUTER_UNIT], check=True)
                current[0] = serving_state.State(admission.AGENTIC, healthy=True)
            def stop_router():
                subprocess.run(["systemctl", "--user", "stop", serving_state.ROUTER_UNIT], check=True)
                current[0] = serving_state.State(admission.STOPPED)
                return True, "stopped"

            with mock.patch("matuto.serving_state.read", side_effect=lambda *_args, **_kwargs: current[0]), \
                    mock.patch("matuto.router.start_unit", side_effect=start_router), \
                    mock.patch("matuto.router.wait_ready", side_effect=lambda *_args, **_kwargs: current[0]), \
                    mock.patch("matuto.router.stop_unit", side_effect=stop_router), \
                    mock.patch("matuto.router.card_holder",
                               return_value=admission.Card(admission.CARD_FREE)):
                status, serving, _ = request("/api/overview/serving", token=token)
                self.assertEqual(status, 200, serving)
                journey_payloads.append(serving)
                self.assertEqual(serving["profiles"][0]["qualification"]["state"], "candidate")
                resolve_links(serving, shell, profile_ids)
                status, started, _ = request("/api/serving/start", method="POST", token=token, body={})
                self.assertEqual(status, 200, started)
                journey_payloads.append(started)
                self.assertEqual(started["state"], admission.AGENTIC)
                status, offered, _ = request("/api/runs", method="POST", token=token, body={
                    "action": "start", "record": "prof-coder", "procedure": "smoke",
                })
                self.assertEqual(status, 409, offered)
                journey_payloads.append(offered)
                self.assertTrue(offered["offer"])
                status, preview, _ = request("/api/serving/stop", method="POST", token=token,
                                              body={"confirm": False})
                self.assertEqual(status, 200, preview)
                journey_payloads.append(preview)
                self.assertFalse(preview["acted"])
                status, stopped, _ = request("/api/serving/stop", method="POST", token=token,
                                              body={"confirm": True})
                self.assertEqual(status, 200, stopped)
                journey_payloads.append(stopped)
                self.assertTrue(stopped["stopped"])
                status, run, _ = request("/api/runs", method="POST", token=token, body={
                    "action": "start", "record": "prof-coder", "procedure": "smoke",
                })
                self.assertEqual(status, 202, run)
                journey_payloads.append(run)
                status, runs, _ = request("/api/runs", token=token)
                self.assertEqual(status, 200, runs)
                own_run = next(item for item in runs["entries"] if item["id"] == run["work"])
                self.assertEqual(own_run["outcome"], "completed")
                journey_payloads.append(runs)
                bench_status, bench_run, _ = request("/api/runs", method="POST", token=token, body={
                    "action": "start", "record": "prof-coder", "procedure": "bench",
                })
                self.assertEqual(bench_status, 202, bench_run)
                status, runs, _ = request("/api/runs", token=token)
                self.assertEqual(status, 200, runs)
                own_bench = next(item for item in runs["entries"] if item["id"] == bench_run["work"])
                self.assertEqual(own_bench["outcome"], "completed")
                journey_payloads.append(runs)
                run_route = f"#/runs?profile={urllib.parse.quote('prof-coder')}"
                resolve_links({"href": run_route}, shell, profile_ids)

                status, acceptance, _ = request("/api/acceptance?kind=language&id=prof-coder",
                                                token=token)
                self.assertEqual(status, 200, acceptance)
                journey_payloads.append(acceptance)
                smoke_evidence = next(item for item in acceptance["runs"]
                                      if item["procedure"] == "smoke")
                bench_evidence = next(item for item in acceptance["runs"]
                                      if item["procedure"] == "bench")
                self.assertTrue(smoke_evidence["citable"], smoke_evidence)
                self.assertTrue(bench_evidence["citable"], bench_evidence)
                self.assertEqual(smoke_evidence["run_id"], run["work"])
                self.assertEqual(bench_evidence["run_id"], bench_run["work"])
                self.assertTrue(smoke_evidence["checks"], smoke_evidence)
                self.assertTrue(all(check["status"] == "PASS"
                                    for check in smoke_evidence["checks"]), smoke_evidence)
                self.assertTrue(bench_evidence["checks"], bench_evidence)
                self.assertTrue(all(check["status"] == "PASS"
                                    for check in bench_evidence["checks"]), bench_evidence)
                status, result, _ = request("/api/acceptance", method="POST", token=token, body={
                    "action": "accept", "kind": "language", "id": "prof-coder",
                    "run_ids": [smoke_evidence["run_id"], bench_evidence["run_id"]],
                    "scope": "single consumer",
                    "exceptions": {}, "residual_risks": "fixture runtime only",
                })
                self.assertEqual(status, 200, result)
                journey_payloads.append(result)
                status, serving, _ = request("/api/overview/serving", token=token)
                self.assertEqual(status, 200, serving)
                journey_payloads.append(serving)
                self.assertEqual(serving["profiles"][0]["qualification"]["state"], "qualified")
            rendered = shell + json.dumps(journey_payloads, ensure_ascii=False)
            for forbidden in ("Stop voice", "Change login", "Log out", "No job running"):
                self.assertNotIn(forbidden, rendered)
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()

    def test_acceptance_history_withdrawal_and_session_identity_use_browser_api(self):
        status, data = self.request("/api/acceptance?kind=language&id=prof-coder", token=self.token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["record"]["state"]["state"], "candidate")
        runs = {run["run_id"]: run for run in data["runs"]}
        self.assertTrue(runs["run-smoke-1"]["citable"])
        self.assertTrue(runs["run-bench-1"]["citable"])
        for run_id, reason in (("run-interrupted", "interrupted"), ("run-errored", "errored"),
                               ("run-cancelled", "cancelled"), ("run-legacy", "fingerprint")):
            self.assertFalse(runs[run_id]["citable"])
            self.assertIn(reason, runs[run_id]["reason"])
        self.assertFalse(runs["run-stale"]["citable"])
        self.assertIn("fingerprint", runs["run-stale"]["reason"])
        self.assertIn("Vulkan test build", runs["run-bench-1"]["build_warning"])

        status, result = self.request("/api/acceptance", method="POST", token=self.token, body={
            "action": "accept", "kind": "language", "id": "prof-coder",
            "run_ids": ["run-smoke-1", "run-bench-1"], "scope": "single consumer",
            "exceptions": {"measurement": "thermal limit"}, "residual_risks": "slow bench",
            "username": "attacker", "accepted_by": "attacker", "accepted_at": "forged",
        })
        self.assertEqual(status, 200, result)
        self.assertEqual(result["acceptance"]["accepted_by"], "operator")
        self.assertNotEqual(result["acceptance"]["accepted_at"], "forged")

        status, data = self.request("/api/acceptance?kind=language&id=prof-coder", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(data["record"]["state"]["state"], "qualified")
        acceptance = data["acceptances"][-1]
        self.assertTrue(acceptance["withdrawable"])
        self.assertEqual(acceptance["exceptions"], [
            {"check_name": "measurement", "reason": "thermal limit"}])
        self.assertEqual({run["run_id"] for run in acceptance["runs"]},
                         {"run-smoke-1", "run-bench-1"})

        status, result = self.request("/api/acceptance", method="POST", token=self.token, body={
            "action": "withdraw", "kind": "language", "id": "prof-coder",
            "acceptance_id": acceptance["acceptance_id"],
        })
        self.assertEqual(status, 200, result)
        self.assertTrue(result["acceptance"]["withdrawn_at"])
        status, data = self.request("/api/acceptance?kind=language&id=prof-coder", token=self.token)
        self.assertEqual(data["record"]["state"]["state"], "candidate")
        self.assertFalse(data["acceptances"][-1]["withdrawable"])
        status, result = self.request("/api/runs", method="POST", token=self.token, body={
            "action": "delete", "id": "run-smoke-1",
        })
        self.assertEqual(status, 409, result)
        self.assertIn("acceptance", result["error"])
        self.assertTrue(self.opened.connection().execute(
            "SELECT 1 FROM runs WHERE run_id = 'run-smoke-1'").fetchone())
        self.assertFalse(self.systemctl_log.exists())

        profile = store.read_record(self.opened, "language", "prof-coder")
        fingerprint = store.fingerprint(self.opened, profile)
        self.add_run("run-wq-failed", "language", "prof-coder", profile["name"], fingerprint,
                     "wq", checks=(("retrieval quality", "ERROR", "missed"),))
        data = self.request("/api/acceptance?kind=language&id=prof-coder", token=self.token)[1]
        self.assertTrue(next(run for run in data["runs"] if run["run_id"] == "run-wq-failed")["citable"])
        common = {"action": "accept", "kind": "language", "id": "prof-coder",
                  "run_ids": ["run-smoke-1", "run-bench-1", "run-wq-failed"],
                  "scope": "single consumer", "residual_risks": "quality varied"}
        status, result = self.request("/api/acceptance", method="POST", token=self.token,
                                      body={**common, "exceptions": {}})
        self.assertEqual(status, 409, result)
        self.assertIn("retrieval quality", result["error"])
        status, result = self.request("/api/acceptance", method="POST", token=self.token,
                                      body={**common, "exceptions": {"measurement": "thermal limit",
                                                                      "retrieval quality": "reviewed"}})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["acceptance"]["exceptions"], {
            "measurement": "thermal limit", "retrieval quality": "reviewed"})
        self.assertFalse(self.systemctl_log.exists())

    def test_http_acceptance_refuses_missing_minimum_required_failures_and_stale_forms(self):
        profile = store.read_record(self.opened, "language", "prof-coder")
        fingerprint = store.fingerprint(self.opened, profile)
        before = self.opened.connection().execute("SELECT COUNT(*) FROM acceptances").fetchone()[0]
        status, result = self.request("/api/acceptance", method="POST", token=self.token, body={
            "action": "accept", "kind": "language", "id": "prof-coder",
            "run_ids": ["run-smoke-1"], "scope": "single consumer", "exceptions": {},
            "residual_risks": "none",
        })
        self.assertEqual(status, 409, result)
        self.assertIn("bench", result["error"])

        self.add_run("run-smoke-failed", "language", "prof-coder", profile["name"], fingerprint,
                     "smoke", checks=(("exact reply", "FAIL", "no reply"),))
        status, data = self.request("/api/acceptance?kind=language&id=prof-coder", token=self.token)
        self.assertFalse(next(run for run in data["runs"] if run["run_id"] == "run-smoke-failed")["citable"])
        status, result = self.request("/api/acceptance", method="POST", token=self.token, body={
            "action": "accept", "kind": "language", "id": "prof-coder",
            "run_ids": ["run-smoke-failed", "run-bench-1"], "scope": "single consumer",
            "exceptions": {"exact reply": "ignore it"}, "residual_risks": "none",
        })
        self.assertEqual(status, 409, result)
        self.assertIn("cannot be cited", result["error"])

        bundle = self.make_bundle_citable()
        bundle_fingerprint = store.fingerprint(self.opened, bundle)
        self.add_run("run-bundle-failed", "bundle", "bundle-voice", bundle["name"],
                     bundle_fingerprint, "bundle", checks=(("voice response", "FAIL", "wrong"),))
        status, data = self.request("/api/acceptance?kind=bundle&id=bundle-voice", token=self.token)
        failed = next(run for run in data["runs"] if run["run_id"] == "run-bundle-failed")
        self.assertFalse(failed["citable"])
        status, result = self.request("/api/acceptance", method="POST", token=self.token, body={
            "action": "accept", "kind": "bundle", "id": "bundle-voice",
            "run_ids": ["run-bundle-failed"], "scope": "voice role",
            "exceptions": {"voice response": "ignore it"}, "residual_risks": "none",
        })
        self.assertEqual(status, 409, result)
        self.assertIn("cannot be cited", result["error"])
        self.assertEqual(self.opened.connection().execute("SELECT COUNT(*) FROM acceptances").fetchone()[0], before)

        self.add_run("run-bundle-pass", "bundle", "bundle-voice", bundle["name"],
                     bundle_fingerprint, "bundle", checks=(("voice response", "PASS", "clear"),))
        status, result = self.request("/api/acceptance", method="POST", token=self.token, body={
            "action": "accept", "kind": "bundle", "id": "bundle-voice",
            "run_ids": ["run-bundle-pass"], "scope": "voice role",
            "exceptions": {}, "residual_risks": "microphone noise",
        })
        self.assertEqual(status, 200, result)
        data = self.request("/api/acceptance?kind=bundle&id=bundle-voice", token=self.token)[1]
        self.assertEqual(data["record"]["state"]["state"], "qualified")
        status, result = self.request("/api/acceptance", method="POST", token=self.token, body={
            "action": "withdraw", "kind": "bundle", "id": "bundle-voice",
            "acceptance_id": data["acceptances"][-1]["acceptance_id"],
        })
        self.assertEqual(status, 200, result)
        data = self.request("/api/acceptance?kind=bundle&id=bundle-voice", token=self.token)[1]
        self.assertEqual(data["record"]["state"]["state"], "candidate")
        self.assertTrue(data["acceptances"][-1]["withdrawn_at"])
        self.assertFalse(self.systemctl_log.exists())
        before_race = self.opened.connection().execute("SELECT COUNT(*) FROM acceptances").fetchone()[0]

        status, _ = self.request("/api/acceptance?kind=language&id=prof-coder", token=self.token)
        self.opened.connection().execute(
            "UPDATE profile_arguments SET value = '2' WHERE profile_id = 'prof-coder' AND flag = '--parallel'")
        changed = store.read_record(self.opened, "language", "prof-coder")
        self.assertNotEqual(store.fingerprint(self.opened, changed), fingerprint)
        status, result = self.request("/api/acceptance", method="POST", token=self.token, body={
            "action": "accept", "kind": "language", "id": "prof-coder",
            "run_ids": ["run-smoke-1", "run-bench-1"], "scope": "single consumer",
            "exceptions": {"measurement": "thermal limit"}, "residual_risks": "slow bench",
        })
        self.assertEqual(status, 409, result)
        self.assertIn("fingerprint", result["error"])
        self.assertEqual(self.opened.connection().execute("SELECT COUNT(*) FROM acceptances").fetchone()[0], before_race)

    def test_acceptance_route_refuses_without_session_origin_or_csrf(self):
        status, _ = self.request("/api/acceptance?kind=language&id=prof-coder")
        self.assertEqual(status, 401)
        body = {"action": "withdraw", "kind": "language", "id": "prof-coder",
                "acceptance_id": "acc-1"}
        status, _ = self.request("/api/acceptance", method="POST", body=body)
        self.assertEqual(status, 401)
        status, _ = self.request("/api/acceptance", method="POST", token=self.token,
                                 origin=False, body=body)
        self.assertEqual(status, 403)
        status, _ = self.request("/api/acceptance", method="POST", token=self.token,
                                 csrf=False, body=body)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
