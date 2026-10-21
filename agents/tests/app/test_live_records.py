"""One refusal rule for changes to live records, over a fake systemctl and a fake router.

Every state the rule reads is a real serving state answered by the fakes: the
router serving a published profile, voice serving a bundle and its members,
nothing serving, and a systemd that reports no unit at all. Each refusal is
checked for the record it names and for Overview's Stop serving, and the store,
the generated router preset and the fake systemctl's own log are checked for what
a refusal leaves behind.
"""

import contextlib
import json
import os
import shutil
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from agents.tests.app.test_record_validation import RecordTestCase

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import admission, live_records, serving_state  # noqa: E402
from matuto import store as store_module  # noqa: E402

# The real reader, kept before any test replaces it with a settled answer.
READ_STATE = live_records.read_state

ROUTER = serving_state.ROUTER_UNIT
VOICE_ROUTER = serving_state.VOICE_ROUTER_UNIT
SYNTHESIS_UNIT = serving_state.part_unit("prof-breeze")
RECOGNITION_UNIT = serving_state.part_unit("prof-whisper")
KEY = "t" * 40
PORTS = {"synthesis": 8810, "recognition": 8811}
OTHER_GPU = "0000:04:00.0"
FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
"""A fake systemctl: answers per unit from a file and logs every call."""
import json
import os
import sys
from pathlib import Path

