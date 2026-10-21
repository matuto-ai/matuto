"""Overview derives setup and repair state from the authenticated manager HTTP route."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import login, sessions, store, storage  # noqa: E402
from matuto.web import ALLOWED_HOST, SESSION_COOKIE, build_server  # noqa: E402


class OverviewHttpTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.environment = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_DATA_HOME": str(self.home / "data"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.opened = store.open_store()
        self.addCleanup(self.opened.close)
        login.create_login("operator", "secret", "secret")
        self.token, _ = sessions.start_session("operator", "secret")
        self.server = build_server(port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}/api/overview"

    def overview(self):
        request = urllib.request.Request(self.address, headers={
            "Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read())

    def test_page_script_is_available_as_authenticated_same_origin_javascript(self):
        request = urllib.request.Request(self.address.removesuffix("/api/overview") + "/overview.js", headers={
            "Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "text/javascript")
            self.assertIn(b"/api/overview", response.read())

    def test_empty_store_shows_four_unordered_setup_tasks_and_exact_destinations(self):
        result = self.overview()
        self.assertEqual([task["label"] for task in result["tasks"]], [
            "Set model storage", "Get model files", "Prepare the runtime",
            "Create or import a language profile",
        ])
        self.assertEqual([task["href"] for task in result["tasks"]], [
            "#/settings/storage-artifacts", "#/model-files/acquisition", "#/runtime/install",
            "#/profiles/language",
        ])
        self.assertTrue(all(task["destination"] for task in result["tasks"]))
        self.assertEqual(result["configured"], [])
        self.assertIsNone(result["storageRepair"])
        connection = self.opened.connection()
        connection.execute(
            "INSERT INTO builds (build_id,label,kind,location,backend,recipe,state)"
            " VALUES ('registered','Registered build','llama-hip','/builds/registered','hip','llama-hip','registered')")
        result = self.overview()
        runtime = next(task for task in result["tasks"] if task["label"] == "Prepare the runtime")
        self.assertEqual(runtime["href"], "#/runtime/builds")

    def test_only_complete_records_count_and_all_four_hide_both_lists(self):
        connection = self.opened.connection()
        root = self.home / "models"
        root.mkdir(mode=0o700)
        storage.save_model_storage(connection, root)
        result = self.overview()
        self.assertEqual([task["label"] for task in result["tasks"]], [
            "Get model files", "Prepare the runtime", "Create or import a language profile",
        ])
        self.assertEqual([item["label"] for item in result["configured"]], ["Set model storage"])
        stamp = "2026-10-12T00:00:00+00:00"
        connection.execute(
            "INSERT INTO acquisition_records (acquisition_id,name,source,location_id,created_at)"
            " VALUES ('acq-one','Complete model','adoption','loc-1',?)", (stamp,))
        result = self.overview()
        self.assertIn("Get model files", [task["label"] for task in result["tasks"]])
        connection.execute(
            "INSERT INTO acquisition_files (acquisition_id,file_name,size_bytes,computed_sha256)"
            " VALUES ('acq-one','model.gguf',4,'')")
        result = self.overview()
        self.assertIn("Get model files", [task["label"] for task in result["tasks"]])
        # A full computed identity proves this record was complete even if a later
        # unavailable-file check has cleared the record's current verified_at.
        connection.execute(
            "UPDATE acquisition_files SET computed_sha256 = ?, verified_at = ?"
            " WHERE acquisition_id = 'acq-one'", ("a" * 64, stamp))
        result = self.overview()
        self.assertEqual([task["label"] for task in result["tasks"]], [
            "Prepare the runtime", "Create or import a language profile",
        ])
        self.assertEqual([item["label"] for item in result["configured"]], [
            "Set model storage", "Get model files",
        ])
        connection.execute(
            "INSERT INTO cards (card_id,name,gpu_identity,vram_ceiling_bytes,default_gpu_layers,"
            "smoke_output_tokens,bench_prompt_tokens,bench_generation_tokens,bench_repetitions,created_at)"
            " VALUES ('card-one','Test card','0000:01:00.0',1000,-1,8,8,8,1,?)", (stamp,))
        connection.execute(
            "INSERT INTO builds (build_id,label,kind,location,backend,recipe,state)"
            " VALUES ('build-one','Test runtime','llama-hip','/builds/test','hip','llama-hip','registered')")
        connection.execute(
            "INSERT INTO runtime_selection (selection_id,build_id,card_id,backend,saved_at)"
            " VALUES (1,'build-one','card-one','hip',?)", (stamp,))
        result = self.overview()
        self.assertEqual([task["label"] for task in result["tasks"]], [
            "Create or import a language profile",
        ])
        self.assertEqual([item["label"] for item in result["configured"]], [
            "Set model storage", "Get model files", "Prepare the runtime",
        ])
        connection.execute(
            "INSERT INTO model_profiles (profile_id,kind,name,alias,created_at)"
            " VALUES ('profile-one','language','Test profile','test',?)", (stamp,))
        result = self.overview()
        self.assertEqual(result["tasks"], [])
        self.assertEqual(result["configured"], [])
        self.assertIsNone(result["storageRepair"])

    def test_cutover_shape_ignores_checkout_settings_and_uses_build_list(self):
        connection = self.opened.connection()
        legacy_store = store.store_directory() / "model-root"
        legacy_store.write_text("/legacy/models\n")
        legacy_runtime = store.store_directory() / "runtime-selection.json"
        legacy_runtime.write_text('{"build":"old"}\n')
        stamp = "2026-10-12T00:00:00+00:00"
        connection.execute(
            "INSERT INTO artifact_locations (location_id,path,created_at)"
            " VALUES ('loc-import','/imported',?)", (stamp,))
        connection.execute(
            "INSERT INTO acquisition_records (acquisition_id,name,source,location_id,verified_at,created_at)"
            " VALUES ('acq-imported','Imported models','adoption','loc-import',?,?)", (stamp, stamp))
        connection.execute(
            "INSERT INTO acquisition_files (acquisition_id,file_name,size_bytes,computed_sha256,verified_at)"
            " VALUES ('acq-imported','model.gguf',4,?,?)", ("b" * 64, stamp))
        connection.execute(
            "INSERT INTO model_profiles (profile_id,kind,name,alias,created_at)"
            " VALUES ('profile-imported','language','Imported profile','imported',?)", (stamp,))
        build = self.home / "data" / "ai-agents-sandbox" / "builds" / "found"
        (build / "bin").mkdir(parents=True)
        (build / "bin" / "llama-server").write_text("fixture")
        result = self.overview()
        self.assertEqual([task["label"] for task in result["tasks"]], [
            "Set model storage", "Prepare the runtime",
        ])
        self.assertEqual(result["tasks"][1]["href"], "#/runtime/builds")
        self.assertEqual([item["label"] for item in result["configured"]], [
            "Get model files", "Create or import a language profile",
        ])

    def test_unavailable_saved_storage_is_repair_not_setup_and_names_affected_records(self):
        connection = self.opened.connection()
        root = self.home / "models"
        root.mkdir(mode=0o700)
        model = root / "model.gguf"
        model.write_bytes(b"verified model bytes")
        saved = storage.save_model_storage(connection, root)
        stamp = "2026-10-12T00:00:00+00:00"
        connection.execute(
            "INSERT INTO acquisition_records (acquisition_id,name,source,location_id,verified_at,created_at)"
            " VALUES ('acq-affected','Affected acquisition','adoption',?,?,?)",
            (saved["location_id"], stamp, stamp))
        connection.execute(
            "INSERT INTO acquisition_files (acquisition_id,file_name,size_bytes,computed_sha256,verified_at)"
            " VALUES ('acq-affected','model.gguf',?,?,?)",
            (len(model.read_bytes()), hashlib.sha256(model.read_bytes()).hexdigest(), stamp))
        missing = self.home / "missing-models"
        outcome = storage.change_location(connection, saved["location_id"], missing,
                                          serving_state="stopped")
        self.assertFalse(outcome["available"])
        result = self.overview()
        self.assertNotIn("Set model storage", [task["label"] for task in result["tasks"]])
        self.assertNotIn("Get model files", [task["label"] for task in result["tasks"]])
        self.assertIn("Get model files", [item["label"] for item in result["configured"]])
        configured_storage = next(item for item in result["configured"]
                                  if item["label"] == "Set model storage")
        self.assertEqual(configured_storage["value"], str(missing))
        repair = result["storageRepair"]
        self.assertEqual(repair["title"], "Storage unavailable")
        self.assertEqual(repair["path"], str(missing))
        self.assertIn("does not exist", repair["detail"])
        self.assertIn("Affected acquisition", repair["detail"])
        self.assertEqual(repair["link"], "#/settings/storage-artifacts")


if __name__ == "__main__":
    unittest.main()
