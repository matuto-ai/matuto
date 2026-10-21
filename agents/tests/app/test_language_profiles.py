"""Language profile listing and creation through the protected manager HTTP seam."""

import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import admission, login, sessions, serving_state, store  # noqa: E402
from matuto.cards import detect  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, build_server  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class LanguageProfilesHttpTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.models = self.home / "models"
        self.models.mkdir()
        (self.models / "model.gguf").write_bytes(b"model bytes")
        self.opened = store.open_store()
        self.addCleanup(self.opened.close)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        db = self.opened.connection()
        db.executescript(f"""
            INSERT INTO cards VALUES ('card-a', 'Test GPU', '0000:03:00.0', 1000,
                -1, 16, 64, 16, 1, '', '{now}');
            INSERT INTO artifact_locations VALUES ('loc-a', '{self.models}', '', '{now}');
            INSERT INTO acquisition_records VALUES ('acq-a', 'Model set', 'adoption',
                'locally-hashed', NULL, NULL, NULL, 'loc-a', '{now}', '', '{now}');
            INSERT INTO acquisition_files VALUES ('acq-a', 'model.gguf', 11, NULL,
                '{'a' * 64}', '{now}');
            INSERT INTO argument_catalogue VALUES ('arg-temp', '--temperature', 1,
                'Temperature', 'server,smoke,bench', '');
        """)
        login.create_login("operator", "secret", "secret")
        self.cookie, _expiry = sessions.start_session("operator", "secret")
        self.server = build_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf
        self.opener = urllib.request.build_opener(NoRedirect)
        self.gpus = {"state": "completed", "devices": [
            {"pciAddress": "0000:03:00.0", "vramBytes": 1000}]}

    def request(self, path, *, method="GET", body=None, authenticated=True):
        headers = {"Host": ALLOWED_HOST}
        if authenticated:
            headers["Cookie"] = f"matuto_session={self.cookie}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "Origin": MANAGER_ORIGIN,
                            "X-CSRF-Token": self.csrf})
        request = urllib.request.Request(self.address + path, method=method, data=data, headers=headers)
        try:
            response = self.opener.open(request, timeout=5)
            return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def profile(self, profile_id="profile-new"):
        return {"kind": "language", "profile_id": profile_id, "name": "New profile",
                "alias": "new-profile", "context_size": 4096, "thinking": "off",
                "card_id": "card-a", "notes": "", "model": {
                    "acquisition_id": "acq-a", "file_name": "model.gguf"},
                "mmproj": None, "mtp": None, "arguments": [
                    {"tool": "server", "option": "--custom-option",
                     "value": "space ' quote"}]}

    def test_non_language_record_is_refused_without_side_effects(self):
        card = {"kind": "card", "card_id": "injected-card", "name": "Unexpected card",
                "gpu_identity": "0000:04:00.0", "vram_ceiling_bytes": 1000,
                "default_gpu_layers": "all", "smoke_output_tokens": 16,
                "bench_prompt_tokens": 64, "bench_generation_tokens": 16,
                "bench_repetitions": 1, "notes": ""}
        db = self.opened.connection()
        before = db.total_changes
        with mock.patch("matuto.cards.detect") as detect_cards:
            status, body = self.request("/api/language-profiles", method="POST", body=card)
        self.assertEqual(status, 409)
        self.assertIn("kind", json.loads(body)["error"])
        detect_cards.assert_not_called()
        self.assertEqual(db.execute("SELECT count(*) FROM cards").fetchone()[0], 1)
        self.assertEqual(db.total_changes, before)

    def test_create_uses_store_validation_preserves_custom_argv_and_returns_unpublished(self):
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            status, body = self.request("/api/language-profiles", method="POST", body=self.profile())
        self.assertEqual(status, 201, body)
        self.assertEqual(json.loads(body)["published"], False)
        saved = store.read_record(self.opened, "language", "profile-new")
        self.assertEqual(saved["arguments"], [{"tool": "server", "option": "--custom-option",
                                                "value": "space ' quote"}])
        self.assertFalse(saved["published"])
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            status, body = self.request("/api/language-profiles")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["profiles"][0]["profile_id"], "profile-new")

    def test_bad_path_digest_and_incomplete_picks_are_refused_without_writing(self):
        cases = []
        typed_path = self.profile("typed-path")
        typed_path["path"] = str(self.models / "model.gguf")
        cases.append((typed_path, "path"))
        typed_digest = self.profile("typed-digest")
        typed_digest["model"] = {"path": str(self.models / "model.gguf"), "sha256": "a" * 64}
        cases.append((typed_digest, "model"))
        db = self.opened.connection()
        db.execute("INSERT INTO acquisition_records VALUES ('acq-incomplete', 'Incomplete', 'adoption',"
                   " 'locally-hashed', NULL, NULL, NULL, 'loc-a', NULL, '', ?)",
                   (datetime.datetime.now(datetime.timezone.utc).isoformat(),))
        db.execute("INSERT INTO acquisition_files VALUES ('acq-incomplete', 'partial.gguf', 0, NULL, '', NULL)")
        incomplete = self.profile("incomplete")
        incomplete["model"] = {"acquisition_id": "acq-incomplete", "file_name": "partial.gguf"}
        cases.append((incomplete, "model"))
        refused_argument = self.profile("refused-argument")
        refused_argument["arguments"] = [{"tool": "server", "option": "--api-key",
                                           "value": "must-not-save"}]
        cases.append((refused_argument, "--api-key"))
        invalid_tool = self.profile("invalid-tool")
        invalid_tool["arguments"] = [{"tool": [], "option": "--unknown", "value": None}]
        cases.append((invalid_tool, "arguments row 1"))
        before = db.total_changes
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            for invalid, field in cases:
                with self.subTest(field=field):
                    status, body = self.request("/api/language-profiles", method="POST", body=invalid)
                    self.assertEqual(status, 409)
                    self.assertIn(field, json.loads(body)["error"])
        self.assertEqual(db.execute("SELECT count(*) FROM model_profiles").fetchone()[0], 0)
        self.assertEqual(db.total_changes, before)

    def test_get_names_missing_runtime_and_card_with_setup_destinations_without_writing(self):
        db = self.opened.connection()

        def get():
            version = db.execute("PRAGMA data_version").fetchone()[0]
            response = self.request("/api/language-profiles")
            self.assertEqual(db.execute("PRAGMA data_version").fetchone()[0], version)
            return response

        with (mock.patch("matuto.cards.detect", return_value=self.gpus),
              mock.patch("matuto.registry.listing", return_value=[]) as listing):
            status, body = get()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["prerequisites"], [{
            "name": "a selected runtime", "href": "#/runtime/install",
            "destination": "Runtime › Install a version"}])
        listing.assert_called_once()

        db.execute("DELETE FROM cards WHERE card_id = 'card-a'")
        with (mock.patch("matuto.cards.detect", return_value=self.gpus),
              mock.patch("matuto.registry.listing", return_value=[]) as listing):
            status, body = get()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["prerequisites"], [
            {"name": "a selected runtime", "href": "#/runtime/install",
             "destination": "Runtime › Install a version"},
            {"name": "a card", "href": "#/settings/cards", "destination": "Settings › Cards"}])

        db.execute("INSERT INTO cards VALUES ('card-a', 'Test GPU', '0000:03:00.0', 1000,"
                   " -1, 16, 64, 16, 1, '', ?)",
                   (datetime.datetime.now(datetime.timezone.utc).isoformat(),))
        db.execute("INSERT INTO builds (build_id, label, kind, location, backend)"
                   " VALUES ('build-a', 'Fixture build', 'llama-hip', '/missing/build', 'hip')")
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            status, body = get()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["prerequisites"][0]["href"], "#/runtime/builds")

        db.execute("DELETE FROM builds WHERE build_id = 'build-a'")
        with (mock.patch("matuto.cards.detect", return_value=self.gpus),
              mock.patch("matuto.registry.listing", return_value=[{"status": "discovered"}])):
            status, body = get()
        self.assertEqual(json.loads(body)["prerequisites"][0]["destination"],
                         "Runtime › llama.cpp builds")

        db.execute("INSERT INTO builds (build_id, label, kind, location, backend)"
                   " VALUES ('build-a', 'Fixture build', 'llama-hip', '/missing/build', 'hip')")
        db.execute("INSERT INTO runtime_selection VALUES (1, 'build-a', 'card-a', 'hip', ?)",
                   (datetime.datetime.now(datetime.timezone.utc).isoformat(),))
        with (mock.patch("matuto.cards.detect", return_value=self.gpus),
              mock.patch("matuto.registry.listing") as listing):
            status, body = get()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["prerequisites"], [])
        listing.assert_not_called()

        db.execute("DELETE FROM runtime_selection")
        db.execute("DELETE FROM cards WHERE card_id = 'card-a'")
        # Normal writes enforce this foreign key; exercise the independently missing-card response.
        with sqlite3.connect(self.opened.path, isolation_level=None) as fixture:
            fixture.execute("PRAGMA foreign_keys = OFF")
            fixture.execute("INSERT INTO runtime_selection VALUES (1, 'build-a', 'missing-card', 'hip', ?)",
                            (datetime.datetime.now(datetime.timezone.utc).isoformat(),))
        with (mock.patch("matuto.cards.detect", return_value=self.gpus),
              mock.patch("matuto.registry.listing") as listing):
            status, body = get()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["prerequisites"], [
            {"name": "a card", "href": "#/settings/cards", "destination": "Settings › Cards"}])
        listing.assert_not_called()
        status, _body = self.request("/api/language-profiles", authenticated=False)
        self.assertEqual(status, 401)

    def test_edit_review_saves_only_after_confirmation_and_revalidates_live_state(self):
        profile = self.profile("profile-edit")
        self.opened.connection().execute(
            "UPDATE acquisition_files SET computed_sha256=?, verified_at='2026-10-14T00:00:00+00:00'"
            " WHERE acquisition_id='acq-a' AND file_name='model.gguf'",
            (store._digest_of(self.models / "model.gguf"),))
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            store.save_record(self.opened, profile, help_text=None)
            self.opened.connection().execute(
                "UPDATE model_profiles SET published=1 WHERE profile_id='profile-edit'")
            store.regenerate_router_files(self.opened)
            self.opened.connection().execute(
                "INSERT INTO bundles (bundle_id,name,notes,load_tokens,load_prompt,created_at)"
                " VALUES ('bundle-edit','Edit reaches this bundle','',128,'hello','2026-10-14T00:00:00+00:00')")
            self.opened.connection().execute(
                "INSERT INTO bundle_members (bundle_id,role,profile_id)"
                " VALUES ('bundle-edit','language','profile-edit')")
        stopped = mock.patch("matuto.live_records.read_state",
                             return_value=serving_state.State(admission.STOPPED))
        stopped.start()
        self.addCleanup(stopped.stop)
        before = store.read_record(self.opened, "language", "profile-edit")
        fingerprint = store.fingerprint(self.opened, before)
        self.opened.connection().execute(
            "INSERT INTO qualification_records (qualification_id, record_kind, record_id, record_label, fingerprint, created_at)"
            " VALUES ('qual-edit', 'language', 'profile-edit', 'Profile', ?, '2026-10-14T00:00:00+00:00')",
            (fingerprint,))
        self.opened.connection().execute(
            "INSERT INTO acceptances (acceptance_id, qualification_id, scope, accepted_by, accepted_at)"
            " VALUES ('accept-edit', 'qual-edit', 'profile', 'operator', '2026-10-14T00:00:00+00:00')")
        self.opened.connection().commit()
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            status, body = self.request("/api/language-profile-edits?profile_id=profile-edit")
        self.assertEqual(status, 200, body)
        editor = json.loads(body)
        self.assertEqual(editor["profile"]["profile_id"], "profile-edit")
        self.assertEqual(editor["profile"]["arguments"], profile["arguments"])
        self.assertEqual(editor["cards"], [{"card_id":"card-a","name":"Test GPU"}])

        status, body = self.request("/api/language-profile-edits", method="POST",
            body={"action": "preview", "record": dict(profile, context_size=8192)})
        self.assertEqual(status, 200, body)
        preview = json.loads(body)
        self.assertEqual(preview["preview"]["state"]["state"], "candidate")
        self.assertEqual({row["name"] for row in preview["preview"]["affected"]}, {"Edit reaches this bundle"})
        self.assertEqual(store.read_record(self.opened, "language", "profile-edit")["context_size"], 4096)

        preset_path = self.opened.path.parent / "router-presets.ini"
        preset_before = preset_path.read_bytes()
        changed = dict(profile, context_size=8192)
        status, body = self.request("/api/language-profile-edits", method="POST",
            body={"action": "save", "record": changed})
        self.assertEqual(status, 409)
        self.assertIn("Review the changed served values", json.loads(body)["error"])
        self.assertEqual(store.read_record(self.opened, "language", "profile-edit")["context_size"], 4096)

        status, body = self.request("/api/language-profile-edits", method="POST",
            body={"action": "save", "record": changed, "confirm": True,
                   "review_token": preview["review_token"]})
        self.assertEqual(status, 200, body)
        saved_profile = store.read_record(self.opened, "language", "profile-edit")
        self.assertEqual(saved_profile["context_size"], 8192)
        self.assertTrue(saved_profile["published"])
        self.assertNotEqual(preset_path.read_bytes(), preset_before)
        self.assertIn(b"ctx-size = 8192", preset_path.read_bytes())
        self.assertEqual(store.qualification_state(self.opened,
            store.read_record(self.opened, "language", "profile-edit"))["state"], "candidate")
        self.assertIn("state", store.qualification_state(self.opened,
            store.read_record(self.opened, "bundle", "bundle-edit")))

        restored = dict(profile, context_size=4096)
        status, body = self.request("/api/language-profile-edits", method="POST",
            body={"action": "preview", "record": restored})
        self.assertEqual(status, 200, body)
        restored_review = json.loads(body)
        self.assertEqual(restored_review["preview"]["state"]["state"], "qualified")
        self.assertIn("state", restored_review["preview"]["affected"][0]["state"])
        status, body = self.request("/api/language-profile-edits", method="POST",
            body={"action": "save", "record": restored, "confirm": True,
                   "review_token": restored_review["review_token"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(store.qualification_state(self.opened,
            store.read_record(self.opened, "language", "profile-edit"))["state"], "qualified")

        cosmetic = dict(restored, name="Renamed only", notes="cosmetic", alias="new-alias",
            arguments=[*restored["arguments"], {"tool":"smoke","option":"--temperature","value":"0.2"}])
        status, body = self.request("/api/language-profile-edits", method="POST",
            body={"action": "save", "record": cosmetic})
        self.assertEqual(status, 200, body)
        saved_cosmetic = store.read_record(self.opened, "language", "profile-edit")
        self.assertEqual(saved_cosmetic["context_size"], 4096)
        self.assertEqual(saved_cosmetic["name"], "Renamed only")
        self.assertEqual(store.qualification_state(self.opened, saved_cosmetic)["state"], "qualified")

    def test_edit_review_matches_committed_state_after_runtime_alias_normalization(self):
        profile = self.profile("profile-alias-preview")
        profile["arguments"] = [{"tool": "server", "option": "--threads", "value": "4"}]
        help_text = {"server": "-t, --threads <N>"}
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            saved, _warnings = store.save_record(self.opened, profile, help_text=help_text)
        fingerprint = store.fingerprint(self.opened, saved)
        db = self.opened.connection()
        db.execute("INSERT INTO qualification_records (qualification_id,record_kind,record_id,record_label,fingerprint,created_at)"
                   " VALUES ('alias-q','language','profile-alias-preview','Alias profile',?,'2026-10-14T00:00:00+00:00')",
                   (fingerprint,))
        db.execute("INSERT INTO acceptances (acceptance_id,qualification_id,scope,accepted_by,accepted_at)"
                   " VALUES ('alias-a','alias-q','profile','operator','2026-10-14T00:00:00+00:00')")
        changed = dict(profile, arguments=[{"tool": "server", "option": "--threads", "value": "8"}])
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            store.save_record(self.opened, changed, record_id=profile["profile_id"], help_text=help_text)
        restored = dict(profile, arguments=[{"tool": "server", "option": "-t", "value": "4"}])
        with (mock.patch("matuto.cards.detect", return_value=self.gpus),
              mock.patch("matuto.routes.language_profiles._runtime_validation",
                         return_value=(help_text, None, {}))):
            status, body = self.request("/api/language-profile-edits", method="POST",
                body={"action": "preview", "record": restored})
            self.assertEqual(status, 200, body)
            preview = json.loads(body)
            self.assertEqual(preview["preview"]["kind"], "served-change")
            self.assertEqual(preview["preview"]["state"]["state"], "qualified")
            status, body = self.request("/api/language-profile-edits", method="POST",
                body={"action": "save", "record": restored, "confirm": True,
                       "review_token": preview["review_token"]})
        self.assertEqual(status, 200, body)
        final = store.read_record(self.opened, "language", profile["profile_id"])
        self.assertEqual(final["arguments"], [{"tool": "server", "option": "--threads", "value": "4"}])
        self.assertEqual(store.qualification_state(self.opened, final)["state"], "qualified")

    def test_edit_refuses_live_before_review_and_rechecks_when_confirmation_commits(self):
        profile = self.profile("profile-live-edit")
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            store.save_record(self.opened, profile, help_text=None)
        self.opened.connection().execute("UPDATE model_profiles SET published=1 WHERE profile_id='profile-live-edit'")
        self.opened.connection().execute(
            "INSERT INTO bundles (bundle_id,name,notes,load_tokens,load_prompt,created_at)"
            " VALUES ('bundle-live-edit','Live edit bundle','',128,'hello','2026-10-14T00:00:00+00:00')")
        self.opened.connection().execute(
            "INSERT INTO bundle_members (bundle_id,role,profile_id)"
            " VALUES ('bundle-live-edit','language','profile-live-edit')")
        changed = dict(profile, context_size=8192)
        with mock.patch("matuto.live_records.read_state",
                        return_value=serving_state.State(admission.AGENTIC)):
            status, body = self.request("/api/language-profile-edits", method="POST",
                body={"action": "preview", "record": changed})
        self.assertEqual(status, 409)
        self.assertIn("Stop serving on Overview", json.loads(body)["error"])
        self.assertEqual(store.read_record(self.opened, "language", "profile-live-edit")["context_size"], 4096)

        with mock.patch("matuto.live_records.read_state",
                        return_value=serving_state.State(admission.STOPPED)):
            status, body = self.request("/api/language-profile-edits", method="POST",
                body={"action": "preview", "record": changed})
        preview = json.loads(body)
        self.assertEqual(status, 200, preview)
        with mock.patch("matuto.live_records.read_state",
                        return_value=serving_state.State(admission.AGENTIC)):
            status, body = self.request("/api/language-profile-edits", method="POST",
                body={"action": "save", "record": changed, "confirm": True,
                       "review_token": preview["review_token"]})
        self.assertEqual(status, 409)
        self.assertIn("Stop serving on Overview", json.loads(body)["error"])
        self.assertEqual(store.read_record(self.opened, "language", "profile-live-edit")["context_size"], 4096)
        with mock.patch("matuto.live_records.read_state",
                        return_value=serving_state.State(admission.VOICE, bundle="bundle-live-edit")):
            status, body = self.request("/api/language-profile-edits", method="POST",
                body={"action": "preview", "record": changed})
        self.assertEqual(status, 409)
        self.assertIn("Stop serving on Overview", json.loads(body)["error"])
        self.assertEqual(store.read_record(self.opened, "language", "profile-live-edit")["context_size"], 4096)

        cosmetic = dict(profile, name="Cosmetic live edit")
        with mock.patch("matuto.live_records.read_state",
                        return_value=serving_state.State(admission.AGENTIC)):
            status, body = self.request("/api/language-profile-edits", method="POST",
                body={"action": "save", "record": cosmetic})
        self.assertEqual(status, 409)
        self.assertIn("Stop serving on Overview", json.loads(body)["error"])
        self.assertEqual(store.read_record(self.opened, "language", "profile-live-edit")["name"],
                         profile["name"])

    def test_unadvertised_arguments_for_each_tool_are_saved_with_warnings(self):
        db = self.opened.connection()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        db.execute("INSERT INTO builds (build_id, label, kind, location, backend, state)"
                   " VALUES ('build-a', 'Fixture build', 'llama-hip', '/fixture/build', 'hip', 'registered')")
        db.execute("INSERT INTO installations VALUES ('/fixture/build', 'recipe', NULL, NULL, NULL,"
                   " 'checked', '', '{}', ?)", (now,))
        db.execute("INSERT INTO runtime_selection VALUES (1, 'build-a', 'card-a', 'hip', ?)", (now,))
        import matuto_runtime
        for tool in ("server", "smoke", "bench"):
            record = self.profile("unadvertised-" + tool)
            record["arguments"] = [{"tool": tool, "option": "--custom-option", "value": None}]
            with (mock.patch("matuto.cards.detect", return_value=self.gpus),
                  mock.patch("matuto.registry.availability", return_value=(True, None)),
                  mock.patch.object(matuto_runtime, "execution_environment", return_value={}),
                  mock.patch.object(matuto_runtime, "_tool_descriptor", return_value=(10, 11)),
                  mock.patch.object(matuto_runtime, "probe", return_value="--device --list-devices --temperature"),
                  mock.patch("matuto.router._selector", return_value="GPU-0"),
                  mock.patch("os.close")):
                status, body = self.request("/api/language-profiles", method="POST", body=record)
            self.assertEqual(status, 201, body)
            warnings = json.loads(body)["warnings"]
            self.assertEqual(len(warnings), 1)
            self.assertIn("not advertised by the active build", warnings[0])
            self.assertIn("--custom-option", warnings[0])

    def test_server_only_build_saves_profiles_and_warns_for_missing_tool_arguments(self):
        binary_dir = self.home / "server-only" / "bin"
        binary_dir.mkdir(parents=True)
        server = binary_dir / "llama-server"
        server.write_text("#!/bin/sh\nexit 0\n")
        server.chmod(0o700)
        db = self.opened.connection()
        location = str(binary_dir.parent)
        db.execute("INSERT INTO builds (build_id, label, kind, location, backend, state)"
                   " VALUES ('server-only', 'Server only', 'llama-hip', ?, 'hip', 'registered')",
                   (location,))
        db.execute("INSERT INTO installations VALUES (?, 'recipe', NULL, NULL, NULL, 'checked', '', '{}', ?)",
                   (location, datetime.datetime.now(datetime.timezone.utc).isoformat()))
        db.execute("INSERT INTO runtime_selection VALUES (1, 'server-only', 'card-a', 'hip', ?)",
                   (datetime.datetime.now(datetime.timezone.utc).isoformat(),))
        import matuto_runtime
        with (mock.patch("matuto.cards.detect", return_value=self.gpus),
              mock.patch("matuto.registry.availability", return_value=(True, None)),
              mock.patch.object(matuto_runtime, "execution_environment", return_value={}),
              mock.patch.object(matuto_runtime, "probe", return_value="--device --list-devices"),
              mock.patch("matuto.router._selector", return_value="GPU-0")):
            record = self.profile("server-only-no-args")
            record["arguments"] = []
            status, body = self.request("/api/language-profiles", method="POST", body=record)
            self.assertEqual(status, 201, body)
            self.assertEqual(store.read_record(self.opened, "language", record["profile_id"])["arguments"], [])

            record = self.profile("server-only-optional-args")
            record["arguments"] = [
                {"tool": "smoke", "option": "--custom-smoke", "value": None},
                {"tool": "bench", "option": "--custom-bench", "value": None}]
            status, body = self.request("/api/language-profiles", method="POST", body=record)
        self.assertEqual(status, 201, body)
        warnings = json.loads(body)["warnings"]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("smoke" in warning and "llama-cli" in warning for warning in warnings))
        self.assertTrue(any("bench" in warning and "llama-bench" in warning for warning in warnings))
        self.assertIn("--custom-smoke", [row["option"] for row in
                      store.read_record(self.opened, "language", record["profile_id"])["arguments"]])

    def test_list_returns_all_qualification_labels_availability_and_only_complete_picks(self):
        records = [self.profile("candidate"), self.profile("changed"),
                   self.profile("qualified"), self.profile("published-candidate")]
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            for record in records:
                store.save_record(self.opened, record, help_text=None)
        db = self.opened.connection()
        current = store.fingerprint(self.opened, store.read_record(self.opened, "language", "qualified"))
        db.execute("INSERT INTO qualification_records VALUES ('q-qualified', 'language', 'qualified',"
                   " 'New profile', ?, NULL, '2026-10-12T00:00:00+00:00')", (current,))
        db.execute("INSERT INTO acceptances VALUES ('a-qualified', 'q-qualified', 'profile', '',"
                   " 'operator', '2026-10-12T00:00:00+00:00', NULL)")
        db.execute("INSERT INTO qualification_records VALUES ('q-changed', 'language', 'changed',"
                   " 'New profile', ?, NULL, '2026-10-12T00:00:00+00:00')", ('sha256:' + '0' * 64,))
        db.execute("INSERT INTO acceptances VALUES ('a-changed', 'q-changed', 'profile', '',"
                   " 'operator', '2026-10-12T00:00:00+00:00', NULL)")
        db.execute("UPDATE model_profiles SET published = 1 WHERE profile_id = 'published-candidate'")
        db.execute("UPDATE acquisition_records SET verified_at = NULL WHERE acquisition_id = 'acq-a'")
        with mock.patch("matuto.cards.detect", return_value=self.gpus):
            status, body = self.request("/api/language-profiles")
        self.assertEqual(status, 200)
        data = json.loads(body)
        by_id = {record["profile_id"]: record for record in data["profiles"]}
        self.assertEqual(by_id["candidate"]["qualification"]["state"], "candidate")
        self.assertEqual(by_id["changed"]["qualification"]["state"], "candidate")
        self.assertTrue(by_id["changed"]["qualification"]["changed_since_qualification"])
        self.assertEqual(by_id["qualified"]["qualification"]["state"], "qualified")
        self.assertEqual(by_id["published-candidate"]["qualification"]["state"], "candidate")
        self.assertTrue(by_id["published-candidate"]["published"])
        self.assertTrue(all(not record["available"] and record["reason"] for record in by_id.values()))
        self.assertEqual(data["picks"]["model"], [])
        self.assertEqual(data["cards"], [{"card_id": "card-a", "name": "Test GPU"}])
        self.assertEqual(data["catalogue"]["server"], [
            {"flag": "--temperature", "takes_value": True}])


if __name__ == "__main__":
    unittest.main()