with Path(os.environ["FAKE_SYSTEMCTL_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
answers = json.loads(Path(os.environ["FAKE_SYSTEMCTL_UNITS"]).read_text(encoding="utf-8"))
if "list-units" in sys.argv:
    sys.stdout.write("".join(f"{unit} loaded active running\\n"
                              for unit in answers.get("*", "").split()))
else:
    sys.stdout.write(answers.get(sys.argv[-1], ""))
'''


class RouterHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.answer(b'{"status": "ok"}')
        elif self.path == "/models":
            self.answer(json.dumps(
                {"data": [{"id": "qwen3-coder", "status": {"value": "loaded"}}]}).encode())
        else:
            self.send_error(404)

    def answer(self, body):
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *arguments):
        pass


class LiveRecordTest(RecordTestCase):
    """The store's own fixtures, plus a serving state the rule can read."""

    def setUp(self):
        super().setUp()
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.units_file = self.home / "units.json"
        self.log = self.home / "systemctl.log"
        self.log.write_text("", encoding="utf-8")
        (self.bin / "systemctl").write_text(FAKE_SYSTEMCTL, encoding="utf-8")
        os.chmod(self.bin / "systemctl", 0o755)
        (self.home / "systemd" / "user").mkdir(parents=True)
        self.key = self.home / store_module.CONFIG_ROOT_NAME / "pi-local" / "api-key"
        self.key.parent.mkdir(parents=True, mode=0o700)
        self.key.write_text(f"{KEY}\n", encoding="utf-8")
        os.chmod(self.key, 0o600)
        for name, value in (("PATH", f"{self.bin}{os.pathsep}{os.environ['PATH']}"),
                            ("XDG_CONFIG_HOME", str(self.home)),
                            ("FAKE_SYSTEMCTL_LOG", str(self.log)),
                            ("FAKE_SYSTEMCTL_UNITS", str(self.units_file))):
            patch = mock.patch.dict(os.environ, {name: value})
            patch.start()
            self.addCleanup(patch.stop)
        self.stop_serving()
        # The base class settles the serving state; this suite reads the real one.
        live = mock.patch.object(live_records, "read_state", READ_STATE)
        live.start()
        self.addCleanup(live.stop)
        self.router = ThreadingHTTPServer(("127.0.0.1", 0), RouterHandler)
        port = mock.patch.object(serving_state, "ROUTER_PORT", self.router.server_port)
        port.start()
        self.addCleanup(port.stop)
        # read's default port was bound at definition time; pass the patched port.
        reader = serving_state.read
        reading = mock.patch.object(serving_state, "read", side_effect=lambda **kwargs: reader(
            **{"router_port": serving_state.ROUTER_PORT, **kwargs}))
        reading.start()
        self.addCleanup(reading.stop)
        thread = threading.Thread(target=self.router.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.router.server_close)
        self.addCleanup(self.router.shutdown)
        self.insert_language_profile()
        self.save(self.bundle(), help_text=self.help_text())
        self.connection.execute(
            "UPDATE model_profiles SET published = 1 WHERE profile_id = 'prof-coder'")
        self.connection.commit()
        store_module.regenerate_router_files(self.store)

    def units(self, **answers):
        (self.units_file).write_text(json.dumps(answers), encoding="utf-8")

    def stop_serving(self):
        self.units(**{ROUTER: "inactive", VOICE_ROUTER: "inactive", "*": ""})

    def serve_agentic(self):
        self.units(**{ROUTER: "active", VOICE_ROUTER: "inactive", "*": ""})

    def serve_voice(self):
        self.units(**{ROUTER: "inactive", VOICE_ROUTER: "active", SYNTHESIS_UNIT: "active",
                      RECOGNITION_UNIT: "active", "*": ""})
        self.connection.executescript(
            f"""
            INSERT INTO voice_serving VALUES (1, 'bundle-voice');
            INSERT INTO voice_serving_parts VALUES ('synthesis', 'bundle-voice', {PORTS['synthesis']});
            INSERT INTO voice_serving_parts VALUES ('recognition', 'bundle-voice', {PORTS['recognition']});
            """)
        self.connection.commit()

    def report_nothing(self):
        """A systemd that answers no unit, which the reader never calls stopped."""
        self.units()

    def save(self, record, **kwargs):
        return store_module.save_record(self.store, record, **kwargs)

    def preset(self):
        path = self.store.path.parent / "router-presets.ini"
        return path.read_text(encoding="utf-8")

    def stored(self, kind, identifier):
        return store_module.read_card(self.store, identifier) if kind == "card" \
            else store_module.read_record(self.store, kind, identifier)

    def assertRefusedLive(self, kind, identifier, record, **kwargs):
        """The save is refused naming the record and Stop serving, and nothing moves."""
        before = self.preset()
        with self.assertRaises(store_module.RecordRefusal) as raised:
            self.save(record, record_id=identifier, **kwargs)
        message = str(raised.exception)
        self.assertIn(self.stored(kind, identifier)["name"], message)
        self.assertIn("Stop serving on Overview", message)
        self.assertNotIn("Stop voice", message)
        self.assertEqual(self.preset(), before)
        return message

    def assertNoLifecycleCall(self):
        """The fake systemctl was only ever asked what is running, never told to run it."""
        calls = [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]
        self.assertTrue(calls, "the rule read no unit state at all")
        lifecycle = {"start", "stop", "restart", "reload", "enable", "disable", "mask", "kill"}
        for call in calls:
            self.assertIn("--user", call.split(), call)
            self.assertFalse(lifecycle.intersection(call.split()), call)

    def test_router_serving_refuses_a_published_profile_save_and_changes_nothing(self):
        self.serve_agentic()
        self.assertEqual(READ_STATE(self.store).state, admission.AGENTIC)
        record = self.stored("language", "prof-coder")

        for changed, description in ((dict(record, context_size=262144), "a served value"),
                                     (dict(record, notes="only the notes"), "notes")):
            with self.subTest(change=description):
                self.assertRefusedLive("language", "prof-coder", changed,
                                       help_text=self.help_text())
                self.assertEqual(self.stored("language", "prof-coder"), record)

        # A save that changes nothing is not a change, so it is not refused.
        saved, _ = self.save(record, record_id="prof-coder", help_text=self.help_text())
        self.assertEqual(saved["context_size"], record["context_size"])
        self.assertNoLifecycleCall()

    def test_router_serving_saves_an_unpublished_profile_outside_the_voice_bundle(self):
        self.serve_agentic()
        candidate = dict(self.language(), profile_id="prof-candidate", name="Unpublished coder",
                         alias="unpublished-coder", notes="")

        saved, _ = self.save(candidate, help_text=self.help_text())

        self.assertEqual((saved["profile_id"], saved["published"]), ("prof-candidate", False))
        self.assertNotIn("prof-candidate", self.preset())

    def test_router_serving_saves_a_bundle_whose_id_matches_a_published_profile(self):
        # Bundle and profile IDs are separate namespaces. The router never serves a
        # bundle, so a non-live bundle sharing a published profile's ID is not live.
        self.serve_agentic()
        shared = dict(self.bundle(), bundle_id="prof-coder", name="Separate bundle")
        self.save(shared, help_text=self.help_text())

        saved, _ = self.save(dict(shared, notes="edited"), record_id="prof-coder",
                             help_text=self.help_text())
        self.assertEqual(saved["notes"], "edited")

        store_module.remove_record(self.store, "bundle", "prof-coder")
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM bundles WHERE bundle_id = 'prof-coder'").fetchone())
        self.assertTrue(self.stored("language", "prof-coder")["published"])

    def test_router_serving_refuses_an_import_that_edits_a_published_profile(self):
        self.serve_agentic()
        published = {key: value for key, value in self.stored("language", "prof-coder").items()
                     if key not in ("created_at", "published", "qualification")}
        document = {"version": store_module.DOCUMENT_VERSION, "records": [
            dict(published, context_size=262144),
            dict(self.language(), profile_id="prof-fresh", name="Fresh coder",
                 alias="fresh-coder"),
        ]}

        with self.assertRaises(store_module.RecordRefusal) as raised:
            store_module.import_records(self.store, document, help_text=self.help_text())

        message = str(raised.exception)
        self.assertIn("Qwen3.6 Coder 192K", message)
        self.assertIn("Stop serving on Overview", message)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM model_profiles WHERE profile_id = 'prof-fresh'").fetchone())
        self.assertEqual(self.stored("language", "prof-coder")["context_size"], 131072)

    def test_router_serving_refuses_a_card_a_published_profile_points_to(self):
        self.serve_agentic()
        card = self.stored("card", "card-r9")
        detected = {"state": "completed", "devices": [
            {"pciAddress": identity, "vramBytes": 34359738368}
            for identity in (card["gpu_identity"], OTHER_GPU)]}

        for changed, description in ((dict(card, gpu_identity=OTHER_GPU), "its physical GPU"),
                                     (dict(card, vram_ceiling_bytes=16000000000),
                                      "its VRAM ceiling")):
            with self.subTest(change=description):
                before = self.preset()
                with mock.patch("matuto.cards.detect", return_value=detected):
                    with self.assertRaises(store_module.RecordRefusal) as raised:
                        self.save(changed, record_id="card-r9")
                message = str(raised.exception)
                self.assertIn("Radeon AI PRO R9700", message)
                self.assertIn("Stop serving on Overview", message)
                self.assertEqual(self.stored("card", "card-r9"), card)
                self.assertEqual(self.preset(), before)

        # A card a published profile points to is refused on removal too, before the
        # store's own reference rule can answer it in its own words.
        before = self.preset()
        with self.assertRaises(store_module.RecordRefusal) as raised:
            store_module.remove_record(self.store, "card", "card-r9")
        self.assertIn("Stop serving on Overview", str(raised.exception))
        self.assertEqual(self.stored("card", "card-r9"), card)
        self.assertEqual(self.preset(), before)

    def test_voice_serving_refuses_its_bundle_its_members_and_its_removal(self):
        self.serve_voice()
        self.assertEqual(READ_STATE(self.store).state, admission.VOICE)
        refused = (
            ("bundle", "bundle-voice", dict(self.stored("bundle", "bundle-voice"), notes="edited")),
            ("synthesis", "prof-breeze", dict(self.stored("synthesis", "prof-breeze"),
                                              notes="edited")),
            ("language", "prof-coder", dict(self.stored("language", "prof-coder"),
                                            context_size=262144)),
        )
        for kind, identifier, candidate in refused:
            with self.subTest(record=identifier):
                before = self.stored(kind, identifier)
                message = self.assertRefusedLive(kind, identifier, candidate,
                                                 help_text=self.help_text())
                self.assertIn("voice serving runs it", message)
                self.assertEqual(self.stored(kind, identifier), before)

        before = self.preset()
        with self.assertRaises(store_module.RecordRefusal) as raised:
            store_module.remove_record(self.store, "bundle", "bundle-voice")
        message = str(raised.exception)
        self.assertIn("Voice 192K", message)
        self.assertIn("Stop serving on Overview", message)
        self.assertEqual(self.stored("bundle", "bundle-voice")["bundle_id"], "bundle-voice")
        self.assertEqual(self.preset(), before)
        self.assertNoLifecycleCall()

    def test_voice_serving_saves_a_record_the_running_bundle_does_not_reach(self):
        self.serve_voice()
        # A recognition profile no bundle names is outside what the running bundle
        # reaches, so voice serving does not hold it.
        candidate = dict(self.recognition(), profile_id="prof-spare", name="Spare recognizer")
        self.save(candidate, help_text=self.help_text())

        saved, _ = self.save(dict(candidate, notes="edited"), record_id="prof-spare",
                             help_text=self.help_text())

        self.assertEqual((saved["profile_id"], saved["kind"], saved["notes"]),
                         ("prof-spare", "recognition", "edited"))

    def test_duplicating_a_live_profile_leaves_it_unchanged(self):
        self.serve_agentic()
        before = self.stored("language", "prof-coder")

        copy, _ = store_module.duplicate_record(self.store, "language", "prof-coder",
                                                "prof-coder-copy", "Qwen3.6 Coder 192K (copy)",
                                                alias="qwen3-coder-copy")

        self.assertEqual((copy["profile_id"], copy["alias"], copy["published"]),
                         ("prof-coder-copy", "qwen3-coder-copy", False))
        self.assertEqual(self.stored("language", "prof-coder"), before)

    def test_serving_stopped_saves_a_published_profile_and_regenerates_its_preset(self):
        self.stop_serving()
        self.assertEqual(READ_STATE(self.store).state, admission.STOPPED)
        before = self.preset()

        saved, _ = self.save(dict(self.stored("language", "prof-coder"), context_size=262144),
                             record_id="prof-coder", help_text=self.help_text())

        self.assertTrue(saved["published"])
        self.assertEqual(store_module.qualification_state(self.store, saved)["state"], "candidate")
        self.assertNotEqual(self.preset(), before)
        self.assertIn("ctx-size = 262144", self.preset())

    def test_serving_started_after_the_record_was_read_still_refuses_the_save(self):
        self.stop_serving()
        self.assertEqual(READ_STATE(self.store).state, admission.STOPPED)
        record = self.stored("language", "prof-coder")
        self.serve_agentic()

        self.assertRefusedLive("language", "prof-coder", dict(record, context_size=262144),
                               help_text=self.help_text())

    def test_an_unknown_serving_state_refuses_what_a_readable_state_would_hold(self):
        self.report_nothing()
        self.assertEqual(READ_STATE(self.store).state, admission.SERVING_UNKNOWN)
        detected = {"state": "completed", "devices": [
            {"pciAddress": card["gpu_identity"], "vramBytes": card["vram_ceiling_bytes"]}
            for card in (self.stored("card", "card-r9"),)]}
        cases = (
            ("prof-coder", dict(self.stored("language", "prof-coder"), context_size=262144),
             "Qwen3.6 Coder 192K", {}),
            ("prof-whisper", dict(self.stored("recognition", "prof-whisper"), notes="edited"),
             "Whisper CPU", {}),
            ("bundle-voice", dict(self.stored("bundle", "bundle-voice"), notes="edited"),
             "Voice 192K", {}),
            ("card-r9", dict(self.stored("card", "card-r9"), vram_ceiling_bytes=16000000000),
             "Radeon AI PRO R9700", {"detect": detected}),
        )
        for identifier, candidate, name, extra in cases:
            with self.subTest(record=identifier):
                before = self.preset()
                with mock.patch("matuto.cards.detect", return_value=extra["detect"]) if extra else \
                        contextlib.nullcontext():
                    with self.assertRaises(store_module.RecordRefusal) as raised:
                        self.save(candidate, record_id=identifier,
                                  help_text=self.help_text())
                message = str(raised.exception)
                self.assertIn(name, message)
                self.assertIn("the serving state is unknown", message)
                self.assertIn("Stop serving on Overview", message)
                self.assertEqual(self.preset(), before)


if __name__ == "__main__":
    unittest.main()