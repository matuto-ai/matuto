"""The Model files wizard's authenticated HTTP API, using a local source fixture."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import acquisition, admission, download, login, origins, sessions, storage, store  # noqa: E402
from matuto.routes import acquisition as acquisition_route  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server  # noqa: E402

COMMIT = "0123456789abcdef0123456789abcdef01234567"
FILES = [
    ("model-00001-of-00002.gguf", b"first shard", True),
    ("model-00002-of-00002.gguf", b"second shard", True),
    ("tokenizer.json", b"tokenizer", False),
]


class Source(BaseHTTPRequestHandler):
    def do_GET(self):
        revision = self.path.rsplit("/", 1)[-1].split("?", 1)[0]
        result = {"id": "owner/model", "sha": COMMIT, "private": False, "gated": False,
                  "siblings": [{"rfilename": name, "size": len(body),
                                "lfs": {"sha256": hashlib.sha256(body).hexdigest()} if published else None}
                               for name, body, published in FILES]}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        body = json.dumps(result).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class FakeRunner:
    held = False
    request = None
    starts = 0
    admitted = False
    serving = admission.STOPPED
    work_entries = []

    def __init__(self, _root, *, opened_store, **_kwargs):
        self.store = opened_store
        self.logs = Path(tempfile.gettempdir()) / "matuto-wizard-test-logs"

    def serving_state(self):
        return type(self).serving

    def works(self):
        if self.work_entries:
            return self.work_entries
        if not self.held:
            return []
        return [SimpleNamespace(outcome="", kind="build", purpose="fixture build",
                                work_id="build-0123456789abcdef", state="running")]

    def progress(self, work):
        return work.progress

    def start(self, request):
        type(self).starts += 1
        type(self).request = request
        if self.held:
            return SimpleNamespace(decision=admission.REFUSE,
                                   reason="The admission slot is held by the build for fixture build. Stop it first; nothing is queued.",
                                   work=None)
        type(self).admitted = True
        return SimpleNamespace(decision=admission.ADMIT, reason="",
                               work=SimpleNamespace(work_id="download-0123456789abcdef"))


class AcquisitionWizardHTTPTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.opened = store.open_store()
        self.addCleanup(self.opened.close)
        self.models = self.home / "models"
        self.models.mkdir(mode=0o700)
        self.source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        self.source.daemon_threads = True
        self.thread = threading.Thread(target=self.source.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_source)
        self.base = f"http://127.0.0.1:{self.source.server_port}"
        login.create_login("operator", "secret", "secret")
        self.token, _expiry = sessions.start_session("operator", "secret")
        self.server = build_server(port=0)
        self.server_thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.server_thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf
        self.runner_patch = mock.patch("matuto.runner.Runner", FakeRunner)
        self.runner_patch.start()
        self.addCleanup(self.runner_patch.stop)
        FakeRunner.held = False
        FakeRunner.request = None
        FakeRunner.starts = 0
        FakeRunner.admitted = False
        FakeRunner.serving = admission.STOPPED
        FakeRunner.work_entries = []
        self.source_patch = mock.patch.object(acquisition, "API_BASE", self.base)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)

    def close_source(self):
        self.source.shutdown()
        self.source.server_close()
        self.thread.join(timeout=2)

    def request(self, method="GET", payload=None, *, cookie=True, csrf=True, origin=True):
        headers = {"Host": ALLOWED_HOST}
        if cookie:
            headers["Cookie"] = f"{SESSION_COOKIE}={self.token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if method != "GET":
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
            if origin:
                headers["Origin"] = MANAGER_ORIGIN
        request = urllib.request.Request(self.address + "/api/acquisition", data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def configured(self):
        storage.save_model_storage(self.opened.connection(), self.models)
        origins.add(self.opened.connection(), "organisation", "owner")

    def test_prepare_names_both_missing_prerequisites_without_guessing(self):
        status, answer = self.request()
        self.assertEqual(status, 200)
        self.assertFalse(answer["ready"])
        self.assertEqual([item["label"] for item in answer["blockers"]],
                         ["Settings › Storage & artifacts", "Settings › Download origins"])
        self.assertEqual([item["href"] for item in answer["blockers"]],
                         ["#/settings/storage-artifacts", "#/settings/origins"])
        self.assertTrue(all("path" not in item for item in answer["blockers"]))

    def test_list_and_resolve_keep_source_order_pin_commit_and_expand_selected_shards(self):
        self.configured()
        status, listed = self.request("POST", {"action": "list", "repository": "owner/model",
                                                "revision": "main"})
        self.assertEqual(status, 200, listed)
        self.assertEqual([item["name"] for item in listed["files"]], [item[0] for item in FILES])
        self.assertEqual(listed["revision"], COMMIT)

        status, reviewed = self.request("POST", {"action": "resolve", "repository": "owner/model",
                                                  "revision": "main", "files": [FILES[0][0]]})
        self.assertEqual(status, 200, reviewed)
        self.assertEqual(reviewed["origin"], "owner")
        self.assertEqual(reviewed["revision"], COMMIT)
        self.assertEqual([item["name"] for item in reviewed["files"]],
                         [FILES[0][0], FILES[1][0]])
        self.assertEqual(reviewed["total_size"], len(FILES[0][1]) + len(FILES[1][1]))
        self.assertEqual(reviewed["destination"], str(self.models))

    def test_acquire_requires_no_checksum_consent_and_starts_the_reviewed_pin(self):
        self.configured()
        payload = {"action": "acquire", "repository": "owner/model", "revision": COMMIT,
                   "files": [FILES[0][0], FILES[2][0]], "consent": False,
                   "destination": str(self.models)}
        status, refusal = self.request("POST", payload)
        self.assertEqual(status, 409)
        self.assertIn("consent", refusal["error"].lower())
        self.assertIsNone(FakeRunner.request)
        self.assertEqual(FakeRunner.starts, 0)

        payload["consent"] = True
        FakeRunner.serving = admission.AGENTIC
        status, answer = self.request("POST", payload)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["job_id"], "download-0123456789abcdef")
        self.assertIn("may slow it", answer["warning"])
        pinned = json.loads(FakeRunner.request.pinned_request)
        self.assertEqual(pinned["revision"], COMMIT)
        self.assertEqual([item["name"] for item in pinned["files"]],
                         [FILES[0][0], FILES[1][0], FILES[2][0]])
        self.assertTrue(pinned["consent"])
        self.assertEqual(pinned["destination"], str(self.models))

    def test_a_held_slot_refuses_acquire_without_queueing(self):
        self.configured()
        FakeRunner.held = True
        status, answer = self.request("POST", {"action": "acquire", "repository": "owner/model",
            "revision": COMMIT, "files": [FILES[0][0]], "consent": False,
            "destination": str(self.models)})
        self.assertEqual(status, 409)
        self.assertIn("build", answer["error"])
        self.assertIn("nothing is queued", answer["error"])
        self.assertIsNotNone(FakeRunner.request)
        self.assertEqual(FakeRunner.starts, 1)
        self.assertFalse(FakeRunner.admitted)

    def test_acquire_refuses_if_model_storage_changed_after_review(self):
        self.configured()
        status, reviewed = self.request("POST", {"action": "resolve", "repository": "owner/model",
            "revision": "main", "files": [FILES[0][0]]})
        self.assertEqual(status, 200, reviewed)
        changed = self.home / "new-models"
        changed.mkdir(mode=0o700)
        connection = self.opened.connection()
        location = connection.execute("SELECT location_id FROM model_storage").fetchone()["location_id"]
        storage.change_location(connection, location, changed, serving_state=admission.STOPPED)

        status, answer = self.request("POST", {"action": "acquire", "repository": reviewed["repository"],
            "revision": reviewed["revision"], "files": [item["name"] for item in reviewed["files"]],
            "consent": False, "destination": reviewed["destination"]})
        self.assertEqual(status, 409)
        self.assertIn("changed since Review", answer["error"])
        self.assertEqual(FakeRunner.starts, 0)

    def test_download_status_and_recovery_actions_use_runner_and_safe_discard_preview(self):
        job_id = "download-0123456789abcdef"
        work = SimpleNamespace(work_id=job_id, kind="download",
            purpose=json.dumps({"repository": "owner/model", "revision": COMMIT}),
            outcome="", state="running", started_at="then", ended_at="",
            progress=SimpleNamespace(work_id=job_id, state="interrupted", phase="download",
                step="model.gguf: 4 of 8 bytes", elapsed=12, duration=12, log="curl exited", unknown=""))
        FakeRunner.work_entries = [work]
        self.opened.connection().execute(
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, phase, outcome, started_at, ended_at)"
            " VALUES (?, 'download', 'matuto-download-fixture', '{}', ?, NULL, 'then', NULL)",
            (job_id, json.dumps({"phase": "download", "file": "model.gguf", "transferred": 4, "total": 8})))
        status, answer = self.request()
        self.assertEqual(status, 200, answer)
        item = answer["downloads"][0]
        self.assertEqual((item["phase"], item["file"], item["transferred"], item["total"]),
                         ("download", "model.gguf", 4, 8))
        self.assertEqual(item["purpose"], "owner/model@" + COMMIT)

        work.progress = SimpleNamespace(work_id=job_id, state="running", phase="verification",
            step="model.gguf: 6 of 8 bytes", elapsed=13, duration=0, log="", unknown="")
        self.opened.connection().execute("UPDATE jobs SET phase = ? WHERE job_id = ?",
            (json.dumps({"phase": "verification", "file": "model.gguf", "transferred": 6, "total": 8}), job_id))
        status, verifying = self.request()
        item = verifying["downloads"][0]
        self.assertEqual(status, 200)
        self.assertEqual((item["phase"], item["file"], item["transferred"], item["total"]),
                         ("verification", "model.gguf", 6, 8))

        preview = download.DiscardPreview(job_id, (("model.gguf", 4),), 4)
        work.outcome, work.state = "interrupted", "interrupted"
        work.progress = SimpleNamespace(work_id=job_id, state="interrupted", phase="download",
            step="model.gguf: 4 of 8 bytes", elapsed=12, duration=12, log="curl exited", unknown="")
        self.opened.connection().execute("UPDATE jobs SET outcome = 'interrupted', ended_at = 'then' WHERE job_id = ?", (job_id,))
        with mock.patch.object(download, "preview_discard", return_value=preview):
            status, answer = self.request()
        item = answer["downloads"][0]
        self.assertEqual(status, 200, answer)
        self.assertEqual(item["phase"], "")
        self.assertEqual(item["partials"]["total_size"], 4)
        self.assertIn("curl exited", item["reason"])
        work.progress = SimpleNamespace(work_id=job_id, state="unknown", phase="", step="", elapsed=12,
                                        duration=0, log="", unknown="progress could not be read")
        with mock.patch.object(download, "preview_discard", return_value=preview):
            status, unknown = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(unknown["downloads"][0]["phase"], "")
        self.assertIsNone(unknown["downloads"][0]["transferred"])

        admitted = SimpleNamespace(decision=admission.ADMIT,
            work=SimpleNamespace(work_id="download-resumed"), reason="")
        with mock.patch.object(download, "resume", return_value=SimpleNamespace(start=admitted, warning="")) as resume:
            status, resumed = self.request("POST", {"action": "resume", "job_id": job_id})
        self.assertEqual(status, 200, resumed)
        self.assertEqual(resumed["job_id"], "download-resumed")
        resume.assert_called_once()

        with mock.patch.object(download, "preview_discard", return_value=preview):
            status, preview_data = self.request("POST", {"action": "discard-preview", "job_id": job_id})
        self.assertEqual(status, 200)
        self.assertEqual(preview_data["files"], [{"name": "model.gguf", "size": 4}])
        fresh = download.DiscardPreview(job_id, (("model.gguf", 5),), 5)
        with mock.patch.object(download, "confirm_discard", side_effect=download.DiscardChanged(fresh)):
            status, refusal = self.request("POST", {"action": "discard", "job_id": job_id,
                "preview": preview_data})
        self.assertEqual(status, 409)
        self.assertEqual(refusal["preview"]["total_size"], 5)
        self.assertIn("staging changed", refusal["error"])

        with mock.patch.object(download, "confirm_discard", side_effect=download.DownloadError("job state is unknown")), \
                mock.patch.object(download, "preview_discard", side_effect=download.DownloadError("state is unknown")):
            status, unavailable = self.request("POST", {"action": "discard", "job_id": job_id,
                "preview": preview_data})
        self.assertEqual(status, 409)
        self.assertEqual(unavailable["preview"]["files"], [])
        self.assertIn("No fresh safe preview is available", unavailable["preview"]["consequence"])
        self.assertIn("job state is unknown", unavailable["error"])

        status, _answer = self.request("POST", {"action": "resume", "job_id": job_id}, csrf=False)
        self.assertEqual(status, 403)

    def test_model_records_list_verify_and_keep_recorded_digests_on_mismatch(self):
        location = self.home / "artifacts"
        location.mkdir(mode=0o700)
        location_id = storage.add_artifact_location(self.opened.connection(), location)["location_id"]
        first = location / "model-00001-of-00002.gguf"
        second = location / "model-00002-of-00002.gguf"
        first.write_bytes(b"first shard")
        second.write_bytes(b"second shard")
        connection = self.opened.connection()
        connection.execute(
            "INSERT INTO acquisition_records (acquisition_id,name,source,checksum_status,"
            "location_id,verified_at,created_at) VALUES ('acq-model','Model','adoption',"
            "'locally-hashed',?,?, 'before')", (location_id, "before"))
        for path in (first, second):
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            connection.execute(
                "INSERT INTO acquisition_files VALUES ('acq-model',?,?,NULL,?,?)",
                (path.name, len(body), digest, "before"))
        status, listed = self.request()
        self.assertEqual(status, 200)
        record, = listed["records"]
        self.assertEqual((record["source"], record["revision"], record["label"], record["complete"]),
                         ("unknown", "unknown", "locally hashed; publisher unverified", True))
        self.assertEqual({item["file_name"] for item in record["files"]},
                         {first.name, second.name})
        status, intact = self.request("POST", {"action": "verify-record", "acquisition_id": "acq-model"})
        self.assertEqual(status, 200)
        self.assertTrue(intact["complete"])
        self.assertTrue(intact["verified_at"])
        prior = {row["file_name"]: row["computed_sha256"] for row in connection.execute(
            "SELECT file_name, computed_sha256 FROM acquisition_files WHERE acquisition_id='acq-model'")}
        second.write_bytes(b"changed shard")
        status, verified = self.request("POST", {"action": "verify-record", "acquisition_id": "acq-model"})
        self.assertEqual(status, 200)
        self.assertFalse(verified["complete"])
        self.assertIn(second.name, verified["reason"])
        after = connection.execute(
            "SELECT file_name, computed_sha256 FROM acquisition_files WHERE acquisition_id='acq-model'")
        self.assertEqual({row["file_name"]: row["computed_sha256"] for row in after}, prior)
        status, listed = self.request()
        self.assertFalse(listed["records"][0]["complete"])
        failed, = listed["records"]
        self.assertEqual(failed["verified_at"], intact["verified_at"])
        self.assertIn(second.name, failed["reason"])
        self.assertIn(second.name, verified["reason"])

    def test_record_profile_kind_ignores_display_names_and_non_model_sidecars(self):
        location = self.home / "kinds"
        location.mkdir(mode=0o700)
        location_id = storage.add_artifact_location(self.opened.connection(), location)["location_id"]
        records = [
            ("acq-language-kind", "Speechless language model", [
                ("model-00001-of-00002.gguf", b"language shard one"),
                ("model-00002-of-00002.gguf", b"language shard two"),
                ("speech-metadata.bin", b"language sidecar"),
            ]),
            ("acq-speech-kind", "Ordinary display label", [("voice-model.gguf", b"speech gguf")]),
            ("acq-unknown-kind", "Speechless label", [("weights.gguf", b"ambiguous gguf")]),
        ]
        connection = self.opened.connection()
        for acquisition_id, name, files in records:
            connection.execute(
                "INSERT INTO acquisition_records (acquisition_id,name,source,checksum_status,"
                "location_id,verified_at,created_at) VALUES (?,?,'adoption','locally-hashed',?,?,?)",
                (acquisition_id, name, location_id, "verified", "before"))
            for file_name, body in files:
                (location / file_name).write_bytes(body)
                connection.execute(
                    "INSERT INTO acquisition_files VALUES (?,?,?,NULL,?,?)",
                    (acquisition_id, file_name, len(body), hashlib.sha256(body).hexdigest(), "verified"))
        status, answer = self.request()
        self.assertEqual(status, 200)
        kinds = {record["acquisition_id"]: record["profile_kind"] for record in answer["records"]}
        self.assertEqual(kinds, {"acq-language-kind": "language", "acq-speech-kind": "speech",
                                 "acq-unknown-kind": "unknown"})

    def test_adoption_lists_configured_files_and_requires_consent_without_model_storage(self):
        location = self.home / "adopt-here"
        location.mkdir(mode=0o700)
        originals = []
        for name, content, _published in FILES[:2]:
            path = location / name
            path.write_bytes(content)
            originals.append((path, path.stat().st_ino, path.stat().st_mtime_ns, content))
        (location / ".matuto-downloads").mkdir()
        (location / ".matuto-downloads" / "partial.gguf").write_bytes(b"partial")
        outside = self.home / "outside.gguf"
        outside.write_bytes(b"outside")
        (location / "outside.gguf").symlink_to(outside)
        location_id = storage.add_artifact_location(self.opened.connection(), location)["location_id"]
        status, listed = self.request("POST", {"action": "artifact-files", "location_id": location_id})
        self.assertEqual(status, 200)
        self.assertEqual({item["name"] for item in listed["files"]}, {path.name for path, *_ in originals})
        self.assertTrue(all("path" not in item for item in listed["files"]))

        payload = {"action": "adopt", "location_id": location_id,
                   "file_name": originals[0][0].name, "name": "Local model", "consent": False}
        status, refusal = self.request("POST", payload)
        self.assertEqual(status, 409)
        self.assertIn("explicit consent", refusal["error"])
        self.assertEqual(self.opened.connection().execute(
            "SELECT count(*) FROM acquisition_records").fetchone()[0], 0)
        for unsafe_name in ("outside.gguf", "../outside.gguf"):
            status, refused = self.request("POST", {**payload, "file_name": unsafe_name,
                                                       "consent": True})
            self.assertEqual(status, 409)
            self.assertIn("listed file", refused["error"])
        payload["consent"] = True
        status, adopted = self.request("POST", payload)
        self.assertEqual(status, 200, adopted)
        self.assertTrue(adopted["complete"])
        self.assertEqual(adopted["label"], "locally hashed; publisher unverified")
        self.assertIsNone(storage.model_storage(self.opened.connection()))
        for path, inode, mtime, content in originals:
            self.assertEqual((path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns),
                             (content, inode, mtime))
        status, page = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(len(page["records"]), 1)
        adopted_record, = page["records"]
        self.assertTrue(adopted_record["complete"])
        self.assertEqual(adopted_record["source"], "unknown")
        self.assertEqual({item["file_name"] for item in adopted_record["files"]},
                         {path.name for path, *_ in originals})

    def test_record_mutations_require_session_origin_and_csrf(self):
        location = self.home / "protected"
        location.mkdir(mode=0o700)
        location_id = storage.add_artifact_location(self.opened.connection(), location)["location_id"]
        (location / "model.gguf").write_bytes(b"model")
        for payload in ({"action": "adopt", "location_id": location_id, "file_name": "model.gguf",
                         "consent": True},
                        {"action": "verify-record", "acquisition_id": "missing"}):
            for cookie, csrf, origin in ((False, True, True), (True, False, True), (True, True, False)):
                with self.subTest(payload=payload, cookie=cookie, csrf=csrf, origin=origin):
                    status, answer = self.request("POST", payload, cookie=cookie, csrf=csrf, origin=origin)
                    self.assertIn(status, (401, 403))
                    self.assertNotIn("acquisition_id", answer)

    def test_root_wizard_inline_style_and_script_match_csp_nonce(self):
        request = urllib.request.Request(self.address + "/", headers={
            "Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}"})
        with urllib.request.urlopen(request, timeout=10) as response:
            html = response.read().decode()
            policy = response.headers["Content-Security-Policy"]
        self.assertIn(f"<style nonce=\"{self.csrf}\">", html)
        self.assertIn(f"<script nonce=\"{self.csrf}\">", html)
        self.assertIn(f"style-src 'self' 'nonce-{self.csrf}'", policy)

    def test_missing_storage_refuses_before_runner_start(self):
        origins.add(self.opened.connection(), "organisation", "owner")
        status, answer = self.request("POST", {"action": "acquire", "repository": "owner/model",
            "revision": COMMIT, "files": [FILES[0][0]], "consent": False})
        self.assertEqual(status, 409)
        self.assertIn("model storage", answer["error"])
        self.assertEqual(FakeRunner.starts, 0)

    def test_download_mutations_reject_missing_session_origin_and_csrf(self):
        actions = (
            {"action": "resume", "job_id": "download-0123456789abcdef"},
            {"action": "discard-preview", "job_id": "download-0123456789abcdef"},
            {"action": "discard", "job_id": "download-0123456789abcdef",
             "preview": {"job_id": "download-0123456789abcdef", "files": [], "total_size": 0}},
        )
        for payload in actions:
            for cookie, csrf, origin in ((False, True, True), (True, False, True), (True, True, False)):
                with self.subTest(action=payload["action"], cookie=cookie, csrf=csrf, origin=origin):
                    status, answer = self.request("POST", payload, cookie=cookie, csrf=csrf, origin=origin)
                    self.assertIn(status, (401, 403))
                    self.assertNotIn("job_id", answer)


if __name__ == "__main__":
    unittest.main()
