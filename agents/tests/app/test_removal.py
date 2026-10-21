"""Exercise Matuto removal at its Python and CLI-facing operation seam."""

import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import admission, bootstrap, removal, serving_state, snapshot, store, updater  # noqa: E402


class RemovalTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="matuto-removal-test-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.data_home = self.home / "data"
        self.data_root = self.data_home / "matuto"
        self.config.mkdir(mode=0o700)
        self.data_root.mkdir(parents=True, mode=0o700)
        self.bin = self.home / "bin"
        self.bin.mkdir(mode=0o700)
        self.calls = self.home / "systemctl.log"
        self.calls.touch()
        systemctl = self.bin / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\nif [ \"$2\" != is-active ] && [ \"$2\" != list-units ]; then\n"
            "  printf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\nfi\n"
            "if [ \"$2\" = is-active ]; then echo inactive; exit 3; fi\n"
            "if [ -n \"$FAIL_DISABLE\" ] && [ \"$*\" = '--user disable matuto-manager.service' ]; then exit 1; fi\n"
            "exit 0\n")
        systemctl.chmod(0o700)
        self.systemd_run_calls = self.home / "systemd-run.log"
        self.systemd_run_calls.touch()
        systemd_run = self.bin / "systemd-run"
        systemd_run.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMD_RUN_LOG\"\n"
            "while [ $# -gt 0 ]; do case \"$1\" in --*) shift;; *) break;; esac; done\n"
            "unset XDG_CONFIG_HOME XDG_DATA_HOME\nexec \"$@\"\n")
        systemd_run.chmod(0o700)
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.config),
            "XDG_DATA_HOME": str(self.data_home), "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "SYSTEMCTL_LOG": str(self.calls), "SYSTEMD_RUN_LOG": str(self.systemd_run_calls),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self._installation()
        self.opened = store.open_store(self.config)
        self.addCleanup(self.opened.close)
        connection = self.opened.connection()
        self.models = self.home / "models"
        self.models.mkdir()
        (self.models / "weights.gguf").write_bytes(b"model weights")
        (self.models / "breeze").write_bytes(b"speech executable")
        (self.models / "whisper").write_bytes(b"recognition executable")
        partials = self.models / ".matuto-downloads"
        partials.mkdir()
        (partials / "download.partial").write_bytes(b"partial download")
        self.build = self.home / "build"
        self.build.mkdir()
        (self.build / "llama-server").write_bytes(b"registered build")
        self.build_source = self.data_home / "ai-agents-sandbox/build-source"
        self.build_source.mkdir(parents=True)
        (self.build_source / "source.txt").write_bytes(b"build source")
        self.build_staging = self.data_home / "ai-agents-sandbox/build-staging"
        self.build_staging.mkdir(parents=True)
        (self.build_staging / "stage.txt").write_bytes(b"build staging")
        self.runtime_builds = self.data_home / "ai-agents-sandbox/builds"
        self.runtime_builds.mkdir(parents=True)
        (self.runtime_builds / "runtime.txt").write_bytes(b"runtime build")
        connection.executescript(
            "INSERT INTO cards (card_id, name, gpu_identity, vram_ceiling_bytes, default_gpu_layers,"
            " smoke_output_tokens, bench_prompt_tokens, bench_generation_tokens, bench_repetitions, created_at)"
            " VALUES ('card-r9', 'Card', '0000:03:00.0', 1024, 1, 1, 1, 1, 1, 'now');"
            "INSERT INTO artifact_locations (location_id, path, created_at) VALUES ('loc', '/models', 'now');"
            "INSERT INTO speech_executables (executable_id, kind, location_id, path, sha256, installed_at)"
            " VALUES ('exe-breeze', 'synthesis', 'loc', '/models/breeze', 'abc', 'now');"
            "INSERT INTO speech_executables (executable_id, kind, location_id, path, sha256, installed_at)"
            " VALUES ('exe-whisper', 'recognition', 'loc', '/models/whisper', 'abc', 'now');"
            "INSERT INTO model_profiles (profile_id, kind, name, alias, context_size, thinking,"
            " card_id, created_at) VALUES ('prof-coder', 'language', 'Language', 'language',"
            " 2048, 'high', 'card-r9', 'now');"
            "INSERT INTO model_profiles (profile_id, kind, name, card_id, speech_executable_id,"
            " created_at) VALUES ('prof-breeze', 'synthesis', 'Synthesis', 'card-r9', 'exe-breeze', 'now');"
            "INSERT INTO model_profiles (profile_id, kind, name, speech_executable_id, created_at)"
            " VALUES ('prof-whisper', 'recognition', 'Recognition', 'exe-whisper', 'now');"
            "INSERT INTO bundles (bundle_id, name, created_at) VALUES ('bundle', 'Voice', 'now');"
            "INSERT INTO bundle_members VALUES ('bundle', 'language', 'prof-coder');"
            "INSERT INTO bundle_members VALUES ('bundle', 'synthesis', 'prof-breeze');"
            "INSERT INTO bundle_members VALUES ('bundle', 'recognition', 'prof-whisper');"
            "INSERT INTO voice_serving VALUES (1, 'bundle');"
            "INSERT INTO voice_serving_parts VALUES ('synthesis', 'bundle', 8801);"
            "INSERT INTO voice_serving_parts VALUES ('recognition', 'bundle', 8802);"
            "INSERT INTO builds (build_id, label, kind, location, backend, state)"
            " VALUES ('build', 'Build', 'llama-hip', '/build', 'HIP/ROCm', 'registered');"
            "INSERT INTO runtime_selection (selection_id, build_id, card_id, backend, saved_at)"
            " VALUES (1, 'build', 'card-r9', 'HIP/ROCm', 'now');")
        connection.execute("UPDATE artifact_locations SET path = ? WHERE location_id = 'loc'",
                           (str(self.models),))
        connection.execute("UPDATE speech_executables SET path = ? WHERE executable_id = 'exe-breeze'",
                           (str(self.models / "breeze"),))
        connection.execute("UPDATE speech_executables SET path = ? WHERE executable_id = 'exe-whisper'",
                           (str(self.models / "whisper"),))
        connection.execute("UPDATE builds SET location = ? WHERE build_id = 'build'",
                           (str(self.build),))
        connection.commit()
        self.pre_update_snapshot = snapshot.snapshot_path(store.store_path(self.config))
        self.pre_update_snapshot.write_bytes(b"pre-update snapshot")

    def _installation(self):
        releases = self.data_root / "releases"
        version = releases / "2026.10.0"
        (version / "agents").mkdir(parents=True, mode=0o700)
        shutil.copytree(AGENTS / "matuto", version / "agents/matuto",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(AGENTS / "matuto_runtime.py", version / "agents/matuto_runtime.py")
        (version / "agents/matuto-lab").write_text("lab")
        (version / "RELEASE.json").write_text("manifest")
        releases.mkdir(mode=0o700, exist_ok=True)
        (releases / "current").symlink_to(version.name)
        self.command = self.home / ".local/bin/matuto-manager"
        self.command.parent.mkdir(parents=True, mode=0o700)
        self.command.write_text(bootstrap._manager_command(self.data_root))
        self.command.chmod(0o755)
        updater_root = self.data_root / "updater"
        updater_version = updater_root / "versions/2026.10.0"
        (updater_version / "agents").mkdir(parents=True, mode=0o700)
        shutil.copytree(AGENTS / "matuto", updater_version / "agents/matuto",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(AGENTS / "matuto_runtime.py", updater_version / "agents/matuto_runtime.py")
        (updater_root / "current").symlink_to("versions/2026.10.0")
        unit_dir = self.config / "systemd/user"
        unit_dir.mkdir(parents=True)
        for name in ("matuto-manager.service", "matuto-app-router.service",
                     "matuto-app-voice-router.service", "matuto-app-voice-part-speech.service",
                     "other.service"):
            (unit_dir / name).write_text(name)
        apps = self.data_home / "applications"
        apps.mkdir()
        for name in ("matuto.desktop", "repair-matuto.desktop", "update-matuto.desktop",
                     "other.desktop"):
            (apps / name).write_text(name)
        state_dir = store.store_directory(self.config)
        state_dir.mkdir(parents=True, mode=0o700)
        (state_dir / "router-presets.ini").write_text("preset")
        (state_dir / "voice-presets.ini").write_text("voice")
        (state_dir / "login.json").write_bytes(b"credentials")
        key = self.config / "ai-agents-sandbox/pi-local/api-key"
        key.parent.mkdir(parents=True)
        key.write_bytes(b"router secret")
        (self.data_root / ".accepted-release-list-number").write_text("4\n")
        checkout_runtime = self.home / ".config/ai-agents-sandbox/matuto-runtime"
        checkout_runtime.mkdir(parents=True)
        (checkout_runtime / "selection.json").write_text("checkout selection")
        (checkout_runtime / "builds.json").write_text("checkout builds")

    def test_preview_is_read_only_and_distinguishes_removed_and_kept_paths(self):
        before = {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        plan = removal.preview(config_root=self.config, data_root=self.data_root)
        after = {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertIn("Router API key", {item["item"] for item in plan["keep"]})
        self.assertIn("Independent updater (last)", {item["item"] for item in plan["remove"]})
        self.assertIn("Installed matuto-manager command", {item["item"] for item in plan["remove"]})
        records = next(item for item in plan["remove"]
                       if item["item"] == "Transient voice-serving records")
        self.assertIn("voice_serving_parts", records["path"])
        self.assertEqual(plan["consumer_configuration"], "Never touched.")

    def test_confirmed_removal_keeps_data_and_checkout_owned_paths(self):
        self.opened.close()
        retained_files = [
            self.pre_update_snapshot, store.store_directory(self.config) / "login.json",
            self.config / "ai-agents-sandbox/pi-local/api-key",
            self.models / "weights.gguf", self.models / "breeze", self.models / "whisper",
            self.models / ".matuto-downloads/download.partial",
            self.build / "llama-server", self.build_source / "source.txt",
            self.build_staging / "stage.txt", self.runtime_builds / "runtime.txt",
            self.home / ".config/ai-agents-sandbox/matuto-runtime/selection.json",
            self.home / ".config/ai-agents-sandbox/matuto-runtime/builds.json",
        ]
        retained_before = {path: (path.read_bytes(), path.stat().st_ino) for path in retained_files}
        before_store = store._readonly_connect(store.store_path(self.config))
        try:
            tables = [row[0] for row in before_store.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ) if row[0] not in ("voice_serving", "voice_serving_parts")]
            rows_before = {table: sorted(
                (tuple(row) for row in before_store.execute(f'SELECT * FROM "{table}"')), key=repr)
                for table in tables}
        finally:
            before_store.close()
        command = self.data_root / "releases/current/agents/matuto/cli.py"
        result = __import__("subprocess").run([sys.executable, str(command), "remove"],
                                              input="REMOVE\n", capture_output=True, text=True,
                                              env=os.environ.copy())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Matuto and its updater were removed.", result.stdout)
        self.assertTrue(store.store_path(self.config).is_file())
        after_store = store._readonly_connect(store.store_path(self.config))
        try:
            rows_after = {table: sorted(
                (tuple(row) for row in after_store.execute(f'SELECT * FROM "{table}"')), key=repr)
                for table in tables}
            self.assertEqual(rows_after, rows_before)
        finally:
            after_store.close()
        for path, (body, inode) in retained_before.items():
            self.assertEqual(path.read_bytes(), body, str(path))
            self.assertEqual(path.stat().st_ino, inode, str(path))
        self.assertEqual((store.store_directory(self.config) / "login.json").read_bytes(), b"credentials")
        self.assertEqual((self.config / "ai-agents-sandbox/pi-local/api-key").read_bytes(), b"router secret")
        self.assertTrue((self.data_root / ".accepted-release-list-number").exists())
        self.assertEqual((self.home / ".config/ai-agents-sandbox/matuto-runtime/selection.json").read_text(),
                         "checkout selection")
        self.assertEqual((self.home / ".config/ai-agents-sandbox/matuto-runtime/builds.json").read_text(),
                         "checkout builds")
        self.assertEqual((self.data_root / "releases").exists(), False)
        self.assertEqual((self.data_root / "updater").exists(), False)
        self.assertFalse(self.command.exists())
        self.assertTrue((self.config / "systemd/user/other.service").exists())
        self.assertTrue((self.data_home / "applications/other.desktop").exists())
        self.assertTrue((self.data_home / "applications/matuto.desktop").exists() is False)
        self.assertTrue((self.config / "systemd/user/matuto-manager.service").exists() is False)
        self.assertTrue((self.config / "systemd/user/matuto-app-router.service").exists() is False)
        calls = self.calls.read_text().splitlines()
        self.assertIn("--user disable matuto-manager.service", calls)
        self.assertIn("--user stop matuto-manager.service", calls)
        self.assertNotIn("--user stop matuto-app-router.service", calls)
        finalizer = self.systemd_run_calls.read_text()
        self.assertIn("--wait", finalizer)
        self.assertIn("--collect", finalizer)
        self.assertIn("matuto-removal-finalizer-", finalizer)
        self.assertIn("updater/current/agents/matuto/updater.py remove", finalizer)
        opened = store.open_store(self.config)
        try:
            connection = opened.connection()
            selection = connection.execute(
                "SELECT build_id, card_id, backend FROM runtime_selection WHERE selection_id = 1"
            ).fetchone()
            self.assertEqual(tuple(selection), ("build", "card-r9", "HIP/ROCm"))
            self.assertEqual(connection.execute("SELECT count(*) FROM voice_serving").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM voice_serving_parts").fetchone()[0], 0)
        finally:
            opened.close()

    def test_removal_refuses_to_delete_a_command_not_owned_by_this_release(self):
        self.opened.close()
        self.command.write_text("checkout-owned command")
        with self.assertRaisesRegex(removal.RemovalError, "not installed by this release"):
            removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        self.assertEqual(self.command.read_text(), "checkout-owned command")
        self.assertTrue((self.data_root / "releases/2026.10.0/RELEASE.json").exists())

    def test_busy_work_refuses_without_removing_or_modifying_files(self):
        self.opened.connection().execute(
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at)"
            " VALUES ('build-running', 'build', 'matuto-build-test.service', 'build', 'now')")
        self.opened.connection().commit()
        before = (self.data_root / "releases/2026.10.0/RELEASE.json").read_bytes()
        with self.assertRaises(removal.RemovalError) as raised:
            removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        self.assertIn("Stop build build-running in Runs & jobs", str(raised.exception))
        self.assertEqual((self.data_root / "releases/2026.10.0/RELEASE.json").read_bytes(), before)
        self.assertTrue((self.config / "systemd/user/matuto-manager.service").exists())

    def test_confirmation_rechecks_for_a_job_that_appeared_after_preview(self):
        self.opened.close()
        original = removal.blockers
        calls = 0

        def job_appears(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                connection = kwargs["opened_store"].connection()
                connection.execute(
                    "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at)"
                    " VALUES ('late-download', 'download', 'matuto-download-late.service', 'download', 'now')")
                connection.commit()
            return original(**kwargs)

        with mock.patch.object(removal, "blockers", side_effect=job_appears):
            with self.assertRaisesRegex(removal.RemovalError, "Cancel download late-download"):
                removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        self.assertTrue((self.data_root / "releases/2026.10.0/RELEASE.json").exists())
        self.assertTrue((self.config / "systemd/user/matuto-manager.service").exists())

    def test_run_build_download_and_update_blockers_are_reported_together(self):
        connection = self.opened.connection()
        connection.executescript(
            "INSERT INTO runs (run_id, record_kind, record_id, record_label, procedure,"
            " procedure_revision, unit_name, started_at)"
            " VALUES ('verify-run', 'language', 'profile', 'Profile', 'verify', 'r1',"
            " 'matuto-verify-running.service', 'now');"
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at) VALUES"
            " ('running-build', 'build', 'matuto-build-running.service', 'build', 'now');"
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at) VALUES"
            " ('running-download', 'download', 'matuto-download-running.service', 'download', 'now');")
        connection.commit()
        with self.assertRaises(removal.RemovalError) as raised:
            removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        message = str(raised.exception)
        for blocker in ("Stop verify verify-run", "Stop build running-build",
                        "Cancel download running-download"):
            self.assertIn(blocker, message)
        connection.execute("DELETE FROM runs")
        connection.execute("DELETE FROM jobs")
        connection.execute(
            "INSERT INTO update_state (state_id, release_version, phase, updated_at)"
            " VALUES (1, '2026.10.0', '{\"stage\":\"activating\"}', 'now')")
        connection.commit()
        with self.assertRaisesRegex(removal.RemovalError, "an update is installing"):
            removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        self.assertTrue((self.data_root / "releases/2026.10.0/RELEASE.json").exists())
        lock = updater._updater_lock(self.data_root)
        try:
            with self.assertRaisesRegex(removal.RemovalError, "an update is installing"):
                removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        finally:
            os.close(lock)

    def test_active_serving_refuses_without_mutation(self):
        marker = self.data_root / "releases/2026.10.0/RELEASE.json"
        before = marker.read_bytes()
        for state in (admission.AGENTIC, admission.VOICE):
            with self.subTest(state=state), mock.patch.object(
                    serving_state, "read", return_value=serving_state.State(state)):
                with self.assertRaisesRegex(removal.RemovalError, "Stop serving on Overview"):
                    removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
                self.assertEqual(marker.read_bytes(), before)

    def test_unrecognized_systemd_work_state_is_unknown(self):
        with mock.patch.object(serving_state, "read",
                               return_value=serving_state.State(admission.STOPPED)), \
                mock.patch.object(removal.subprocess, "run", return_value=mock.Mock(
                    returncode=0, stdout="matuto-build-ab.service loaded UNKNOWN -\n")):
            with self.assertRaisesRegex(removal.RemovalError, "job state is unknown"):
                removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)

    def test_unknown_serving_or_work_state_refuses(self):
        with mock.patch.object(serving_state, "read",
                               return_value=serving_state.State(admission.SERVING_UNKNOWN)):
            with self.assertRaisesRegex(removal.RemovalError, "serving state is unknown"):
                removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        with mock.patch.object(removal, "_unrecorded_work_units", return_value=None):
            with self.assertRaisesRegex(removal.RemovalError, "job state is unknown"):
                removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        self.assertTrue((self.data_root / "releases/2026.10.0/RELEASE.json").exists())

    def test_kept_weights_inside_release_tree_refuse_removal(self):
        weights = self.data_root / "releases/2026.10.0/weights"
        weights.mkdir()
        model = weights / "model.gguf"
        model.write_bytes(b"weights")
        self.opened.connection().execute("UPDATE artifact_locations SET path = ? WHERE location_id = 'loc'",
                                         (str(weights),))
        self.opened.connection().commit()
        self.opened.close()
        with self.assertRaisesRegex(removal.RemovalError, "overlaps kept Model weights"):
            removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        self.assertEqual(model.read_bytes(), b"weights")
        self.assertTrue((self.config / "systemd/user/matuto-manager.service").exists())

    def test_link_in_a_removal_tree_refuses_before_deleting_anything(self):
        outside = self.home / "outside"
        outside.write_text("keep")
        release_file = self.data_root / "releases/2026.10.0/agents/matuto-lab"
        release_file.unlink()
        release_file.symlink_to(outside)
        self.opened.close()
        with self.assertRaises(removal.RemovalError) as raised:
            removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        self.assertIn("linked removal path", str(raised.exception))
        self.assertEqual(outside.read_text(), "keep")
        self.assertTrue((self.config / "systemd/user/matuto-manager.service").exists())

    def test_swapped_data_parent_cannot_overwrite_kept_progress_file(self):
        self.opened.close()
        kept_progress = self.models / ".removal-state"
        kept_progress.write_text("kept operator data")
        moved_data = self.home / "moved-matuto-data"
        real_mark = removal._mark_progress

        def swap_then_mark(data, stage):
            shutil.move(data, moved_data)
            data.symlink_to(self.models, target_is_directory=True)
            return real_mark(data, stage)

        with mock.patch.object(removal, "_mark_progress", side_effect=swap_then_mark):
            with self.assertRaisesRegex(removal.RemovalError, "unsafe parent path"):
                removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        self.assertEqual(kept_progress.read_text(), "kept operator data")
        self.assertTrue(self.data_root.is_symlink())

    def test_parent_swapped_for_symlink_after_preflight_cannot_delete_outside_files(self):
        self.opened.close()
        outside = self.home / "outside-agents"
        outside.mkdir()
        protected = outside / "keep.txt"
        protected.write_text("outside data")
        real_mark = removal._mark_progress

        def swap_after_preflight(data, stage):
            real_mark(data, stage)
            agents = self.data_root / "releases/2026.10.0/agents"
            shutil.move(agents, self.home / "moved-agents")
            agents.symlink_to(outside, target_is_directory=True)

        with mock.patch.object(removal, "_mark_progress", side_effect=swap_after_preflight):
            with self.assertRaises(removal.RemovalError):
                removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        self.assertEqual(protected.read_text(), "outside data")
        self.assertTrue((self.data_root / "releases/2026.10.0/agents").is_symlink())

    def test_recovery_helper_recovers_when_updater_cleanup_is_interrupted(self):
        self.opened.close()
        removal._mark_progress(self.data_root, "final-step")
        real_remove = removal._remove_tree
        updater_script = self.data_root / "updater/current/agents/matuto/updater.py"

        def interrupt_updater(path, **kwargs):
            if Path(path) == self.data_root / "updater":
                updater_script.unlink()
                raise removal.RemovalError("simulated interruption during updater cleanup")
            return real_remove(path, **kwargs)

        with mock.patch.object(removal, "_remove_tree", side_effect=interrupt_updater):
            with self.assertRaisesRegex(removal.RemovalError, "simulated interruption"):
                removal.finish(config_root=self.config, data_root=self.data_root)
        helper = self.data_root / removal.RECOVERY_NAME
        self.assertTrue(helper.is_file())
        self.assertFalse((self.data_root / "releases").exists())
        self.assertFalse(updater_script.exists())
        recovery_env = os.environ.copy()
        recovery_env.pop("XDG_CONFIG_HOME", None)
        recovery_env.pop("XDG_DATA_HOME", None)
        result = __import__("subprocess").run(
            [sys.executable, str(helper)], capture_output=True, text=True, env=recovery_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.data_root / "releases").exists())
        self.assertFalse((self.data_root / "updater").exists())

    def test_foreign_owned_installation_refuses_without_mutation(self):
        marker = self.data_root / "releases/2026.10.0/RELEASE.json"
        before = marker.read_bytes()
        self.opened.close()
        validate = removal._validate_plan

        def foreign_owned(config, data, connection):
            with mock.patch.object(removal.os, "getuid", return_value=os.getuid() + 1):
                validate(config, data, connection)

        with mock.patch.object(removal, "_validate_plan", side_effect=foreign_owned):
            with self.assertRaises(removal.RemovalError) as raised:
                removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        self.assertIn("owned by another account", str(raised.exception))
        self.assertEqual(marker.read_bytes(), before)

    def test_missing_kept_handoff_ack_stops_before_final_step(self):
        self.opened.close()
        handoff = mock.Mock(side_effect=removal.RemovalError("kept-list acknowledgment missing"))
        with mock.patch.object(removal, "_run_finalizer") as finalizer:
            with self.assertRaisesRegex(removal.RemovalError, "acknowledgment missing"):
                removal.remove(confirm=True, config_root=self.config, data_root=self.data_root,
                               on_kept=handoff)
        handoff.assert_called_once()
        self.assertTrue(handoff.call_args.args[0])
        self.assertEqual(removal._progress(self.data_root), "first-step")
        finalizer.assert_not_called()

    def test_failed_final_step_can_be_retried(self):
        self.opened.close()
        with mock.patch.dict(os.environ, {"FAIL_DISABLE": "1"}):
            with self.assertRaises(removal.RemovalError):
                removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        self.assertTrue((self.data_root / "releases/2026.10.0/agents/matuto/commands/remove.py").exists())
        removal.remove(confirm=True, config_root=self.config, data_root=self.data_root)
        self.assertFalse((self.data_root / "releases").exists())
        self.assertFalse((self.data_root / "updater").exists())
        self.assertFalse((self.data_root / removal.RECOVERY_NAME).exists())


if __name__ == "__main__":
    unittest.main()
