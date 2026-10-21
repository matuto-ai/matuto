"""Speech profile management through the protected manager HTTP interface."""

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from agents.tests.app.test_record_validation import RecordTestCase

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import admission, login, sessions, store  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server  # noqa: E402

GPU = "0000:03:00.0"


class SpeechProfilesHttpTest(RecordTestCase):
    def setUp(self):
        super().setUp()
        self.insert_language_profile()
        self.save_bundle = dict(self.bundle(), load_tokens=128, load_prompt="hello")
        store.save_record(self.store, self.save_bundle)
        store.save_record(self.store, dict(self.save_bundle, bundle_id="bundle-voice-2", name="Second voice"))
        fingerprint = store.fingerprint(self.store, self.save_bundle)
        self.connection.execute(
            "INSERT INTO qualification_records (qualification_id, record_kind, record_id, record_label,"
            " fingerprint, created_at) VALUES ('qual-voice', 'bundle', 'bundle-voice', 'Voice', ?, ?) ",
            (fingerprint, "2026-10-10T00:00:00+00:00"))
        self.connection.execute(
            "INSERT INTO acceptances (acceptance_id, qualification_id, scope, accepted_by, accepted_at)"
            " VALUES ('accept-voice', 'qual-voice', 'fixture', 'operator', '2026-10-10T00:00:00+00:00')")
        provenance = {"project": "Breeze-TTS-2.cpp", "backend": "vulkan", "commit": "b" * 40}
        self.connection.execute("UPDATE speech_executables SET notes=? WHERE executable_id='exe-breeze'",
                                (json.dumps(provenance),))
        second = self.executable("breeze-second")
        second.write_text("#!/bin/sh\n# second checked fixture\n", encoding="utf-8")
        identical = self.executable("breeze-identical")
        original_executable = Path(self.connection.execute(
            "SELECT path FROM speech_executables WHERE executable_id='exe-breeze'").fetchone()[0])
        identical.write_bytes(original_executable.read_bytes())
        self.connection.execute(
            "INSERT INTO speech_executables VALUES ('exe-breeze-second','synthesis','loc-models', ?, ?,"
            " '1.0', ?, ?)", (str(second), store._digest_of(second), json.dumps(dict(provenance, commit="c" * 40)), "2026-10-10T00:00:00+00:00"))
        self.connection.execute(
            "INSERT INTO speech_executables VALUES ('exe-breeze-identical','synthesis','loc-models', ?, ?,"
            " '1.0', ?, ?)", (str(identical), store._digest_of(identical), json.dumps(dict(provenance, commit="d" * 40)), "2026-10-10T00:00:00+00:00"))
        self.connection.commit()

        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        self.env.start()
        self.addCleanup(self.env.stop)
        login.create_login("operator", "secret", "secret")
        self.token, _expiry = sessions.start_session("operator", "secret", config_root=self.config)
        self.server = build_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf
        self.opener = urllib.request.build_opener()
        self.gpus = {"state": "completed", "devices": [{"pciAddress": GPU, "vramBytes": 34359738368}]}

    def _request(self, body=None, *, path="/api/speech-profiles", token=True, csrf=True):
        headers = {"Host": ALLOWED_HOST}
        if token:
            headers["Cookie"] = f"{SESSION_COOKIE}={self.token}"
        method = "POST" if body is not None else "GET"
        if body is not None:
            headers["Content-Type"] = "application/json"
            if csrf:
                headers.update({"Origin": MANAGER_ORIGIN, "X-CSRF-Token": self.csrf})
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.address + path, data=data, method=method, headers=headers)
        try:
            response = self.opener.open(request, timeout=5)
            return response.status, json.loads(response.read()) if "json" in response.headers.get("Content-Type", "") else response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw) if raw else {}

    def synthesis(self, profile_id="new-speech"):
        return {"kind": "synthesis", "profile_id": profile_id, "name": "New Breeze",
                "speech_executable_id": "exe-breeze", "weights": dict(self.synthesis_record()["weights"]),
                "card_id": "card-r9", "arguments": [], "notes": "fixture"}

    def synthesis_record(self):
        return store.read_record(self.store, "synthesis", "prof-breeze")

    def _post(self, action, **fields):
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            return self._request({"action": action, **fields})

    def test_list_and_create_show_only_approved_picks_and_no_profile_state(self):
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            status, listed = self._request()
        self.assertEqual(status, 200)
        by_id = {item["id"]: item for item in listed["profiles"]}
        self.assertEqual(by_id["prof-breeze"]["kind"], "synthesis")
        self.assertEqual({bundle["name"] for bundle in by_id["prof-breeze"]["bundles"]},
                         {"Voice 192K", "Second voice"})
        self.assertNotIn("qualification", by_id["prof-breeze"])
        self.assertNotIn("card", by_id["prof-whisper"])
        self.assertNotIn("card_id", by_id["prof-whisper"])
        self.assertIn("Breeze-TTS-2.cpp — vulkan", by_id["prof-breeze"]["executable"])
        self.assertEqual({item["kind"] for item in listed["executables"]}, {"synthesis", "recognition"})
        self.assertIn("breeze-tts-2-q8_0.gguf", [item["file_name"] for item in listed["weights"]])

        for record in (self.synthesis(), self.recognition_new()):
            status, created = self._post("save", mode="create", record=record)
            self.assertEqual(status, 201, created)
            self.assertEqual(created["profile"]["id"], record["profile_id"])

    def recognition_new(self):
        record = self.recognition()
        record.update(profile_id="new-recognition", name="New Whisper")
        return record

    def test_invalid_kind_card_executable_and_unverified_picks_refuse_without_writing(self):
        cases = []
        recognition_with_card = self.recognition_new(); recognition_with_card["card_id"] = "card-r9"
        cases.append((recognition_with_card, "card_id"))
        wrong_executable = self.synthesis(); wrong_executable["speech_executable_id"] = "exe-whisper"
        cases.append((wrong_executable, "speech_executable_id"))
        path = self.synthesis(); path["path"] = "/tmp/weights.gguf"
        cases.append((path, "path"))
        digest = self.synthesis(); digest["weights"] = {"path": "/tmp/weights.gguf", "sha256": "a" * 64}
        cases.append((digest, "weights"))
        self.connection.execute(
            "INSERT INTO acquisition_records VALUES ('acq-incomplete', 'Incomplete', 'adoption',"
            " 'locally-hashed', NULL, NULL, NULL, 'loc-models', NULL, '', ?)",
            ("2026-10-10T00:00:00+00:00",))
        self.connection.execute(
            "INSERT INTO acquisition_files VALUES ('acq-incomplete', 'partial.gguf', 0, NULL, '', NULL)")
        incomplete = self.synthesis(); incomplete["weights"] = {
            "acquisition_id": "acq-incomplete", "file_name": "partial.gguf"}
        cases.append((incomplete, "incomplete acquisition record"))
        before = self.connection.total_changes
        for record, field in cases:
            with self.subTest(field=field):
                status, refusal = self._post("save", mode="create", record=record)
                self.assertEqual(status, 409)
                self.assertIn(field, refusal["error"])
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM model_profiles WHERE profile_id LIKE 'new-%'").fetchone()[0], 0)
        self.assertEqual(self.connection.total_changes, before)

    def test_served_change_is_reviewed_then_saves_and_restores_bundle_qualification(self):
        edited = self.synthesis_record()
        edited["speech_executable_id"] = "exe-breeze-second"
        status, preview = self._post("preview", record=edited)
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["preview"]["kind"], "served-change")
        self.assertEqual(preview["preview"]["state"], None)
        self.assertEqual(preview["preview"]["affected"][0]["state"]["state"], "candidate")
        self.assertEqual({item["name"] for item in preview["preview"]["affected"]},
                         {"Voice 192K", "Second voice"})
        refused_status, refusal = self._post("save", mode="edit", record=edited)
        self.assertEqual(refused_status, 409)
        self.assertIn("Review the changed served values", refusal["error"])
        self.assertEqual(store.read_record(self.store, "synthesis", "prof-breeze")["speech_executable_id"], "exe-breeze")
        status, saved = self._post("save", mode="edit", record=edited, confirm=True,
                                   review_token=preview["review_token"])
        self.assertEqual(status, 200, saved)
        self.assertEqual(store.read_record(self.store, "synthesis", "prof-breeze")["speech_executable_id"],
                         "exe-breeze-second")
        restored = dict(edited, speech_executable_id="exe-breeze")
        status, preview = self._post("preview", record=restored)
        self.assertEqual(status, 200)
        self.assertEqual(preview["preview"]["affected"][0]["state"]["state"], "qualified")
        status, saved = self._post("save", mode="edit", record=restored, confirm=True,
                                   review_token=preview["review_token"])
        self.assertEqual(status, 200, saved)

        cosmetic = dict(restored, name="Renamed", notes="updated")
        status, saved = self._post("save", mode="edit", record=cosmetic)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["profile"]["name"], "Renamed")

        edited = store.read_record(self.store, "synthesis", "prof-breeze")
        edited["speech_executable_id"] = "exe-breeze-identical"
        status, preview = self._post("preview", record=edited)
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["preview"]["kind"], "served-change")
        self.assertIn({"field": "speech_executable_id", "label": "Speech executable",
                       "old": "exe-breeze", "new": "exe-breeze-identical"},
                      preview["preview"]["changed_values"])
        affected = {item["name"]: item["state"]["state"] for item in preview["preview"]["affected"]}
        self.assertEqual(affected, {"Voice 192K": "qualified", "Second voice": "candidate"})
        status, refusal = self._post("save", mode="edit", record=edited)
        self.assertEqual(status, 409)
        self.assertEqual(store.read_record(self.store, "synthesis", "prof-breeze")["speech_executable_id"],
                         "exe-breeze")
        status, saved = self._post("save", mode="edit", record=edited, confirm=True,
                                   review_token=preview["review_token"])
        self.assertEqual(status, 200, saved)
        self.assertEqual(store.read_record(self.store, "synthesis", "prof-breeze")["speech_executable_id"],
                         "exe-breeze-identical")

    def test_live_profile_is_refused_before_review_and_again_when_confirmation_commits(self):
        edited = self.synthesis_record(); edited["speech_executable_id"] = "exe-breeze-second"
        from matuto import live_records, serving_state
        voice = serving_state.State(admission.VOICE, "bundle-voice")
        with mock.patch.object(live_records, "read_state", return_value=voice):
            status, refusal = self._post("preview", record=edited)
        self.assertEqual(status, 409)
        self.assertIn("Stop serving on Overview", refusal["error"])
        self.assertEqual(store.read_record(self.store, "synthesis", "prof-breeze")["speech_executable_id"], "exe-breeze")

        with mock.patch.object(live_records, "read_state", return_value=serving_state.State(admission.STOPPED)):
            status, preview = self._post("preview", record=edited)
        self.assertEqual(status, 200)
        with mock.patch.object(live_records, "read_state", return_value=voice):
            status, refusal = self._post("save", mode="edit", record=edited, confirm=True,
                                         review_token=preview["review_token"])
        self.assertEqual(status, 409)
        self.assertIn("Stop serving on Overview", refusal["error"])

    def test_duplicate_and_remove_respect_references_and_keep_history(self):
        self.connection.execute(
            "INSERT INTO cards (card_id,name,gpu_identity,vram_ceiling_bytes,default_gpu_layers,"
            "smoke_output_tokens,bench_prompt_tokens,bench_generation_tokens,bench_repetitions,notes,created_at)"
            " SELECT 'unused-card','Unused card','0000:04:00.0',vram_ceiling_bytes,default_gpu_layers,"
            "smoke_output_tokens,bench_prompt_tokens,bench_generation_tokens,bench_repetitions,notes,created_at"
            " FROM cards WHERE card_id='card-r9'")
        self.connection.commit()
        status, refusal = self._post("duplicate", kind="bundle", profile_id="bundle-voice",
                                     new_id="not-speech", name="Not speech")
        self.assertEqual(status, 409)
        self.assertIn("synthesis or recognition", refusal["error"])
        status, refusal = self._post("remove", kind="bundle", profile_id="bundle-voice")
        self.assertEqual(status, 409)
        self.assertIn("synthesis or recognition", refusal["error"])
        status, refusal = self._post("remove", kind="card", profile_id="unused-card")
        self.assertEqual(status, 409)
        self.assertIn("synthesis or recognition", refusal["error"])
        self.assertIsNotNone(self.connection.execute(
            "SELECT 1 FROM bundles WHERE bundle_id='bundle-voice'").fetchone())
        self.assertIsNotNone(self.connection.execute(
            "SELECT 1 FROM cards WHERE card_id='unused-card'").fetchone())
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM model_profiles WHERE profile_id='not-speech'").fetchone())

        status, copied = self._post("duplicate", kind="synthesis", profile_id="prof-breeze",
                                    new_id="prof-breeze-copy", name="Breeze copy")
        self.assertEqual(status, 201, copied)
        self.assertEqual(copied["profile"]["arguments"], self.synthesis_record()["arguments"])
        self.assertEqual(copied["profile"]["bundles"], [])
        status, refusal = self._post("remove", kind="synthesis", profile_id="prof-breeze")
        self.assertEqual(status, 409)
        self.assertIn("Voice 192K", refusal["error"])
        self.assertIn("Second voice", refusal["error"])
        self.connection.execute(
            "INSERT INTO runs (run_id, record_kind, record_id, record_label, procedure, procedure_revision, started_at)"
            " VALUES ('voice-history', 'bundle', 'bundle-voice', 'Voice 192K', 'fixture', '1', ?) ",
            ("2026-10-10T00:00:00+00:00",))
        status, removed = self._post("remove", kind="synthesis", profile_id="prof-breeze-copy")
        self.assertEqual(status, 200, removed)
        self.assertIsNotNone(self.connection.execute("SELECT 1 FROM runs WHERE run_id='voice-history'").fetchone())
        self.assertIsNotNone(self.connection.execute("SELECT 1 FROM acceptances WHERE acceptance_id='accept-voice'").fetchone())

    def test_section_and_its_assets_are_discovered_by_the_manager(self):
        status, page = self._request(path="/")
        self.assertEqual(status, 200)
        self.assertIn(b"Speech profiles", page)
        self.assertIn(b"speech-profiles.js", page)
        for path in ("/speech-profiles.js", "/speech-profiles.css"):
            status, body = self._request(path=path)
            self.assertEqual(status, 200)
            self.assertTrue(body)

    def test_session_and_csrf_protect_the_api(self):
        status, refused = self._request(token=False)
        self.assertEqual(status, 401)
        self.assertIn("sign-in", refused["error"])
        status, refused = self._request({"action": "remove", "kind": "synthesis", "profile_id": "prof-breeze"}, csrf=False)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
