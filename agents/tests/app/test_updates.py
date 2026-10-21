"""The operator-requested update check through the authenticated manager HTTP route."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import admission, login, sessions, serving_state, store, updates, updater  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server  # noqa: E402


class FeedHandler(BaseHTTPRequestHandler):
    document = b""
    signature = b""
    requests = []
    delay = 0
    missing_signature = False

    def do_GET(self):
        type(self).requests.append(self.path)
        if type(self).delay:
            time.sleep(type(self).delay)
        if self.path == "/release-list.json":
            body = type(self).document
        elif self.path == "/release-list.json.sig":
            if type(self).missing_signature:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = type(self).signature
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, *_args):
        pass


class UpdatesHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix="matuto-update-http-key-")
        cls.key = Path(cls.keys.name) / "trusted"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(cls.key)],
                       check=True, capture_output=True)
        cls.public_key = Path(f"{cls.key}.pub").read_bytes().decode().strip()
        cls.home = Path(tempfile.mkdtemp(prefix="matuto-update-http-"))
        cls.environment = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(cls.home / "config"),
            "XDG_DATA_HOME": str(cls.home / "data"),
        })
        cls.environment.start()
        cls.feed = ThreadingHTTPServer(("127.0.0.1", 0), FeedHandler)
        cls.feed_thread = threading.Thread(target=cls.feed.serve_forever, daemon=True)
        cls.feed_thread.start()
        cls.feed_address = f"http://127.0.0.1:{cls.feed.server_address[1]}/"
        cls.server = build_server(port=0)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.address = f"http://127.0.0.1:{cls.server.server_address[1]}/api/updates"

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.feed):
            server.shutdown()
            server.server_close()
        cls.environment.stop()
        shutil.rmtree(cls.home, ignore_errors=True)
        cls.keys.cleanup()

    def setUp(self):
        self.home = self.__class__.home
        shutil.rmtree(self.home / "config", ignore_errors=True)
        shutil.rmtree(self.home / "data", ignore_errors=True)
        self.opened = store.open_store()
        self.addCleanup(self.opened.close)
        release_root = updater.releases_directory()
        (release_root / "2026.9.0").mkdir(parents=True, exist_ok=True)
        login.create_login("operator", "secret", "secret")
        self.token, _ = sessions.start_session("operator", "secret")

        FeedHandler.document = b""
        FeedHandler.signature = b""
        FeedHandler.requests = []
        FeedHandler.delay = 0
        FeedHandler.missing_signature = False
        self.feed = self.__class__.feed
        self.feed_address = self.__class__.feed_address
        self.server = self.__class__.server
        self.address = self.__class__.address

    def seed(self, installed="2026.9.0", *, address=None, number=2, offer=None,
             last_checked=None, stage="complete"):
        release_root = updater.releases_directory()
        (release_root / installed).mkdir(parents=True, exist_ok=True)
        active = updater.active_release()
        active.unlink(missing_ok=True)
        active.symlink_to(installed)
        details = {"trusted_key": self.public_key,
                   "release_address": self.feed_address if address is None else address}
        if offer is not None:
            details["update_offer"] = offer
        if last_checked is not None:
            details["last_update_check"] = last_checked
        store.write_update_state(store.store_path(), installed, stage,
                                 list_number=number, details=details)

    def offer(self):
        return {"version": "2026.10.0", "notes": "A verified release note.", "size": 123,
                "prerequisites": ["openssh-client"], "changesDatabase": True,
                "withdrawn": False}

    def install_gates(self, state=admission.STOPPED, works=()):
        state_mock = mock.patch.object(serving_state, "read", return_value=serving_state.State(state))
        reader = mock.Mock()
        reader.works.return_value = list(works)
        reader.live_works.return_value = list(works)
        reader.close.return_value = None
        runner_mock = mock.patch.object(updates.runner, "Runner", return_value=reader)
        prereqs_mock = mock.patch.object(updater, "missing_prerequisites", return_value=[])
        return state_mock, runner_mock, prereqs_mock

    def fake_systemd_run(self):
        bindir = self.home / "fake-systemd-bin"
        bindir.mkdir(exist_ok=True)
        (bindir / "systemd-run").write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['FAKE_SYSTEMD_RUN_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
            "message = os.environ.get('FAKE_SYSTEMD_RUN_MESSAGE', '')\n"
            "print(message) if message else None\n"
            "sys.exit(int(os.environ.get('FAKE_SYSTEMD_RUN_STATUS', '0')))\n")
        os.chmod(bindir / "systemd-run", 0o700)
        log = self.home / "systemd-run-args.json"
        environment = mock.patch.dict(os.environ, {
            "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
            "FAKE_SYSTEMD_RUN_LOG": str(log),
        })
        return log, environment

    def sign_list(self, number, records):
        return self.sign_document(json.dumps({"number": number, "releases": records}).encode())

    def sign_document(self, document):
        with tempfile.TemporaryDirectory(prefix="matuto-update-list-") as directory:
            path = Path(directory) / "release-list.json"
            path.write_bytes(document)
            subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(self.key),
                            "-n", "matuto-release", str(path)],
                           check=True, capture_output=True)
            signature = Path(f"{path}.sig").read_bytes()
            FeedHandler.document = document
            FeedHandler.signature = signature
            return document, signature

    def entry(self, version="2026.10.0", *, withdrawn=False, changes_database=True):
        return {"version": version, "commit": "a" * 40, "size": 123,
                "sha256": hashlib.sha256(b"x" * 123).hexdigest(),
                "notes": "A verified release note.", "prerequisites": ["openssh-client"],
                "changes_database": changes_database, "withdrawn": withdrawn}

    def call(self, method="GET", body=None, *, token=True, csrf=None, origin=True,
             include_headers=False):
        headers = {"Host": ALLOWED_HOST}
        if token:
            headers["Cookie"] = f"{SESSION_COOKIE}={self.token}"
        if method == "POST":
            headers["Content-Type"] = "application/json"
            headers["Origin"] = MANAGER_ORIGIN if origin else "http://elsewhere.test"
            headers["X-CSRF-Token"] = csrf if csrf is not None else self.server.RequestHandlerClass.csrf
        request = urllib.request.Request(self.address, data=(json.dumps(body or {}).encode()
                                     if method == "POST" else None), headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            body = error.read()
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {"error": body.decode("utf-8", "replace")[:200]}
            return ((error.code, payload, error.headers) if include_headers
                    else (error.code, payload))
        with response:
            result = (response.status, json.loads(response.read()))
            return (*result, response.headers) if include_headers else result

    def test_get_reads_saved_state_but_never_checks_or_fetches_the_feed(self):
        self.seed(last_checked="2026-10-12T12:00:00Z")
        status, result = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(result["installedVersion"], "2026.9.0")
        self.assertEqual(result["lastChecked"], "2026-10-12T12:00:00Z")
        self.assertEqual(FeedHandler.requests, [])

    def test_missing_feed_reports_no_public_releases_without_contacting_a_host(self):
        self.seed(address="")
        status, result = self.call("POST", {"action": "check"})
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "no-feed")
        self.assertIsNone(result["offer"])
        self.assertEqual(FeedHandler.requests, [])

    def test_explicit_check_offers_verified_facts_and_fetches_no_release_file(self):
        self.seed()
        self.sign_list(3, [self.entry()])
        status, result = self.call("POST", {"action": "check"})
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "offer")
        self.assertEqual(result["offer"], {
            "version": "2026.10.0", "notes": "A verified release note.", "size": 123,
            "prerequisites": ["openssh-client"], "changesDatabase": True, "withdrawn": False,
        })
        self.assertEqual(result["installedVersion"], "2026.9.0")
        self.assertTrue(result["lastChecked"].endswith("Z"))
        self.assertEqual(FeedHandler.requests, ["/release-list.json", "/release-list.json.sig"])
        state = store.read_update_state(store.store_path())
        persisted = json.loads(state["phase"])
        self.assertEqual(persisted["list_number"], 3)
        self.assertEqual(persisted["update_offer"]["version"], "2026.10.0")
        self.assertEqual(persisted["stage"], "complete")

    def test_unreachable_or_invalid_signature_keeps_last_success_and_offers_none(self):
        self.seed(last_checked="2026-10-12T12:00:00Z",
                  offer={"version": "2026.10.0", "notes": "Old offer"})
        self.sign_list(3, [self.entry()])
        FeedHandler.signature = b"not a valid signature"
        status, result = self.call("POST", {"action": "check"})
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["offer"])
        self.assertEqual(result["lastChecked"], "2026-10-12T12:00:00Z")
        state = json.loads(store.read_update_state(store.store_path())["phase"])
        self.assertEqual(state["update_offer"]["version"], "2026.10.0")
        self.assertEqual(state["last_update_check"], "2026-10-12T12:00:00Z")
        status, reopened = self.call()
        self.assertIsNone(reopened["offer"])
        self.assertEqual(reopened["state"], "failed")
        self.assertIn("signature", reopened["reason"])

    def test_offline_timeout_and_malformed_signed_list_fail_without_clearing_last_success(self):
        self.seed(address="http://127.0.0.1:1/", last_checked="2026-10-12T12:00:00Z")
        status, offline = self.call("POST", {"action": "check"})
        self.assertEqual(offline["state"], "failed")
        self.assertIn("cannot", offline["reason"])
        self.assertEqual(offline["lastChecked"], "2026-10-12T12:00:00Z")

        self.seed(last_checked="2026-10-12T12:00:00Z")
        self.sign_list(3, [self.entry()])
        FeedHandler.delay = 0.3
        with mock.patch.object(updater, "FETCH_TIMEOUT", 0.1):
            status, slow = self.call("POST", {"action": "check"})
        self.assertEqual(slow["state"], "failed")
        self.assertIn("time limit", slow["reason"])
        self.assertEqual(slow["lastChecked"], "2026-10-12T12:00:00Z")
        FeedHandler.delay = 0

        self.sign_document(b'{"number":3,"releases":[],"unknown":true}')
        status, malformed = self.call("POST", {"action": "check"})
        self.assertEqual(malformed["state"], "failed")
        self.assertIn("unknown field", malformed["reason"])
        self.assertEqual(malformed["lastChecked"], "2026-10-12T12:00:00Z")

    def test_unpublished_feed_404_is_no_public_releases_but_missing_signature_is_failure(self):
        self.seed(address=self.feed_address + "not-published/")
        status, result = self.call("POST", {"action": "check"})
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "no-feed")
        self.assertIn("no public releases", result["reason"])

        self.seed()
        self.sign_list(3, [self.entry()])
        FeedHandler.missing_signature = True
        status, result = self.call("POST", {"action": "check"})
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "failed")
        self.assertIn("HTTP 404", result["reason"])
        self.assertIsNone(result["offer"])

    def test_stale_list_brings_no_offer_and_does_not_advance_success_time(self):
        self.seed(number=4)
        self.sign_list(3, [self.entry()])
        status, result = self.call("POST", {"action": "check"})
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "stale")
        self.assertIsNone(result["lastChecked"])
        self.assertIsNone(result["offer"])
        self.assertEqual(json.loads(store.read_update_state(store.store_path())["phase"])["list_number"], 4)

    def test_withdrawn_previous_offer_and_withdrawn_installed_release_are_never_offered(self):
        self.seed(offer={"version": "2026.10.0", "notes": "Old offer"})
        self.sign_list(3, [self.entry(withdrawn=True)])
        status, result = self.call("POST", {"action": "check"})
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "withdrawn")
        self.assertIsNone(result["offer"])
        self.assertEqual(result["withdrawn"]["version"], "2026.10.0")
        self.assertFalse(result["withdrawn"]["installed"])
        status, reopened = self.call()
        self.assertEqual(reopened["state"], "success")
        self.assertIsNone(reopened["offer"])
        self.assertEqual(reopened["withdrawn"]["version"], "2026.10.0")
        persisted = json.loads(store.read_update_state(store.store_path())["phase"])
        self.assertIsNone(persisted["update_offer"])
        self.assertEqual(FeedHandler.requests, ["/release-list.json", "/release-list.json.sig"])
        status, repeated = self.call("POST", {"action": "check"})
        self.assertEqual(repeated["withdrawn"]["version"], "2026.10.0")
        self.sign_list(2, [self.entry(withdrawn=False)])
        status, stale = self.call("POST", {"action": "check"})
        self.assertEqual(stale["state"], "stale")
        self.assertIsNone(stale["offer"])
        self.assertEqual(stale["withdrawn"]["version"], "2026.10.0")
        status, reopened = self.call()
        self.assertIsNone(reopened["offer"])
        self.assertEqual(reopened["state"], "stale")
        self.assertEqual(reopened["withdrawn"]["version"], "2026.10.0")

        self.seed("2026.10.0", number=3)
        self.sign_list(4, [self.entry(withdrawn=True)])
        status, result = self.call("POST", {"action": "check"})
        self.assertEqual(result["state"], "withdrawn")
        self.assertTrue(result["withdrawn"]["installed"])
        self.assertIsNone(result["offer"])

    def test_installed_identity_and_last_check_survive_a_failed_update_transition(self):
        self.seed("2026.10.0", number=3,
                  offer={"version": "2026.10.0", "notes": "Previously verified"},
                  last_checked="2026-10-12T12:00:00Z", stage="failed")
        updater.active_release().unlink()
        updater.active_release().symlink_to("2026.9.0")
        updater._record_state(self.opened.connection(), "2026.10.0", "failed",
                              reason="activation stopped before pointer change")

        status, reopened = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(reopened["installedVersion"], "2026.9.0")
        self.assertEqual(reopened["lastChecked"], "2026-10-12T12:00:00Z")
        self.assertEqual(reopened["offer"]["version"], "2026.10.0")

        self.sign_list(4, [self.entry()])
        status, result = self.call("POST", {"action": "check"})
        self.assertEqual(status, 200)
        self.assertEqual(result["installedVersion"], "2026.9.0")
        self.assertEqual(result["offer"]["version"], "2026.10.0")
        persisted = store.read_update_state(store.store_path())
        self.assertEqual(persisted["release_version"], "2026.10.0")
        phase = json.loads(persisted["phase"])
        self.assertEqual(phase["stage"], "failed")
        self.assertEqual(phase["last_update_check"], result["lastChecked"])

    def test_concurrent_older_check_cannot_replace_newer_withdrawal(self):
        self.seed(number=3, offer={"version": "2026.10.0", "notes": "Existing offer"},
                  last_checked="2026-10-12T12:00:00Z")
        old_feed = self.sign_list(4, [self.entry()])
        new_feed = self.sign_list(5, [self.entry(withdrawn=True)])
        feeds = iter((old_feed, new_feed))
        feed_lock = threading.Lock()
        feed_context = threading.local()
        old_waiting = threading.Event()
        release_old = threading.Event()
        new_done = threading.Event()
        outcomes = {}
        failures = []
        original_record = store.record_update_check

        def fetch(url, _limit):
            if url.endswith("/release-list.json"):
                with feed_lock:
                    feed_context.current = next(feeds)
                return feed_context.current[0]
            return feed_context.current[1]

        def delayed_record(path, details, *, list_number=None):
            if list_number == 4:
                old_waiting.set()
                if not release_old.wait(5):
                    raise AssertionError("the newer check did not complete")
            return original_record(path, details, list_number=list_number)

        def post(label):
            try:
                outcomes[label] = self.call("POST", {"action": "check"})
            except BaseException as error:
                failures.append(error)
            finally:
                if label == "new":
                    new_done.set()

        with mock.patch.object(updater, "_http_bytes", side_effect=fetch):
            with mock.patch.object(store, "record_update_check", side_effect=delayed_record):
                old_thread = threading.Thread(target=post, args=("old",))
                old_thread.start()
                self.assertTrue(old_waiting.wait(5), "older check did not reach its delayed commit")
                new_thread = threading.Thread(target=post, args=("new",))
                new_thread.start()
                self.assertTrue(new_done.wait(5), "newer check did not complete")
                release_old.set()
                old_thread.join(5)
                new_thread.join(5)

        self.assertEqual(failures, [])
        self.assertEqual(outcomes["new"][1]["state"], "withdrawn")
        self.assertEqual(outcomes["old"][1]["state"], "stale")
        state = json.loads(store.read_update_state(store.store_path())["phase"])
        self.assertEqual(state["list_number"], 5)
        self.assertIsNone(state["update_offer"])
        self.assertEqual(state["withdrawn_offer"]["version"], "2026.10.0")
        status, reopened = self.call()
        self.assertEqual(reopened["state"], "success")
        self.assertIsNone(reopened["offer"])
        self.assertEqual(reopened["withdrawn"]["version"], "2026.10.0")

    def test_recovery_blocker_read_uses_live_work_without_settling_the_store(self):
        state = serving_state.State(admission.STOPPED)
        reader = mock.Mock()
        reader.works.side_effect = AssertionError("recovery blocker read must not settle work")
        reader.live_works.return_value = []
        with (mock.patch.object(serving_state, "read", return_value=state),
              mock.patch.object(updates.runner, "Runner", return_value=reader),
              mock.patch.object(updater, "missing_prerequisites", return_value=[])):
            updates.install_status(settle_work=False)
        reader.live_works.assert_called_once()
        reader.works.assert_not_called()

    def test_unknown_serving_state_still_names_a_known_active_unit_to_stop(self):
        self.seed()
        unit = serving_state.ROUTER_UNIT
        state = serving_state.State(admission.SERVING_UNKNOWN, reason="voice units could not be listed",
                                    units=((unit, "active"),))
        reader = mock.Mock()
        reader.live_works.return_value = []
        reader.close.return_value = None
        with (mock.patch.object(serving_state, "read", return_value=state),
              mock.patch.object(updates.runner, "Runner", return_value=reader),
              mock.patch.object(updater, "missing_prerequisites", return_value=[])):
            result = updates.install_status(settle_work=False)
        self.assertTrue(any(item.get("command") == f"systemctl --user stop {unit}"
                            for item in result["blockers"]))
        self.assertTrue(any("Serving state is unknown" in item["text"]
                            for item in result["blockers"]))

    def test_install_preview_refuses_every_live_blocker_and_reports_handling_links(self):
        self.seed(number=3, offer=self.offer(), last_checked="2026-10-13T12:00:00Z")
        work = mock.Mock(kind="download", purpose="weights", work_id="job-1", outcome="")
        state_patch, runner_patch, _prereqs_patch = self.install_gates(
            admission.AGENTIC, [work])
        prereqs_patch = mock.patch.object(updater, "missing_prerequisites",
                                          return_value=["openssh-client"])
        with state_patch, runner_patch, prereqs_patch:
            status, result = self.call("POST", {"action": "preview", "version": "2026.10.0",
                                                  "listNumber": 3})
        self.assertEqual(status, 409)
        self.assertEqual(len(result["blockers"]), 3)
        self.assertTrue(any("serving" in item["text"].lower()
                            and item["href"] == "#/overview/serving" for item in result["blockers"]))
        self.assertTrue(any("download" in item["text"].lower()
                            and item["href"] == "#/runs?work=job-1" for item in result["blockers"]))
        self.assertTrue(any("openssh-client" in item["text"] for item in result["blockers"]))

    def test_install_rechecks_the_signed_list_and_spawns_only_the_reviewed_version(self):
        self.seed(number=3, offer=self.offer(), last_checked="2026-10-13T12:00:00Z")
        self.sign_list(3, [self.entry()])
        state_patch, runner_patch, prereqs_patch = self.install_gates()
        unit = "matuto-update-" + "a" * 32 + ".service"
        unit_name = mock.patch.object(updater, "app_install_unit_name", return_value=unit)
        script = mock.patch.object(updater, "independent_updater_script",
                                   return_value=Path("/fixture/updater.py"))
        log, environment = self.fake_systemd_run()
        self.opened.close()
        with state_patch, runner_patch, prereqs_patch, unit_name, script, environment:
            status, result = self.call("POST", {"action": "install", "version": "2026.10.0",
                                                  "listNumber": 3})
        self.assertEqual(status, 200, result)
        self.assertEqual(result, {"started": True, "version": "2026.10.0"})
        self.assertEqual(json.loads(log.read_text()), [
            "--user", "--collect", "--unit", unit, "--", sys.executable, "-B",
            "/fixture/updater.py", "update", "--version", "2026.10.0", "--list-number", "3",
            "--install-unit", unit])
        with mock.patch.object(updater, "app_install_unit_status", return_value="active"):
            status, running = self.call()
            self.assertEqual(status, 200)
            self.assertEqual(running["installation"]["stage"], "starting")
            status, refusal = self.call("POST", {"action": "check"})
            self.assertEqual(status, 409)
            self.assertIn("Matuto update to 2026.10.0 is starting", refusal["error"])
        self.assertEqual(FeedHandler.requests, ["/release-list.json", "/release-list.json.sig"])
        phase = json.loads(store.read_update_state(store.store_path())["phase"])
        self.assertEqual(phase["app_install_pending"]["unit_name"],
                         "matuto-update-" + "a" * 32 + ".service")

    def test_install_reservation_rejects_an_offer_changed_during_final_recheck(self):
        self.seed(number=3, offer=self.offer(), last_checked="2026-10-13T12:00:00Z")
        self.sign_list(3, [self.entry()])
        state_patch, runner_patch, prereqs_patch = self.install_gates()
        status = updates.install_status

        def changed_offer(*args, **kwargs):
            result = status(*args, **kwargs)
            store.record_update_check(
                store.store_path(), {"update_offer": {**self.offer(), "version": "2026.11.0"}},
                list_number=4)
            return result

        self.opened.close()
        with state_patch, runner_patch, prereqs_patch, \
                mock.patch.object(updates, "install_status", side_effect=changed_offer), \
                mock.patch.object(updater, "start_app_install") as start:
            with self.assertRaisesRegex(updater.UpdateError, "offer changed"):
                updates.install("2026.10.0", 3)
        start.assert_not_called()

    def test_interrupted_handoff_keeps_store_blocked_and_session_readable(self):
        self.seed(stage="complete")
        store.write_update_state(
            store.store_path(), "2026.9.0", "complete",
            details={"app_install_pending": {
                "version": "2026.10.0", "list_number": 3,
                "unit_name": "matuto-update-" + "a" * 32 + ".service"}})
        with mock.patch.object(updater, "app_install_unit_status", return_value="inactive"):
            status = updates.display()
        self.assertEqual(status["installation"]["stage"], "interrupted")
        self.assertTrue(sessions.session_is_valid(self.token))
        with self.assertRaisesRegex(store.StoreError, "Matuto update to 2026.10.0 has not recorded an outcome"):
            store.open_store()

    def test_update_gate_refusal_after_another_tab_confirms_includes_blocker_links(self):
        self.seed(number=3, offer=self.offer(), last_checked="2026-10-13T12:00:00Z")
        unit = "matuto-update-" + "a" * 32 + ".service"
        store.write_update_state(
            store.store_path(), "2026.9.0", "complete",
            details={"app_install_pending": {"version": "2026.10.0", "list_number": 3,
                                               "unit_name": unit}})
        self.opened.close()
        with mock.patch.object(updater, "app_install_unit_status", return_value="active"):
            status, result = self.call("POST", {"action": "install", "version": "2026.10.0",
                                                  "listNumber": 3})
            self.assertEqual(status, 409)
            self.assertIn("Matuto update to 2026.10.0 is starting", result["error"])
            self.assertTrue(result["blockers"])
            self.assertTrue(all(item.get("href") and item.get("label")
                                for item in result["blockers"]))
        status, unauthorized = self.call(
            "POST", {"action": "install", "version": "2026.10.0", "listNumber": 3}, token=False)
        self.assertEqual(status, 401, unauthorized)

    def test_session_and_update_status_survive_an_exclusive_store_lock(self):
        self.seed()
        self.opened.close()
        exclusive = store.open_store(exclusive=True)
        try:
            status, result, headers = self.call(include_headers=True)
            self.assertEqual(status, 200, result)
            self.assertNotIn("Set-Cookie", headers)
            self.assertTrue(sessions.session_is_valid(self.token))
        finally:
            exclusive.close()

    def test_unreadable_fallback_does_not_expire_the_browser_session(self):
        self.seed()
        self.opened.close()
        exclusive = store.open_store(exclusive=True)
        try:
            with mock.patch.object(sessions.store, "read_session_at",
                                   side_effect=store.StoreError("unreadable")):
                status, result, headers = self.call(include_headers=True)
            self.assertEqual(status, 503, result)
            self.assertNotIn("Set-Cookie", headers)
        finally:
            exclusive.close()

    def test_session_read_failure_does_not_expire_the_browser_session(self):
        self.seed()
        self.opened.close()
        with mock.patch.object(sessions.store, "read_session_at",
                               side_effect=store.StoreError("unreadable")):
            status, result, headers = self.call(include_headers=True)
        self.assertEqual(status, 503, result)
        self.assertNotIn("Set-Cookie", headers)

    def test_check_reply_includes_fresh_blockers_and_preserves_the_install_outcome(self):
        self.seed(number=3, offer=self.offer(), last_checked="2026-10-13T12:00:00Z")
        store.write_update_state(store.store_path(), "2026.9.0", "complete",
                                 details={"install_result": {"version": "2026.10.0",
                                                              "stage": "failed",
                                                              "reason": "disk full"}})
        self.sign_list(4, [self.entry()])
        state_patch, runner_patch, prereqs_patch = self.install_gates(admission.AGENTIC)
        with state_patch, runner_patch, prereqs_patch:
            status, result = self.call("POST", {"action": "check"})
        self.assertEqual(status, 200)
        self.assertTrue(any("serving" in item["text"].lower() for item in result["blockers"]))
        self.assertEqual(result["installation"]["stage"], "failed")
        self.assertEqual(result["installation"]["reason"], "disk full")

    def test_start_updater_uses_a_transient_user_service_and_exact_pinned_arguments(self):
        script = mock.patch.object(updater, "independent_updater_script",
                                   return_value=Path("/fixture/updater.py"))
        unit = "matuto-update-" + "a" * 32 + ".service"
        log, environment = self.fake_systemd_run()
        with script, environment:
            result = updater.start_app_install("2026.10.0", 3, unit)
        command = json.loads(log.read_text())
        self.assertEqual(result, unit)
        self.assertEqual(command, ["--user", "--collect", "--unit", unit, "--",
                                   sys.executable, "-B", "/fixture/updater.py",
                                   "update", "--version", "2026.10.0",
                                   "--list-number", "3", "--install-unit", unit])
        self.assertTrue(log.exists(), "the fake systemd-run service launcher received the request")

    def test_transient_launcher_distinguishes_refusal_from_uncertain_timeout(self):
        unit = "matuto-update-" + "a" * 32 + ".service"
        script = mock.patch.object(updater, "independent_updater_script",
                                   return_value=Path("/fixture/updater.py"))
        log, environment = self.fake_systemd_run()
        refusal = mock.patch.dict(os.environ, {"FAKE_SYSTEMD_RUN_STATUS": "1",
                                                "FAKE_SYSTEMD_RUN_MESSAGE": "job failed"})
        with script, environment, refusal, self.assertRaisesRegex(updater.UpdateError, "job failed") as error:
            updater.start_app_install("2026.10.0", 3, unit)
        self.assertNotIsInstance(error.exception, updater.UpdaterLaunchUncertain)
        self.assertEqual(json.loads(log.read_text())[3], unit)
        timed_out = mock.patch.object(
            updater.subprocess, "run",
            side_effect=subprocess.TimeoutExpired("systemd-run", 30))
        with script, timed_out, self.assertRaises(updater.UpdaterLaunchUncertain):
            updater.start_app_install("2026.10.0", 3, unit)

    def test_transient_unit_status_distinguishes_running_from_stopped(self):
        unit = "matuto-update-" + "a" * 32 + ".service"
        command = mock.patch.object(
            updater, "_systemctl",
            return_value=subprocess.CompletedProcess([], 0, "loaded\nactive\n", ""))
        with command as systemctl:
            self.assertEqual(updater.app_install_unit_status({"unit_name": unit}), "active")
        self.assertEqual(systemctl.call_args.args[0][-1], unit)
        stopped = mock.patch.object(
            updater, "_systemctl",
            return_value=subprocess.CompletedProcess([], 0, "not-found\ninactive\n", ""))
        with stopped:
            self.assertEqual(updater.app_install_unit_status({"unit_name": unit}), "inactive")
        self.assertEqual(updater.app_install_unit_status({"unit_name": "bad;unit"}), "unknown")

    def test_failed_transient_launch_clears_the_reservation(self):
        self.seed(number=3, offer=self.offer(), last_checked="2026-10-13T12:00:00Z")
        self.sign_list(3, [self.entry()])
        state_patch, runner_patch, prereqs_patch = self.install_gates()
        unit = "matuto-update-" + "a" * 32 + ".service"
        log, environment = self.fake_systemd_run()
        refusal = mock.patch.dict(os.environ, {"FAKE_SYSTEMD_RUN_STATUS": "1",
                                                "FAKE_SYSTEMD_RUN_MESSAGE": "unit refused"})
        script = mock.patch.object(updater, "independent_updater_script",
                                   return_value=Path("/fixture/updater.py"))
        unit_name = mock.patch.object(updater, "app_install_unit_name", return_value=unit)
        self.opened.close()
        with state_patch, runner_patch, prereqs_patch, environment, refusal, script, unit_name:
            status, result = self.call("POST", {"action": "install", "version": "2026.10.0",
                                                  "listNumber": 3})
        self.assertEqual(status, 409)
        self.assertIn("unit refused", result["error"])
        self.assertEqual(json.loads(log.read_text())[3], unit)
        phase = json.loads(store.read_update_state(store.store_path())["phase"])
        self.assertIsNone(phase.get("app_install_pending"))
        self.assertEqual(phase["install_result"]["stage"], "failed")
        reopened = store.open_store()
        reopened.close()

    def test_uncertain_transient_launch_keeps_the_reservation_and_shows_interrupted(self):
        self.seed(number=3, offer=self.offer(), last_checked="2026-10-13T12:00:00Z")
        self.sign_list(3, [self.entry()])
        state_patch, runner_patch, prereqs_patch = self.install_gates()
        failed = mock.patch.object(updater, "start_app_install",
                                   side_effect=updater.UpdaterLaunchUncertain("unknown"))
        unit_status = mock.patch.object(updater, "app_install_unit_status", return_value="inactive")
        self.opened.close()
        with state_patch, runner_patch, prereqs_patch, failed, unit_status:
            status, result = self.call("POST", {"action": "install", "version": "2026.10.0",
                                                  "listNumber": 3})
            self.assertEqual(status, 409)
            self.assertIn("unknown", result["error"])
        phase = json.loads(store.read_update_state(store.store_path())["phase"])
        self.assertIsNotNone(phase.get("app_install_pending"))
        self.assertEqual(updates.display()["installation"]["stage"], "interrupted")

    def test_changed_release_list_refuses_confirmation_without_starting_updater(self):
        self.seed(number=3, offer=self.offer(), last_checked="2026-10-13T12:00:00Z")
        self.sign_list(4, [self.entry()])
        state_patch, runner_patch, prereqs_patch = self.install_gates()
        process = mock.patch.object(updater, "start_app_install")
        self.opened.close()
        with state_patch, runner_patch, prereqs_patch, process as start:
            status, result = self.call("POST", {"action": "install", "version": "2026.10.0",
                                                  "listNumber": 3})
        self.assertEqual(status, 409)
        self.assertIn("offer changed", result["error"].lower())
        start.assert_not_called()

    def test_mutation_requires_session_origin_and_csrf_and_only_accepts_check(self):
        self.seed()
        self.assertEqual(self.call("POST", {"action": "check"}, token=False)[0], 401)
        self.assertEqual(self.call("POST", {"action": "check"}, origin=False)[0], 403)
        self.assertEqual(self.call("POST", {"action": "check"}, csrf="wrong")[0], 403)
        self.assertEqual(self.call("POST", {"action": "anything"})[0], 409)
        self.assertEqual(FeedHandler.requests, [])


if __name__ == "__main__":
    unittest.main()
