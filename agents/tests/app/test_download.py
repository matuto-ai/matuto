"""Pinned, resumable transfers through the download job API and local HTTP fixtures."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
import os, pathlib, sys
state = pathlib.Path(os.environ["RUNNER_SYSTEMD"])
args = sys.argv[1:]
if args and args[0] == "--user": args = args[1:]
if args and args[0] == "list-units": sys.exit(0)
if args and args[0] == "show":
    unit = args[1]
    path = state / unit
    values = {"LoadState": "loaded" if path.exists() else "not-found",
              "ActiveState": path.read_text().strip() if path.exists() else "inactive",
              "SubState": (state / (unit + ".substate")).read_text().strip() if path.exists() else "dead",
              "Result": (state / (unit + ".result")).read_text().strip() if path.exists() else "success",
              "ExecMainStatus": (state / (unit + ".status")).read_text().strip() if path.exists() else "0",
              "StateChangeTimestampMonotonic": "0"}
    for arg in args:
        if arg.startswith("--property="):
            key = arg.split("=", 1)[1]
            print(f"{key}={values.get(key, '')}")
    sys.exit(0)
sys.exit(2)
'''

FAKE_SYSTEMD_RUN = '''#!/usr/bin/env python3
import os, pathlib, subprocess, sys, time
state = pathlib.Path(os.environ["RUNNER_SYSTEMD"])
args = sys.argv[1:]
unit = next(a.split("=", 1)[1] for a in args if a.startswith("--unit="))
env = dict(os.environ)
for arg in args:
    if arg.startswith("--setenv="):
        key, _, value = arg[len("--setenv="):].partition("=")
        env[key] = value
unit_path = state / unit
unit_path.write_text("active")
command = args[args.index("--") + 1:]
log = next((a.split(":", 1)[1] for a in args
            if a.startswith("--property=StandardOutput=append:")), os.devnull)
with open(log, "a") as output:
    result = subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT)
unit_path.write_text("active" if result.returncode == 0 else "failed")
(state / (unit + ".substate")).write_text("exited" if result.returncode == 0 else "failed")
(state / (unit + ".result")).write_text("success" if result.returncode == 0 else "exit-code")
(state / (unit + ".status")).write_text(str(result.returncode))
(state / (unit + ".state-monotonic")).write_text(str(time.clock_gettime_ns(time.CLOCK_MONOTONIC)//1000))
sys.exit(result.returncode)
'''

from matuto import acquisition, adoption, admission, download, login, origins, progress, runner, sessions, storage, store  # noqa: E402
from matuto.web import ALLOWED_HOST, SESSION_COOKIE, build_server  # noqa: E402

COMMIT = "0123456789abcdef0123456789abcdef01234567"
BODY = {"model.gguf": b"model shard", "tokenizer.json": b"tokenizer"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.rsplit("/", 1)[-1]
        body = getattr(self.server, "extra_bodies", {}).get(name, BODY.get(name))
        if body is None:
            self.send_error(404)
            return
        start = int(self.headers.get("Range", "bytes=0-").split("=", 1)[1].split("-", 1)[0])
        self.server.requests.append((name, start))
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body) - start))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(body)-1}/{len(body)}")
        self.end_headers()
        if (name in self.server.drop_always or
                (name in self.server.drop_once and name not in self.server.dropped)) and start == 0:
            self.server.dropped.add(name)
            self.wfile.write(body[:4])
            self.wfile.flush()
            return
        self.wfile.write(body[start:])

    def log_message(self, *_args):
        pass


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.root = self.home / "models"
        self.root.mkdir(mode=0o700)
        self.opened = store.open_store(self.config)
        self.addCleanup(self.opened.close)
        storage.save_model_storage(self.opened.connection(), self.root)
        self.origin_id = origins.add(self.opened.connection(), "organisation", "owner")["origin_id"]
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.http.requests = []
        self.http.extra_bodies = {}
        self.http.drop_once = set()
        self.http.drop_always = set()
        self.http.dropped = set()
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_http)
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        self.files = tuple(acquisition.File(
            name, len(body), hashlib.sha256(body).hexdigest(), "published SHA-256",
            f"https://huggingface.co/owner/model/resolve/{COMMIT}/{name}")
            for name, body in BODY.items())
        self.resolved = acquisition.Resolution("owner/model", COMMIT, self.files,
                                               sum(len(body) for body in BODY.values()))

    def close_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def pin(self, *, consent=False, acquisition_id="acq-test"):
        return download._pin(self.resolved, str(self.root), consent, acquisition_id,
                             origin_id=self.origin_id, api_base=self.base)

    def insert_job(self, job_id="download-0123456789abcdef", pinned=None):
        pinned = pinned or self.pin()
        self.opened.connection().execute(
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at)"
            " VALUES (?, 'download', ?, ?, '2026-10-12T00:00:00+00:00')",
            (job_id, "matuto-download-0123456789abcdef.service",
             json.dumps(pinned, sort_keys=True, separators=(",", ":"))))
        return job_id

    def execute(self, job_id="download-0123456789abcdef", pinned=None, **kwargs):
        report = kwargs.pop("report", lambda *_: None)
        return download.execute(self.opened.connection(), self.insert_job(job_id, pinned),
                                report=report, echo=lambda *_: None, **kwargs)

    def test_full_set_is_verified_then_placed_atomically_under_its_revision(self):
        self.assertEqual(self.execute(), 0)
        row = self.opened.connection().execute(
            "SELECT r.*, l.path FROM acquisition_records r JOIN artifact_locations l USING(location_id)"
        ).fetchone()
        final = download._final_path(self.root, self.pin())
        self.assertEqual(Path(row["path"]), final)
        self.assertEqual(final.parent, self.root)
        discovered = {path for directory in self.root.iterdir() if directory.is_dir()
                      for path in directory.iterdir() if path.suffix == ".gguf"}
        self.assertIn(final / "model.gguf", discovered)
        self.assertEqual(row["verified_at"] is not None, True)
        self.assertEqual({p.name for p in final.iterdir()
                          if p.name != ".matuto-download.json"}, set(BODY))
        files = self.opened.connection().execute(
            "SELECT file_name, computed_sha256, verified_at FROM acquisition_files"
        ).fetchall()
        self.assertEqual({item["file_name"] for item in files}, set(BODY))
        for item in files:
            self.assertEqual(item["computed_sha256"], hashlib.sha256(BODY[item["file_name"]]).hexdigest())
            self.assertTrue(item["verified_at"])
        self.assertFalse(download._stage_path(self.root, self.pin()).exists())

    def test_verification_reports_the_file_and_bytes_actually_being_hashed(self):
        reports = []
        report_path = self.home / "download.progress"
        def report(phase, step):
            reports.append((phase, step))
            download.progress.report(phase, step)
        with mock.patch.dict(os.environ, {download.progress.FILE_VARIABLE: str(report_path)}):
            self.assertEqual(self.execute(report=report), 0)
        self.assertEqual(download.progress.latest(report_path),
                         ("verification", "tokenizer.json: 9 of 9 bytes"))
        verification_steps = [step for phase, step in reports if phase == "verification" and step]
        self.assertEqual(verification_steps, [
            "model.gguf: 0 of 11 bytes", "model.gguf: 11 of 11 bytes",
            "tokenizer.json: 0 of 9 bytes", "tokenizer.json: 9 of 9 bytes",
        ])

    def test_live_verification_progress_is_readable_through_authenticated_http(self):
        job_id = "download-0123456789abcdef"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)}):
            login.create_login("operator", "secret", "secret")
            token, _expiry = sessions.start_session("operator", "secret")
            key = store.store_directory(self.config).parent / "pi-local" / "api-key"
            key.parent.mkdir(parents=True, exist_ok=True)
            key.write_text("fixture-key")
            running = runner.Runner(self.config, opened_store=self.opened,
                                    serving_state=lambda: admission.STOPPED)
            running._prepare(job_id)
            running._log_path(job_id).touch()
            server = build_server(port=0)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            address = f"http://127.0.0.1:{server.server_port}/api/acquisition"
            headers = {"Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={token}"}
            active = {runner.LOAD_STATE: "loaded", runner.ACTIVE_STATE: "active",
                      runner.SUB_STATE: "running", runner.RESULT: "success",
                      runner.EXIT_STATUS: "0"}
            observed = []

            def request_status():
                request = urllib.request.Request(address, headers=headers)
                with urllib.request.urlopen(request, timeout=12) as response:
                    return response.status, json.loads(response.read())

            def report(phase, step):
                progress.report(phase, step, path=running._progress_path(job_id))
                if phase == "verification" and step == "model.gguf: 11 of 11 bytes" and not observed:
                    status, answer = request_status()
                    item = answer["downloads"][0]
                    self.assertEqual(status, 200)
                    self.assertEqual((item["phase"], item["file"], item["transferred"], item["total"]),
                                     ("verification", "model.gguf", 11, 11))
                    observed.append(True)

            try:
                with mock.patch.object(runner.Runner, "_unit_state", return_value=active):
                    status, _answer = request_status()
                    self.assertEqual(status, 200)
                    self.assertEqual(self.execute(job_id=job_id, report=report), 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
        self.assertTrue(observed, "the authenticated page read progress while verification was still running")

    def test_nested_shards_and_projector_keep_native_discovery_names(self):
        bodies = {"model-00001-of-00002.gguf": b"first shard",
                  "model-00002-of-00002.gguf": b"second shard",
                  "mmproj.gguf": b"projector"}
        self.http.extra_bodies.update(bodies)
        source = self.files[0]
        files = tuple(acquisition.File(
            f"nested/{name}", len(body), hashlib.sha256(body).hexdigest(),
            "published SHA-256", source.url.replace("/model.gguf", f"/nested/{name}"))
            for name, body in bodies.items())
        resolved = acquisition.Resolution("owner/model", COMMIT, files,
                                           sum(item.size for item in files))
        pinned = download._pin(resolved, str(self.root), False, "acq-nested-shards",
                               origin_id=self.origin_id, api_base=self.base)

        self.assertEqual(self.execute(pinned=pinned), 0)

        final = download._final_path(self.root, pinned)
        discovered = {path for directory in self.root.iterdir() if directory.is_dir()
                      for path in directory.iterdir() if path.suffix == ".gguf"}
        aliases = {}
        for item in pinned["files"]:
            original_path = final.joinpath(*item["name"].split("/"))
            self.assertEqual(original_path.read_bytes(), bodies[Path(item["name"]).name])
            aliases[item["name"]] = next(path for path in discovered
                                         if os.path.samefile(original_path, path))
        first = aliases["nested/model-00001-of-00002.gguf"].name
        second = aliases["nested/model-00002-of-00002.gguf"].name
        projector = aliases["nested/mmproj.gguf"].name
        self.assertTrue(first.endswith("-00001-of-00002.gguf"))
        self.assertTrue(second.endswith("-00002-of-00002.gguf"))
        self.assertEqual(first.removesuffix("-00001-of-00002.gguf"),
                         second.removesuffix("-00002-of-00002.gguf"))
        self.assertIn("mmproj", projector)
        recorded = {row["file_name"] for row in self.opened.connection().execute(
            "SELECT file_name FROM acquisition_files WHERE acquisition_id = 'acq-nested-shards'")}
        self.assertEqual(recorded, {item["name"] for item in pinned["files"]})

    def test_marker_cleanup_preserves_pinned_sidecars(self):
        bodies = {".matuto-download.json.sidecar.tmp": b"sidecar",
                  ".matuto-download.json.0123456789abcdef0123456789abcdef.tmp": b"uuid sidecar"}
        self.http.extra_bodies.update(bodies)
        source = self.files[0]
        files = (source,) + tuple(acquisition.File(
            name, len(body), hashlib.sha256(body).hexdigest(), "published SHA-256",
            source.url.replace("/model.gguf", f"/{name}")) for name, body in bodies.items())
        resolved = acquisition.Resolution("owner/model", COMMIT, files,
                                           sum(item.size for item in files))
        pinned = download._pin(resolved, str(self.root), False, "acq-marker-sidecars",
                               origin_id=self.origin_id, api_base=self.base)

        with mock.patch.object(download.uuid, "uuid4", side_effect=[
                SimpleNamespace(hex="a" * 32),
                SimpleNamespace(hex="0123456789abcdef0123456789abcdef"),
                SimpleNamespace(hex="b" * 32)]):
            self.assertEqual(self.execute(pinned=pinned), 0)

        final = download._final_path(self.root, pinned)
        for name, body in bodies.items():
            self.assertEqual((final / name).read_bytes(), body)
        record = self.opened.connection().execute(
            "SELECT verified_at FROM acquisition_records WHERE acquisition_id = ?",
            (pinned["acquisition_id"],)).fetchone()
        self.assertIsNotNone(record["verified_at"])

    def test_different_revisions_are_discoverable_beside_each_other(self):
        other_revision = "f" * 40
        files = tuple(acquisition.File(item.name, item.size, item.sha256, item.checksum_status,
                                      item.url.replace(COMMIT, other_revision))
                      for item in self.files)
        resolved = acquisition.Resolution("owner/model", other_revision, files,
                                           sum(item.size for item in files))
        pinned = download._pin(resolved, str(self.root), False, "acq-other-revision",
                               origin_id=self.origin_id, api_base=self.base)

        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.execute(job_id="download-fedcba9876543210", pinned=pinned), 0)

        first = download._final_path(self.root, self.pin())
        second = download._final_path(self.root, pinned)
        self.assertNotEqual(first, second)
        self.assertTrue((first / "model.gguf").is_file())
        self.assertTrue((second / "model.gguf").is_file())

    def test_an_existing_final_revision_refuses_without_overwriting_either_copy(self):
        final = download._final_path(self.root, self.pin())
        final.mkdir(parents=True)
        (final / "model.gguf").write_bytes(b"existing")
        self.assertEqual(self.execute(), 1)
        self.assertEqual((final / "model.gguf").read_bytes(), b"existing")
        stage = download._stage_path(self.root, self.pin())
        self.assertEqual((stage / "model.gguf").read_bytes(), BODY["model.gguf"])
        self.assertEqual(self.opened.connection().execute(
            "SELECT count(*) FROM acquisition_records").fetchone()[0], 0)

    def test_a_published_checksum_mismatch_never_reaches_final_storage(self):
        bad = tuple(acquisition.File(
            item.name, item.size, "f" * 64, item.checksum_status, item.url) for item in self.files)
        pinned = download._pin(acquisition.Resolution("owner/model", COMMIT, bad,
            sum(item.size for item in bad)), str(self.root), False, "acq-test",
            origin_id=self.origin_id, api_base=self.base)
        self.assertEqual(self.execute(pinned=pinned), 1)
        stage = download._stage_path(self.root, pinned)
        self.assertEqual((stage / "model.gguf").read_bytes(), BODY["model.gguf"])
        self.assertFalse(download._final_path(self.root, pinned).exists())
        self.assertFalse(self.opened.connection().execute(
            "SELECT 1 FROM acquisition_records").fetchone())

    def test_resume_continues_a_partial_file_from_its_retained_byte_offset(self):
        pinned = self.pin()
        stage = download._stage_path(self.root, pinned)
        stage.mkdir(parents=True, mode=0o700)
        partial = BODY["model.gguf"][:4]
        (stage / "model.gguf").write_bytes(partial)

        self.assertEqual(self.execute(pinned=pinned), 0)

        self.assertIn(("model.gguf", len(partial)), self.http.requests)
        final = download._final_path(self.root, pinned)
        self.assertEqual((final / "model.gguf").read_bytes(), BODY["model.gguf"])

    def test_failed_curl_keeps_only_its_staged_partial_and_has_bounded_retries(self):
        pinned = self.pin()
        untouched = self.root / "keep.bin"
        untouched.write_bytes(b"leave this file alone")
        observed = {}

        class FailedTransfer:
            returncode = 23
            def poll(self):
                return self.returncode

        def fail_after_writing(argv, **_kwargs):
            observed["argv"] = argv
            target = Path(argv[argv.index("--output") + 1])
            target.write_bytes(BODY["model.gguf"][:4])
            return FailedTransfer()

        self.assertEqual(self.execute(pinned=pinned, popen=fail_after_writing), 1)
        self.assertEqual(observed["argv"][observed["argv"].index("--retry") + 1],
                         str(download.RETRIES))
        stage = download._stage_path(self.root, pinned)
        self.assertEqual((stage / "model.gguf").read_bytes(), BODY["model.gguf"][:4])
        self.assertEqual(untouched.read_bytes(), b"leave this file alone")
        self.assertFalse(download._final_path(self.root, pinned).exists())
        self.assertEqual(self.opened.connection().execute(
            "SELECT count(*) FROM acquisition_records").fetchone()[0], 0)

    def test_resume_rechecks_a_corrupt_complete_partial_and_fetches_it_again(self):
        pinned = self.pin()
        stage = download._stage_path(self.root, pinned)
        stage.mkdir(parents=True, mode=0o700)
        (stage / "model.gguf").write_bytes(b"bad bytes!!!")
        self.assertEqual(self.execute(pinned=pinned), 0)
        self.assertTrue(any(name == "model.gguf" and start == 0 for name, start in self.http.requests))
        final = download._final_path(self.root, pinned)
        self.assertEqual((final / "model.gguf").read_bytes(), BODY["model.gguf"])

    def test_without_no_checksum_consent_record_stays_unusable_and_staged(self):
        files = (self.files[0], acquisition.File(
            "tokenizer.json", len(BODY["tokenizer.json"]), None,
            "no published SHA-256", self.files[1].url))
        pinned = download._pin(acquisition.Resolution("owner/model", COMMIT, files,
            sum(item.size for item in files)), str(self.root), False, "acq-unconsented",
            origin_id=self.origin_id, api_base=self.base)
        self.assertEqual(self.execute(pinned=pinned), 0)
        record = self.opened.connection().execute(
            "SELECT r.verified_at, l.path FROM acquisition_records r JOIN artifact_locations l"
            " USING(location_id) WHERE acquisition_id = 'acq-unconsented'").fetchone()
        self.assertIsNone(record["verified_at"])
        self.assertEqual(Path(record["path"]), download._stage_path(self.root, pinned))
        final = download._final_path(self.root, pinned)
        self.assertFalse(final.exists())

        verified = download.verify_pending(self.opened.connection(), "acq-unconsented", consent=True)

        self.assertTrue(verified.complete, verified.reason)
        self.assertEqual(Path(self.opened.connection().execute(
            "SELECT l.path FROM acquisition_records r JOIN artifact_locations l USING(location_id)"
            " WHERE acquisition_id = 'acq-unconsented'").fetchone()[0]), final)
        self.assertEqual({item.name for item in final.iterdir()
                          if item.name != ".matuto-download.json"}, set(BODY))

    def test_old_download_staging_stays_private_after_storage_changes(self):
        pinned = self.pin(acquisition_id="acq-old-stage")
        self.insert_job(pinned=pinned)
        stage = download._stage_path(self.root, pinned)
        stage.mkdir(parents=True, mode=0o700)
        partial = stage / "model.gguf"
        partial.write_bytes(BODY["model.gguf"][:4])
        moved_storage = self.home / "moved-models"
        moved_storage.mkdir(mode=0o700)
        self.opened.connection().execute(
            "UPDATE artifact_locations SET path = ? WHERE location_id = "
            "(SELECT location_id FROM model_storage WHERE setting_id = 1)",
            (str(moved_storage),))
        storage.add_artifact_location(self.opened.connection(), self.root)

        with self.assertRaisesRegex(adoption.AdoptionError, "private download or work staging"):
            adoption.adopt(self.opened.connection(), "acq-adopt-old", "partial", partial,
                           consent=True)

    def test_download_staging_cannot_be_adopted_as_a_complete_model(self):
        pinned = self.pin(acquisition_id="acq-stage")
        stage = download._stage_path(self.root, pinned)
        stage.mkdir(parents=True, mode=0o700)
        partial = stage / "model.gguf"
        partial.write_bytes(BODY["model.gguf"][:4])

        with self.assertRaisesRegex(adoption.AdoptionError, "private download or work staging"):
            adoption.adopt(self.opened.connection(), "acq-adopt", "partial", partial, consent=True)
        self.assertFalse(self.opened.connection().execute(
            "SELECT 1 FROM acquisition_records WHERE acquisition_id = 'acq-adopt'").fetchone())

    def test_separate_acquisitions_never_share_staging(self):
        first = self.pin(acquisition_id="acq-first")
        second = self.pin(acquisition_id="acq-second")

        self.assertNotEqual(download._stage_path(self.root, first),
                            download._stage_path(self.root, second))

    def test_resume_discards_a_corrupt_incomplete_prefix_and_refetches_from_zero(self):
        pinned = self.pin()
        stage = download._stage_path(self.root, pinned)
        stage.mkdir(parents=True, mode=0o700)
        (stage / "model.gguf").write_bytes(b"bad!")

        self.assertEqual(self.execute(pinned=pinned), 0)

        offsets = [start for name, start in self.http.requests if name == "model.gguf"]
        self.assertEqual(offsets, [4, 0])
        self.assertEqual((download._final_path(self.root, pinned) / "model.gguf").read_bytes(),
                         BODY["model.gguf"])

    def test_resume_refetches_unpublished_checksum_partials_from_zero(self):
        files = (self.files[0], acquisition.File(
            "tokenizer.json", len(BODY["tokenizer.json"]), None,
            "no published SHA-256", self.files[1].url))
        pinned = download._pin(acquisition.Resolution("owner/model", COMMIT, files,
            sum(item.size for item in files)), str(self.root), True, "acq-unpublished-resume",
            origin_id=self.origin_id, api_base=self.base)
        stage = download._stage_path(self.root, pinned)
        stage.mkdir(parents=True, mode=0o700)
        (stage / "tokenizer.json").write_bytes(BODY["tokenizer.json"][:4])

        self.assertEqual(self.execute(pinned=pinned), 0)

        self.assertIn(("tokenizer.json", 0), self.http.requests)
        self.assertEqual((download._final_path(self.root, pinned) / "tokenizer.json").read_bytes(),
                         BODY["tokenizer.json"])

    def test_dropped_connection_is_retried_by_real_curl(self):
        pinned = self.pin()
        self.http.drop_once.add("model.gguf")

        self.assertEqual(self.execute(pinned=pinned), 0)

        requests = [(name, start) for name, start in self.http.requests if name == "model.gguf"]
        self.assertEqual(requests, [("model.gguf", 0), ("model.gguf", 0)])
        self.assertEqual((download._final_path(self.root, pinned) / "model.gguf").read_bytes(),
                         BODY["model.gguf"])

    def test_exhausted_real_curl_retries_fail_and_keep_the_partial(self):
        pinned = self.pin()
        self.http.drop_always.add("model.gguf")

        self.assertEqual(self.execute(pinned=pinned), 1)

        requests = [(name, start) for name, start in self.http.requests if name == "model.gguf"]
        self.assertEqual(len(requests), download.RETRIES + 1)
        self.assertTrue(all(start == 0 for _, start in requests))
        stage = download._stage_path(self.root, pinned)
        self.assertEqual((stage / "model.gguf").read_bytes(), BODY["model.gguf"][:4])
        self.assertFalse(download._final_path(self.root, pinned).exists())

    def test_resume_reconciles_a_placement_interrupted_before_store_commit(self):
        pinned = self.pin()
        job_id = self.insert_job(pinned=pinned)
        script = f'''\nimport os, sys\nsys.path.insert(0, {str(AGENTS)!r})\nfrom matuto import download, store\nopened = store.open_store({str(self.config)!r})\nrename = download.os.rename\ndef interrupt(source, destination):\n    rename(source, destination)\n    os._exit(99)\ndownload.os.rename = interrupt\ndownload.execute(opened.connection(), {job_id!r}, report=lambda *_: None, echo=lambda *_: None)\n'''
        child = subprocess.run([sys.executable, "-c", script], cwd=AGENTS.parent,
                               capture_output=True, text=True)
        final = download._final_path(self.root, pinned)
        self.assertEqual(child.returncode, 99, child.stderr)
        self.assertTrue(download._owns_placement(final, pinned))
        self.assertFalse(self.opened.connection().execute(
            "SELECT 1 FROM acquisition_records WHERE acquisition_id = ?",
            (pinned["acquisition_id"],)).fetchone())
        running = SimpleNamespace(store=self.opened, logs=self.home / "runner-work",
                                  serving_state=lambda: admission.STOPPED,
                                  start=mock.Mock(return_value=SimpleNamespace(
                                      decision=admission.ADMIT, work=object())))
        disk_usage = mock.Mock(side_effect=AssertionError("placed files need no transfer space"))
        resumed = download.resume(running, job_id, disk_usage=disk_usage)
        self.assertEqual(resumed.start.decision, admission.ADMIT)
        running.start.assert_called_once()
        disk_usage.assert_not_called()

        self.assertEqual(download.execute(self.opened.connection(), job_id,
                                          report=lambda *_: None, echo=lambda *_: None), 0)

        record = self.opened.connection().execute(
            "SELECT l.path, r.verified_at FROM acquisition_records r"
            " JOIN artifact_locations l USING(location_id) WHERE r.acquisition_id = ?",
            (pinned["acquisition_id"],)).fetchone()
        self.assertEqual(Path(record["path"]), final)
        self.assertTrue(record["verified_at"])
        self.assertFalse(download._stage_path(self.root, pinned).exists())

    def test_later_consent_recovers_placement_interrupted_before_store_commit(self):
        files = (acquisition.File("tokenizer.json", len(BODY["tokenizer.json"]), None,
                                  "no published SHA-256", self.files[1].url),)
        resolved = acquisition.Resolution("owner/model", COMMIT, files, files[0].size)
        pinned = download._pin(resolved, str(self.root), False, "acq-pending",
                               origin_id=self.origin_id, api_base=self.base)
        job_id = self.insert_job(pinned=pinned)
        self.assertEqual(download.execute(self.opened.connection(), job_id,
                                          report=lambda *_: None, echo=lambda *_: None), 0)
        script = f'''\nimport os, sys\nsys.path.insert(0, {str(AGENTS)!r})\nfrom matuto import download, store\nopened = store.open_store({str(self.config)!r})\nrename = download.os.rename\ndef interrupt(source, destination):\n    rename(source, destination)\n    os._exit(99)\ndownload.os.rename = interrupt\ndownload.verify_pending(opened.connection(), {pinned["acquisition_id"]!r}, consent=True)\n'''
        child = subprocess.run([sys.executable, "-c", script], cwd=AGENTS.parent,
                               capture_output=True, text=True)
        final = download._final_path(self.root, pinned)
        self.assertEqual(child.returncode, 99, child.stderr)
        self.assertTrue(download._owns_placement(final, pinned))

        verified = download.verify_pending(self.opened.connection(), pinned["acquisition_id"],
                                           consent=True)

        self.assertTrue(verified.complete, verified.reason)
        row = self.opened.connection().execute(
            "SELECT l.path, r.verified_at FROM acquisition_records r"
            " JOIN artifact_locations l USING(location_id) WHERE r.acquisition_id = ?",
            (pinned["acquisition_id"],)).fetchone()
        self.assertEqual(Path(row["path"]), final)
        self.assertTrue(row["verified_at"])

    def test_marker_interruption_before_atomic_publication_leaves_no_poison_marker(self):
        pinned = self.pin()
        stage = download._stage_path(self.root, pinned)
        stage.mkdir(parents=True, mode=0o700)
        self.insert_job(pinned=pinned)
        script = f'''\nimport os, sys\nfrom pathlib import Path\nsys.path.insert(0, {str(AGENTS)!r})\nfrom matuto import download\ndef interrupt(*_args, **_kwargs):\n    os._exit(99)\ndownload.os.link = interrupt\ndownload._write_placement_marker(Path({str(stage)!r}), {pinned!r})\n'''

        child = subprocess.run([sys.executable, "-c", script], cwd=AGENTS.parent,
                               capture_output=True, text=True)

        self.assertEqual(child.returncode, 99, child.stderr)
        self.assertFalse((stage / ".matuto-download.json").exists())
        self.assertFalse(download._owns_placement(stage, pinned))
        download._write_placement_marker(stage, pinned)
        self.assertTrue(download._owns_placement(stage, pinned))
        self.assertFalse(list(stage.glob(".matuto-download.json.*.tmp")))

    def test_space_check_counts_unverifiable_bytes_that_resume_will_refetch(self):
        files = (acquisition.File("tokenizer.json", len(BODY["tokenizer.json"]), None,
                                  "no published SHA-256", self.files[1].url),)
        pinned = download._pin(acquisition.Resolution("owner/model", COMMIT, files,
            files[0].size), str(self.root), True, "acq-space-recheck",
            origin_id=self.origin_id, api_base=self.base)
        stage = download._stage_path(self.root, pinned)
        stage.mkdir(parents=True, mode=0o700)
        (stage / "tokenizer.json").write_bytes(BODY["tokenizer.json"][:4])

        self.assertEqual(download.require_space(
            str(self.root), pinned, disk_usage=lambda _path: SimpleNamespace(free=5)),
            len(BODY["tokenizer.json"]))

    def test_space_shortfall_refuses_before_runner_start(self):
        running = SimpleNamespace(store=self.opened, logs=self.home / "runner-work",
                                  serving_state=lambda: admission.STOPPED, start=mock.Mock())
        with self.assertRaisesRegex(download.DownloadError, "bytes short"):
            download.submit(running, self.resolved, api_base=self.base,
                            disk_usage=lambda _path: SimpleNamespace(free=0))
        running.start.assert_not_called()

    def discard_running(self, *, state=None):
        running = SimpleNamespace(store=self.opened, logs=self.home / "runner-work",
                                  serving_state=lambda: admission.STOPPED,
                                  start=mock.Mock())
        running.works = mock.Mock(return_value=[])
        running._unit_state = mock.Mock(return_value=state or {
            runner.LOAD_STATE: runner.NOT_FOUND, runner.ACTIVE_STATE: "inactive",
            runner.SUB_STATE: "dead", runner.RESULT: "success", runner.EXIT_STATUS: "0"})
        running._launching = mock.Mock(return_value=False)
        return running

    def staged_job(self, *, job_id="download-0123456789abcdef", pinned=None):
        pinned = pinned or self.pin()
        self.insert_job(job_id, pinned)
        stage = download._stage_path(self.root, pinned)
        stage.mkdir(parents=True, mode=0o700)
        for item in pinned["files"]:
            body = BODY[Path(item["name"]).name]
            (stage / item["name"]).parent.mkdir(parents=True, exist_ok=True)
            (stage / item["name"]).write_bytes(body[:4])
        return job_id, pinned, stage

    def test_discard_preview_lists_every_file_it_will_remove_and_exact_size(self):
        running = self.discard_running()
        job_id, _pinned, stage = self.staged_job()
        (self.root / "pre-existing.gguf").write_bytes(b"keep")

        preview = download.preview_discard(running, job_id)

        self.assertEqual(preview.files, (("model.gguf", 4), ("tokenizer.json", 4)))
        self.assertEqual(preview.total_size, 8)
        self.assertEqual(preview.consequence, "The next attempt starts again from nothing.")
        self.assertEqual((self.root / "pre-existing.gguf").read_bytes(), b"keep")
        self.assertTrue(stage.exists())

    def test_preview_lists_generated_alias_and_marker_that_confirmation_deletes(self):
        running = self.discard_running()
        source = self.files[0]
        nested = acquisition.File(
            "nested/model.gguf", source.size, source.sha256, source.checksum_status,
            source.url.replace("/model.gguf", "/nested/model.gguf"))
        resolved = acquisition.Resolution("owner/model", COMMIT, (nested,), nested.size)
        pinned = download._pin(resolved, str(self.root), False, "acq-discard-generated",
                               origin_id=self.origin_id, api_base=self.base)
        job_id = self.insert_job(pinned=pinned)
        stage = download._stage_path(self.root, pinned)
        (stage / "nested").mkdir(parents=True)
        (stage / nested.name).write_bytes(BODY["model.gguf"][:4])
        download._add_discovery_aliases(stage, pinned)
        download._write_placement_marker(stage, pinned)
        expected = {nested.name, download._discovery_alias(nested.name), ".matuto-download.json"}

        preview = download.preview_discard(running, job_id)
        self.assertEqual({name for name, _size in preview.files}, expected)
        self.assertEqual(preview.total_size, sum(size for _name, size in preview.files))

        download.confirm_discard(running, preview)
        self.assertFalse(stage.exists())

    def test_discard_refuses_empty_jobs_and_completed_records_after_a_vanished_unit(self):
        running = self.discard_running()
        job_id, pinned, _stage = self.staged_job()
        stage = download._stage_path(self.root, pinned)
        for item in pinned["files"]:
            (stage / item["name"]).unlink()
        stage.rmdir()
        self.opened.connection().execute(
            "UPDATE jobs SET outcome = 'failed', ended_at = 'then' WHERE job_id = ?", (job_id,))
        with self.assertRaisesRegex(download.DownloadError, "no partial files"):
            download.preview_discard(running, job_id)

        final = self.root / "completed-model"
        final.mkdir()
        completed_file = final / "model.gguf"
        completed_file.write_bytes(BODY["model.gguf"])
        location_id = "loc-f7-completed"
        self.opened.connection().execute(
            "INSERT INTO artifact_locations (location_id, path, notes, created_at)"
            " VALUES (?, ?, '', 'then')", (location_id, str(final)))
        self.opened.connection().execute(
            "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
            " origin_id, repository, revision, location_id, verified_at, created_at)"
            " VALUES (?, 'Complete', 'download', 'published-checksum', ?, 'owner/model', ?, ?,"
            " 'then', 'then')", (pinned["acquisition_id"], self.origin_id, COMMIT, location_id))
        self.opened.connection().execute(
            "INSERT INTO acquisition_files (acquisition_id, file_name, size_bytes, published_sha256,"
            " computed_sha256, verified_at) VALUES (?, 'model.gguf', ?, ?, ?, 'then')",
            (pinned["acquisition_id"], len(BODY["model.gguf"]),
             hashlib.sha256(BODY["model.gguf"]).hexdigest(),
             hashlib.sha256(BODY["model.gguf"]).hexdigest()))
        self.opened.connection().execute(
            "UPDATE jobs SET outcome = 'interrupted' WHERE job_id = ?", (job_id,))

        with self.assertRaisesRegex(download.DownloadError, "already has an acquisition record"):
            download.preview_discard(running, job_id)
        self.assertEqual(completed_file.read_bytes(), BODY["model.gguf"])
        self.assertEqual(runner._work(self.opened.connection(), job_id).outcome, "interrupted")

    def test_discard_refuses_running_unknown_completed_and_non_download_work(self):
        job_id, _pinned, stage = self.staged_job()
        live = self.discard_running(state={runner.LOAD_STATE: "loaded",
                                           runner.ACTIVE_STATE: "active",
                                           runner.SUB_STATE: "running"})
        with self.assertRaisesRegex(download.DownloadError, "may still write"):
            download.preview_discard(live, job_id)
        unknown = self.discard_running(state=None)
        unknown._unit_state.return_value = None
        with self.assertRaisesRegex(download.DownloadError, "state.*unknown"):
            download.preview_discard(unknown, job_id)
        self.opened.connection().execute(
            "UPDATE jobs SET outcome = 'completed' WHERE job_id = ?", (job_id,))
        with self.assertRaisesRegex(download.DownloadError, "completed"):
            download.preview_discard(self.discard_running(), job_id)
        self.opened.connection().execute(
            "UPDATE jobs SET outcome = 'cancelled' WHERE job_id = ?", (job_id,))
        with self.assertRaisesRegex(download.DownloadError, "may still write"):
            download.preview_discard(live, job_id)
        self.assertEqual((stage / "model.gguf").read_bytes(), BODY["model.gguf"][:4])
        self.opened.connection().execute(
            "UPDATE jobs SET outcome = 'cancelled' WHERE job_id = ?", (job_id,))
        complete_unit = self.discard_running(state={
            runner.LOAD_STATE: "loaded", runner.ACTIVE_STATE: "inactive",
            runner.SUB_STATE: runner.EXITED, runner.RESULT: "success", runner.EXIT_STATUS: "0"})
        with self.assertRaisesRegex(download.DownloadError, "completed"):
            download.preview_discard(complete_unit, job_id)
        build_id = "download-ffffffffffffffff"
        self.opened.connection().execute(
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at)"
            " VALUES (?, 'build', 'matuto-build.service', 'build', 'now')", (build_id,))
        with self.assertRaisesRegex(download.DownloadError, "no download job"):
            download.preview_discard(self.discard_running(), build_id)

    def test_confirm_refuses_if_a_resume_started_after_preview(self):
        running = self.discard_running()
        job_id, _pinned, stage = self.staged_job()
        preview = download.preview_discard(running, job_id)
        running._unit_state.return_value = {runner.LOAD_STATE: "loaded",
                                            runner.ACTIVE_STATE: "active",
                                            runner.SUB_STATE: "running"}

        with self.assertRaisesRegex(download.DownloadError, "may still write"):
            download.confirm_discard(running, preview)

        self.assertTrue(stage.exists())
        self.assertTrue((stage / "model.gguf").exists())

    def test_discard_checks_every_resume_attempt_and_disables_all_history_rows(self):
        running = self.discard_running()
        job_id, _pinned, stage = self.staged_job()
        resumed_id = "download-fedcba9876543210"
        original_unit = "matuto-download-0123456789abcdef.service"
        resumed_unit = f"matuto-{resumed_id}.service"
        stopped = {runner.LOAD_STATE: runner.NOT_FOUND, runner.ACTIVE_STATE: "inactive",
                   runner.SUB_STATE: "dead", runner.RESULT: "success", runner.EXIT_STATUS: "0"}
        active = {runner.LOAD_STATE: "loaded", runner.ACTIVE_STATE: "active",
                  runner.SUB_STATE: "running"}
        states = {original_unit: stopped, resumed_unit: active}
        running._unit_state.side_effect = states.__getitem__

        def start(request):
            self.insert_job(resumed_id, json.loads(request.pinned_request))
            self.opened.connection().execute("UPDATE jobs SET unit_name = ? WHERE job_id = ?",
                                             (resumed_unit, resumed_id))
            return SimpleNamespace(decision=admission.ADMIT, work=object())

        running.start.side_effect = start
        self.opened.connection().execute(
            "UPDATE jobs SET outcome = 'cancelled' WHERE job_id = ?", (job_id,))
        download.resume(running, job_id, disk_usage=lambda _path: SimpleNamespace(free=1000))

        with self.assertRaisesRegex(download.DownloadError, resumed_id + r" may still write"):
            download.preview_discard(running, job_id)
        self.assertTrue((stage / "model.gguf").exists())

        states[resumed_unit] = stopped
        preview = download.preview_discard(running, job_id)
        download.confirm_discard(running, preview)
        for attempt in (job_id, resumed_id):
            row = self.opened.connection().execute(
                "SELECT phase FROM jobs WHERE job_id = ?", (attempt,)).fetchone()
            self.assertEqual(json.loads(row["phase"]), {"phase": "discarded"})
            with self.assertRaisesRegex(download.DownloadError, "nothing to resume"):
                download.resume(running, attempt)
        self.assertFalse(stage.exists())

    def test_submit_cannot_start_same_staging_during_discard_deletion(self):
        running = self.discard_running()
        job_id, pinned, stage = self.staged_job()
        preview = download.preview_discard(running, job_id)
        original_remove = download._remove_staging
        lock_attempted = threading.Event()
        start_attempted = threading.Event()
        threads, results = [], []

        def submit_same_stage():
            submission_store = store.open_store(self.config)
            submission_running = SimpleNamespace(
                store=submission_store, logs=running.logs,
                serving_state=lambda: admission.STOPPED, start=running.start)
            try:
                download.submit(submission_running, self.resolved,
                                acquisition_id=pinned["acquisition_id"], api_base=self.base,
                                disk_usage=lambda _path: SimpleNamespace(free=1000))
            except download.DownloadError as error:
                results.append(error)
            else:
                results.append("admitted")
            finally:
                submission_store.close()

        def observed_start(_request):
            start_attempted.set()
            return SimpleNamespace(decision=admission.ADMIT, work=object())

        running.start.side_effect = observed_start

        def race_submit_with_delete(root, stage_path, request, entries):
            thread = threading.Thread(target=submit_same_stage)
            threads.append(thread)
            original_flock = download.fcntl.flock

            def observe_flock(descriptor, operation):
                if threading.current_thread() is thread:
                    lock_attempted.set()
                return original_flock(descriptor, operation)

            with mock.patch.object(download.fcntl, "flock", observe_flock):
                thread.start()
                self.assertTrue(lock_attempted.wait(timeout=2))
                self.assertFalse(start_attempted.is_set())
                original_remove(root, stage_path, request, entries)

        with mock.patch.object(download, "_remove_staging", race_submit_with_delete):
            download.confirm_discard(running, preview)
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], download.DownloadError)
        self.assertIn("already owns this staging", str(results[0]))
        running.start.assert_not_called()
        self.assertFalse(stage.exists())

    def test_failed_cancelled_and_interrupted_jobs_can_discard_only_their_own_partials(self):
        running = self.discard_running()
        outside = self.root / "preserve.gguf"
        outside.write_bytes(b"do not remove")
        for index, outcome in enumerate(("failed", "cancelled", "interrupted")):
            with self.subTest(outcome=outcome):
                job_id = f"download-{index + 1:016x}"
                pinned = self.pin(acquisition_id=f"acq-{outcome}")
                job_id, _pinned, stage = self.staged_job(job_id=job_id, pinned=pinned)
                self.opened.connection().execute(
                    "UPDATE jobs SET outcome = ?, ended_at = 'then' WHERE job_id = ?",
                    (outcome, job_id))
                preview = download.preview_discard(running, job_id)
                self.assertEqual(preview.files, (("model.gguf", 4), ("tokenizer.json", 4)))
                download.confirm_discard(running, preview)
                self.assertFalse(stage.exists())
                self.assertEqual(outside.read_bytes(), b"do not remove")

    def test_confirm_rechecks_and_returns_fresh_preview_when_staging_changes(self):
        running = self.discard_running()
        job_id, _pinned, stage = self.staged_job()
        preview = download.preview_discard(running, job_id)
        (stage / "model.gguf").write_bytes(b"changed-size")

        with self.assertRaises(download.DiscardChanged) as caught:
            download.confirm_discard(running, preview)

        self.assertEqual(caught.exception.preview.files[0], ("model.gguf", 12))
        self.assertEqual((stage / "model.gguf").read_bytes(), b"changed-size")
        self.assertTrue((stage / "tokenizer.json").exists())

    def test_discard_refuses_links_unrecorded_entries_and_acquisition_staging(self):
        running = self.discard_running()
        job_id, pinned, stage = self.staged_job()
        outside = self.root / "untouched.gguf"
        outside.write_bytes(b"preserve")
        (stage / "linked.gguf").symlink_to(outside)
        with self.assertRaisesRegex(download.DownloadError, "unsafe or unrecorded"):
            download.preview_discard(running, job_id)
        (stage / "linked.gguf").unlink()
        (stage / "unrecorded").write_bytes(b"extra")
        with self.assertRaisesRegex(download.DownloadError, "unsafe or unrecorded"):
            download.preview_discard(running, job_id)
        original_lstat = Path.lstat
        target = stage / "model.gguf"

        def foreign_owner(path):
            status = original_lstat(path)
            if path == target:
                return SimpleNamespace(st_mode=status.st_mode, st_uid=status.st_uid + 1,
                                       st_size=status.st_size)
            return status

        with mock.patch.object(Path, "lstat", foreign_owner):
            with self.assertRaisesRegex(download.DownloadError, "unsafe or unrecorded"):
                download.preview_discard(running, job_id)
        (stage / "unrecorded").unlink()
        location_id = "loc-discard-test"
        self.opened.connection().execute(
            "INSERT INTO artifact_locations (location_id, path, notes, created_at)"
            " VALUES (?, ?, '', 'now')", (location_id, str(stage.parent)))
        self.opened.connection().execute(
            "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
            " location_id, created_at) VALUES ('acq-hold', 'held', 'download', 'locally-hashed', ?, 'now')",
            (location_id,))
        self.opened.connection().execute(
            "INSERT INTO acquisition_files (acquisition_id, file_name, size_bytes, computed_sha256)"
            " VALUES ('acq-hold', ?, 4, ?)",
            (f"{stage.name}/model.gguf", hashlib.sha256(BODY["model.gguf"][:4]).hexdigest()))
        with self.assertRaisesRegex(download.DownloadError, "named by an acquisition record"):
            download.preview_discard(running, job_id)
        self.assertEqual(outside.read_bytes(), b"preserve")
        self.assertTrue((stage / "model.gguf").exists())

    def test_discard_protects_file_recorded_from_a_nested_acquisition_location(self):
        running = self.discard_running()
        source = self.files[0]
        nested = acquisition.File(
            "nested/model.gguf", source.size, source.sha256, source.checksum_status,
            source.url.replace("/model.gguf", "/nested/model.gguf"))
        resolved = acquisition.Resolution("owner/model", COMMIT, (nested,), nested.size)
        pinned = download._pin(resolved, str(self.root), False, "acq-nested-job",
                               origin_id=self.origin_id, api_base=self.base)
        job_id = self.insert_job("download-1111111111111111", pinned)
        stage = download._stage_path(self.root, pinned)
        (stage / "nested").mkdir(parents=True)
        (stage / nested.name).write_bytes(BODY["model.gguf"][:4])
        location_id = "loc-nested-record"
        self.opened.connection().execute(
            "INSERT INTO artifact_locations (location_id, path, notes, created_at)"
            " VALUES (?, ?, '', 'now')", (location_id, str(stage / "nested")))
        self.opened.connection().execute(
            "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
            " location_id, created_at) VALUES ('acq-nested-record', 'held', 'download',"
            " 'locally-hashed', ?, 'now')", (location_id,))
        self.opened.connection().execute(
            "INSERT INTO acquisition_files (acquisition_id, file_name, size_bytes, computed_sha256)"
            " VALUES ('acq-nested-record', 'model.gguf', 4, ?)",
            (hashlib.sha256(BODY["model.gguf"][:4]).hexdigest(),))

        with self.assertRaisesRegex(download.DownloadError, "named by an acquisition record"):
            download.preview_discard(running, job_id)
        self.assertEqual((stage / nested.name).read_bytes(), BODY["model.gguf"][:4])

    def test_discard_runs_through_runner_reconciliation_before_cleanup(self):
        job_id, _pinned, stage = self.staged_job()
        running = runner.Runner(self.config, opened_store=self.opened,
                                serving_state=lambda: admission.STOPPED)
        running._prepare(job_id)
        stopped = {runner.LOAD_STATE: runner.NOT_FOUND, runner.ACTIVE_STATE: "inactive",
                   runner.SUB_STATE: "dead", runner.RESULT: "success", runner.EXIT_STATUS: "0"}
        with (mock.patch.object(running, "_unit_state", return_value=stopped),
              mock.patch.object(running, "_launching", return_value=False)):
            preview = download.preview_discard(running, job_id)
            self.assertEqual(running.work(job_id).outcome, runner.INTERRUPTED)
            download.confirm_discard(running, preview)
        self.assertFalse(stage.exists())
        self.assertEqual(running.work(job_id).outcome, "discarded")

    def test_confirm_discards_only_the_staging_and_marks_history_non_resumable(self):
        running = self.discard_running()
        job_id, pinned, stage = self.staged_job()
        outside = self.root / "keep.bin"
        outside.write_bytes(b"valuable")
        adoption.adopt(self.opened.connection(), "acq-kept", "Existing weights", outside,
                       consent=True)
        completed = self.root / "completed-model"
        completed.mkdir()
        completed_file = completed / "model.gguf"
        completed_file.write_bytes(b"completed weights")
        self.opened.connection().execute(
            "INSERT INTO artifact_locations (location_id, path, notes, created_at)"
            " VALUES ('loc-completed', ?, '', 'now')", (str(completed),))
        self.opened.connection().execute(
            "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
            " origin_id, repository, revision, location_id, verified_at, created_at)"
            " VALUES ('acq-completed', 'Complete', 'download', 'published-checksum', ?,"
            " 'owner/model', ?, 'loc-completed', 'then', 'now')",
            (self.origin_id, COMMIT))
        self.opened.connection().execute(
            "INSERT INTO acquisition_files (acquisition_id, file_name, size_bytes,"
            " published_sha256, computed_sha256, verified_at) VALUES ('acq-completed',"
            " 'model.gguf', ?, ?, ?, 'then')",
            (completed_file.stat().st_size, hashlib.sha256(completed_file.read_bytes()).hexdigest(),
             hashlib.sha256(completed_file.read_bytes()).hexdigest()))
        preview = download.preview_discard(running, job_id)

        discarded = download.confirm_discard(running, preview)

        row = self.opened.connection().execute(
            "SELECT outcome, ended_at, phase FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        self.assertEqual(discarded, preview)
        self.assertEqual(row["outcome"], "interrupted")
        self.assertTrue(row["ended_at"])
        self.assertEqual(json.loads(row["phase"]), {"phase": "discarded"})
        self.assertEqual(runner._work(self.opened.connection(), job_id).outcome, "discarded")
        self.assertFalse(stage.exists())
        self.assertEqual(outside.read_bytes(), b"valuable")
        self.assertEqual(completed_file.read_bytes(), b"completed weights")
        with self.assertRaisesRegex(download.DownloadError, "nothing to resume"):
            download.resume(running, job_id)
        self.assertEqual(self.opened.connection().execute(
            "SELECT pinned_request FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0],
            json.dumps(pinned, sort_keys=True, separators=(",", ":")))

    def test_interrupted_discard_can_be_retried_without_touching_other_files(self):
        running = self.discard_running()
        job_id, _pinned, stage = self.staged_job()
        outside = self.root / "keep.bin"
        outside.write_bytes(b"still here")
        original_unlink = os.unlink
        calls = []

        def interrupt_second(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("simulated interruption")
            return original_unlink(path, *args, **kwargs)

        preview = download.preview_discard(running, job_id)
        with mock.patch.object(os, "unlink", interrupt_second):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                download.confirm_discard(running, preview)
        self.assertTrue(stage.exists())
        self.assertEqual(outside.read_bytes(), b"still here")

        remainder = download.preview_discard(running, job_id)
        download.confirm_discard(running, remainder)

        self.assertFalse(stage.exists())
        self.assertEqual(outside.read_bytes(), b"still here")

    def test_discard_does_not_follow_a_staging_directory_swapped_to_a_link(self):
        running = self.discard_running()
        job_id, _pinned, stage = self.staged_job()
        outside = self.home / "outside"
        outside.mkdir()
        protected = outside / "model.gguf"
        protected.write_bytes(b"must remain")
        preview = download.preview_discard(running, job_id)
        original_open = os.open

        def swap_stage(name, flags, *args, **kwargs):
            if name == stage.name and kwargs.get("dir_fd") is not None:
                saved = stage.with_name(stage.name + ".saved")
                stage.rename(saved)
                stage.symlink_to(outside, target_is_directory=True)
            return original_open(name, flags, *args, **kwargs)

        with mock.patch.object(os, "open", swap_stage):
            with self.assertRaises(OSError):
                download.confirm_discard(running, preview)

        self.assertEqual(protected.read_bytes(), b"must remain")
        self.assertTrue(stage.is_symlink())

    def test_discard_confirmation_refreshes_after_interruption_left_empty_directories(self):
        running = self.discard_running()
        job_id, _pinned, stage = self.staged_job()
        preview = download.preview_discard(running, job_id)
        with mock.patch.object(os, "rmdir", side_effect=OSError("interrupted after unlink")):
            with self.assertRaisesRegex(OSError, "interrupted after unlink"):
                download.confirm_discard(running, preview)
        self.assertTrue(stage.is_dir())

        with self.assertRaises(download.DiscardChanged) as caught:
            download.confirm_discard(running, preview)
        self.assertEqual(caught.exception.preview.files, ())
        download.confirm_discard(running, caught.exception.preview)

        self.assertFalse(stage.exists())
        self.assertEqual(runner._work(self.opened.connection(), job_id).outcome, "discarded")

    def test_discard_recovers_empty_staging_after_a_process_restart(self):
        running = self.discard_running()
        job_id, _pinned, stage = self.staged_job()
        preview = download.preview_discard(running, job_id)
        with mock.patch.object(os, "rmdir", side_effect=OSError("interrupted after unlink")):
            with self.assertRaisesRegex(OSError, "interrupted after unlink"):
                download.confirm_discard(running, preview)
        self.assertTrue(stage.is_dir())
        phase = self.opened.connection().execute(
            "SELECT phase FROM jobs WHERE job_id = ?", (job_id,)).fetchone()["phase"]
        self.assertEqual(json.loads(phase), {"phase": "discarding"})
        with self.assertRaisesRegex(download.DownloadError, "nothing to resume"):
            download.resume(running, job_id)

        restarted = self.discard_running()
        empty_preview = download.preview_discard(restarted, job_id)
        self.assertEqual(empty_preview.files, ())
        download.confirm_discard(restarted, empty_preview)

        self.assertFalse(stage.exists())
        self.assertEqual(runner._work(self.opened.connection(), job_id).outcome, "discarded")

    def test_resume_submits_the_original_pinned_request_as_a_new_runner_attempt(self):
        pinned = self.pin()
        previous = self.insert_job(pinned=pinned)
        running = SimpleNamespace(store=self.opened, logs=self.home / "runner-work",
                                  serving_state=lambda: admission.STOPPED,
                                  start=mock.Mock(return_value=SimpleNamespace(
                                      decision=admission.ADMIT, work=object())))

        submitted = download.resume(running, previous,
                                    disk_usage=lambda _path: SimpleNamespace(free=1000))

        self.assertEqual(submitted.start.decision, admission.ADMIT)
        request = running.start.call_args.args[0]
        self.assertEqual(json.loads(request.pinned_request), pinned)
        self.assertEqual(request.destination, str(self.root))

    def test_submission_uses_a_runner_download_request_pinned_to_revision(self):
        running = SimpleNamespace(store=self.opened, logs=self.home / "runner-work",
                                  serving_state=lambda: admission.VOICE,
                                  start=mock.Mock(return_value=SimpleNamespace(
                                      decision=admission.ADMIT, work=object())))
        submitted = download.submit(running, self.resolved, api_base=self.base,
                                    disk_usage=lambda _path: SimpleNamespace(free=1000))
        request = running.start.call_args.args[0]
        pinned = json.loads(request.pinned_request)
        self.assertEqual(request.kind, admission.DOWNLOAD)
        self.assertEqual(pinned["revision"], COMMIT)
        self.assertTrue(all("authorization" not in str(item).lower() for item in pinned["files"]))
        self.assertIn("may slow it", submitted.warning)

    def test_runner_starts_the_job_and_records_completion_from_its_transient_unit(self):
        systemd = self.home / "systemd"
        systemd.mkdir()
        binaries = self.home / "bin"
        binaries.mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("systemd-run", FAKE_SYSTEMD_RUN)):
            executable = binaries / name
            executable.write_text(body)
            executable.chmod(0o700)
        (systemd / "run-command").touch()
        env = mock.patch.dict(os.environ, {
            "RUNNER_SYSTEMD": str(systemd), "RUNNER_STORE": str(store.store_path(self.config)),
            "XDG_CONFIG_HOME": str(self.config),
            "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
        })
        with env:
            running = runner.Runner(self.config)
            try:
                submitted = download.submit(running, self.resolved, api_base=self.base)
                self.assertEqual(submitted.start.decision, admission.ADMIT)
                finished = running.works()[0]
            finally:
                running.close()
        self.assertEqual(finished.kind, admission.DOWNLOAD)
        self.assertEqual(finished.outcome, runner.COMPLETED)
        record = self.opened.connection().execute(
            "SELECT verified_at FROM acquisition_records WHERE acquisition_id = ?",
            (json.loads(self.opened.connection().execute(
                "SELECT pinned_request FROM jobs WHERE job_id = ?", (finished.work_id,)
            ).fetchone()[0])["acquisition_id"],)).fetchone()
        self.assertTrue(record["verified_at"])

    def test_unresolved_or_untrusted_transfer_addresses_are_refused(self):
        malicious = acquisition.Resolution("owner/model", COMMIT, (
            acquisition.File("model.gguf", 1, None, "none",
                             "https://attacker.example/model"),), 1)
        with self.assertRaisesRegex(download.DownloadError, "not pinned to Hugging Face"):
            download._pin(malicious, str(self.root), False, "acq-bad",
                          origin_id=self.origin_id)
        with self.assertRaisesRegex(download.DownloadError, "not pinned to Hugging Face"):
            download._pin(self.resolved, str(self.root), False, "acq-bad",
                          origin_id=self.origin_id, api_base="http://attacker.example")


if __name__ == "__main__":
    unittest.main()
