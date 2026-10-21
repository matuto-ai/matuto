"""Bootstrap the signed release in an isolated operator home."""

import contextlib
import hashlib
import http.server
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from matuto import admission, bootstrap, login, release_build, removal, serving_state, sessions, store, updater


class HealthyManagerHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b'<!doctype html><form action="/sign-in">Sign in</form>')

    def log_message(self, *_args):
        pass


class FeedHandler(http.server.BaseHTTPRequestHandler):
    assets = {}
    requests = []

    def do_GET(self):
        self.requests.append(self.path)
        payload = self.assets.get(self.path)
        if payload is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class BootstrapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures = tempfile.TemporaryDirectory(prefix="matuto-bootstrap-fixtures-")
        cls.addClassCleanup(fixtures.cleanup)
        cls.fixture_root = Path(fixtures.name)
        cls.repo = Path(__file__).resolve().parents[3]
        cls.commit = subprocess.check_output(
            ["git", "-C", str(cls.repo), "rev-parse", "HEAD"], text=True).strip()
        archive_path = cls.fixture_root / "matuto.release"
        cls.release = release_build.build_release(
            cls.commit, "2026.10.0", archive_path, repository=cls.repo)
        cls.archive = archive_path.read_bytes()
        cls.migration_archives = {}

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="matuto-bootstrap-test-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.data = self.home / "data" / "matuto"
        self.config.mkdir(mode=0o700)
        self.data.parent.mkdir(mode=0o700)
        self.env = {**os.environ, "HOME": str(self.home),
                    "XDG_CONFIG_HOME": str(self.config),
                    "XDG_DATA_HOME": str(self.data.parent)}
        self.bin = self.home / "bin"
        self.bin.mkdir(mode=0o700)
        self.system_log = self.home / "systemctl.log"
        systemctl = self.bin / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
            "case \"$*\" in\n"
            "  '--user daemon-reload')\n"
            "    if [ -n \"$FAIL_RELOAD_ONCE\" ] && [ ! -e \"$FAIL_RELOAD_ONCE\" ]; then\n"
            "      touch \"$FAIL_RELOAD_ONCE\"; exit 1; fi\n"
            "    if [ -n \"$SYSTEMD_UNIT_FILE\" ] && [ -n \"$SYSTEMD_LOADED_UNIT\" ]; then\n"
            "      cp \"$SYSTEMD_UNIT_FILE\" \"$SYSTEMD_LOADED_UNIT\"; fi ;;\n"
            "  '--user enable matuto-manager.service')\n"
            "    if [ -n \"$FAIL_ENABLE_ONCE\" ] && [ ! -e \"$FAIL_ENABLE_ONCE\" ]; then\n"
            "      touch \"$FAIL_ENABLE_ONCE\"; exit 1; fi\n"
            "    printf enabled > \"$SYSTEMD_ENABLED_STATE\" ;;\n"
            "  '--user disable matuto-manager.service') printf disabled > \"$SYSTEMD_ENABLED_STATE\" ;;\n"
            "esac\n")
        systemctl.chmod(0o700)
        self.enabled_state = self.home / "manager-enabled"
        self.env.update(PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        SYSTEMCTL_LOG=str(self.system_log),
                        SYSTEMD_ENABLED_STATE=str(self.enabled_state))
        self.keys = []
        self.servers = []
        FeedHandler.requests = []

    def _key(self, name):
        path = self.home / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
                       check=True, capture_output=True)
        self.keys.append((path, Path(f"{path}.pub").read_bytes()))
        return path, self.keys[-1][1]

    def _feed(self, key_path, public_key, *, number=1, archive=None, entry=None,
              extra_entries=()):
        archive = self.archive if archive is None else archive
        entry = ({**self.release, "size": len(archive),
                  "sha256": hashlib.sha256(archive).hexdigest()} if entry is None else entry)
        entries = [entry, *extra_entries]
        document = json.dumps({"number": number, "releases": entries}).encode()
        list_path = self.home / f"list-{number}.json"
        list_path.write_bytes(document)
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key_path),
                        "-n", "matuto-release", str(list_path)], check=True, capture_output=True)
        FeedHandler.assets = {"/release-list.json": document,
                              "/release-list.json.sig": Path(f"{list_path}.sig").read_bytes(),
                              **{f"/matuto-{candidate['version']}.release": archive
                                 for candidate in entries}}
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FeedHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/", public_key

    def _migration_release(self, version, *, fails=False, schema_version=None):
        schema_version = store.SCHEMA_VERSION + 1 if schema_version is None else schema_version
        fixture = (version, fails, schema_version)
        if fixture in self.migration_archives:
            return self.migration_archives[fixture]
        repo = self.fixture_root / f"fixture-{version}-{fails}-{schema_version}"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email",
                        "fixture@example.invalid"], check=True)
        shutil.copytree(self.repo / "agents", repo / "agents",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        target_store = repo / "agents/matuto/store.py"
        source = target_store.read_text()
        source = source.replace(f"SCHEMA_VERSION = {store.SCHEMA_VERSION}",
                                f"SCHEMA_VERSION = {schema_version}", 1)
        if schema_version > store.SCHEMA_VERSION:
            migration = ("\n\ndef _bootstrap_migration(connection):\n"
                         "    connection.execute('CREATE TABLE bootstrap_probe (value TEXT)')\n")
            migration += ("    raise RuntimeError('fixture migration failed')\n" if fails else
                          "    connection.execute(\"INSERT INTO bootstrap_probe VALUES ('migrated')\")\n")
            source += migration + f"\nMIGRATIONS[{store.SCHEMA_VERSION}] = _bootstrap_migration\n"
        target_store.write_text(source)
        for name in ("wq.py", "wq.sh", "LICENSE", "docs/MATUTO_LAB.md", "docs/MATUTO_MANAGER.md",
                     "docs/RELEASING.md"):
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.repo / name, target)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture migration"], check=True)
        commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        output = repo / "matuto.release"
        entry = release_build.build_release(commit, version, output, repository=repo,
                                            changes_database=True)
        self.migration_archives[fixture] = output.read_bytes(), entry
        return self.migration_archives[fixture]

    def _run(self, feed, key_path, key):
        fingerprint = bootstrap._fingerprint(key)
        script = Path(__file__).resolve().parents[2] / "matuto-bootstrap"
        return subprocess.run([str(script), "--feed", feed, "--trusted-key", f"{key_path}.pub"],
                              input=fingerprint + "\n", env=self.env, capture_output=True,
                              text=True, timeout=60)

    def test_empty_home_bootstraps_and_same_trust_rerun_changes_nothing(self):
        key_path, key = self._key("release-key")
        feed, _ = self._feed(key_path, key)
        result = self._run(feed, key_path, key)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(bootstrap._fingerprint(key), result.stdout)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertTrue((self.data / "updater/current/agents/matuto/updater.py").is_file())
        command = self.home / ".local/bin/matuto-manager"
        self.assertEqual(command.stat().st_mode & 0o777, 0o755)
        self.assertIn("releases/current/agents/matuto/cli.py", command.read_text())
        help_result = subprocess.run([str(command), "--help"], env=self.env,
                                     capture_output=True, text=True, check=False)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("usage: matuto-manager", help_result.stdout)
        api_key = self.config / "ai-agents-sandbox/pi-local/api-key"
        self.assertEqual(api_key.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.config / "ai-agents-sandbox/matuto-manager/login.json").exists())
        state = json.loads(store.read_update_state(store.store_path(self.config))["phase"])
        self.assertEqual(state["trusted_key"], key.decode().strip())
        self.assertEqual(state["release_address"], feed)
        before_store = hashlib.sha256(store.store_path(self.config).read_bytes()).digest()
        before_key = api_key.read_bytes()
        rerun = self._run(feed, key_path, key)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual(hashlib.sha256(store.store_path(self.config).read_bytes()).digest(), before_store)
        self.assertEqual(api_key.read_bytes(), before_key)
        self.assertEqual(self.system_log.read_text().splitlines(), [
            "--user daemon-reload", "--user enable matuto-manager.service"])
        unit = self.config / "systemd/user/matuto-manager.service"
        self.assertIn("WantedBy=default.target", unit.read_text())
        self.assertIn("StartLimitIntervalSec=60", unit.read_text())
        self.assertIn("StartLimitBurst=3", unit.read_text())
        self.assertIn("releases/current/agents/matuto/cli.py", unit.read_text())
        self.assertNotIn("router", unit.read_text())
        applications = self.data.parent / "applications"
        expected_actions = {"matuto.desktop": ("Matuto", "open"),
                            "repair-matuto.desktop": ("Repair Matuto", "repair"),
                            "update-matuto.desktop": ("Update Matuto", "update")}
        for filename, (name, action) in expected_actions.items():
            entry = (applications / filename).read_text()
            self.assertIn(f"Name={name}", entry)
            self.assertIn(f"updater/current/agents/matuto/updater.py\" {action}", entry)
            self.assertNotIn("releases/2026.10.0", entry)
        stable_updater = self.data / "updater/current/agents/matuto/updater.py"
        expected_updater = (updater.active_release(self.data) / "agents/matuto/updater.py").read_bytes()
        stable_updater.write_bytes(b"broken updater")
        repaired = self._run(feed, key_path, key)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(stable_updater.read_bytes(), expected_updater)

        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)}), \
                mock.patch.object(updater, "apply", return_value=updater.UpdateResult("2026.10.0")) as apply:
            self.assertEqual(updater.main([]), 0)
        apply.assert_called_once_with(feed, key.decode().strip().encode())
        with (mock.patch.object(updater, "_error_page", return_value=0),
              mock.patch.object(updater, "_open_update_screen", return_value=0) as open_updates,
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(updater.main(["update"]), 0)
        open_updates.assert_called_once()
        apply.assert_called_once_with(feed, key.decode().strip().encode())
        self.assertEqual(self.enabled_state.read_text(), "enabled")

    def test_bootstrap_rerun_preserves_a_disabled_manager_and_desktop_files(self):
        key_path, key = self._key("release-key")
        feed, _ = self._feed(key_path, key)
        self.assertEqual(self._run(feed, key_path, key).returncode, 0)
        systemctl = self.bin / "systemctl"
        subprocess.run([str(systemctl), "--user", "disable", "matuto-manager.service"],
                       env=self.env, check=True)
        before = self.system_log.read_text().splitlines()
        unit = self.config / "systemd/user/matuto-manager.service"
        entries = sorted((self.data.parent / "applications").glob("*.desktop"))
        contents = {path: path.read_bytes() for path in [unit, *entries]}
        command = self.home / ".local/bin/matuto-manager"
        command.chmod(0o644)

        result = self._run(feed, key_path, key)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.enabled_state.read_text(), "disabled")
        self.assertEqual(self.system_log.read_text().splitlines(), before)
        self.assertEqual({path: path.read_bytes() for path in contents}, contents)
        self.assertEqual(command.stat().st_mode & 0o777, 0o755)

    def test_removed_disabled_manager_unit_is_recreated_without_reenabling_login_start(self):
        key_path, key = self._key("release-key")
        feed, _ = self._feed(key_path, key)
        self.assertEqual(self._run(feed, key_path, key).returncode, 0)
        systemctl = self.bin / "systemctl"
        subprocess.run([str(systemctl), "--user", "disable", "matuto-manager.service"],
                       env=self.env, check=True)
        unit = self.config / "systemd/user/matuto-manager.service"
        unit.unlink()
        before = self.system_log.read_text().splitlines()

        result = self._run(feed, key_path, key)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(unit.is_file())
        self.assertEqual(self.enabled_state.read_text(), "disabled")
        self.assertEqual(self.system_log.read_text().splitlines(), before + [
            "--user daemon-reload"])

    def test_previously_bootstrapped_release_gets_its_first_manager_unit_enabled(self):
        releases = self.data / "releases"
        release = releases / "2026.10.0"
        release.mkdir(parents=True, mode=0o700)
        (releases / "current").symlink_to("2026.10.0")
        opened = store.open_store(self.config)
        store.write_update_state(opened.path, "2026.10.0", "complete",
                                 details={"trusted_key": "fixture-key",
                                          "release_address": "https://fixture.invalid"})
        opened.close()

        with mock.patch.dict(os.environ, self.env):
            bootstrap._install_login_and_menu(self.data, self.config)

        self.assertEqual(self.enabled_state.read_text(), "enabled")
        self.assertIn("--user enable matuto-manager.service",
                      self.system_log.read_text().splitlines())

    def test_bootstrap_retries_a_failed_daemon_reload_before_returning_success(self):
        key_path, key = self._key("release-key")
        feed, _ = self._feed(key_path, key)
        self.assertEqual(self._run(feed, key_path, key).returncode, 0)
        unit = self.config / "systemd/user/matuto-manager.service"
        unit.write_text("stale unit contents")
        loaded = self.home / "systemd-loaded.unit"
        loaded.write_text("previous loaded unit")
        self.env.update(FAIL_RELOAD_ONCE=str(self.home / "reload-failed"),
                        SYSTEMD_UNIT_FILE=str(unit), SYSTEMD_LOADED_UNIT=str(loaded))

        interrupted = self._run(feed, key_path, key)
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertNotEqual(loaded.read_bytes(), unit.read_bytes())
        opened = store.open_store(self.config)
        try:
            updater._record_state(opened.connection(), "2026.10.0", "preparing")
        finally:
            opened.close()
        state = json.loads(store.read_update_state(store.store_path(self.config))["phase"])
        self.assertTrue(state["manager_daemon_reload_pending"])

        resumed = self._run(feed, key_path, key)

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(loaded.exists(), self.system_log.read_text())
        self.assertEqual(loaded.read_bytes(), unit.read_bytes())
        self.assertEqual(self.enabled_state.read_text(), "enabled")
        self.assertEqual(self.system_log.read_text().splitlines().count("--user daemon-reload"), 3)

    def test_desktop_exec_escapes_percent_field_codes_in_path_arguments(self):
        script = self.home / "operator%f" / "updater.py"
        entry = bootstrap._desktop_entry("Matuto", script, "open")
        self.assertIn('operator%%f', entry)
        exec_line = next(line for line in entry.splitlines() if line.startswith("Exec="))
        self.assertTrue(exec_line.endswith('updater.py" open'))

    def test_interrupted_bootstrap_finishes_enabling_the_manager(self):
        key_path, key = self._key("release-key")
        feed, _ = self._feed(key_path, key)
        self.env["FAIL_ENABLE_ONCE"] = str(self.home / "enable-failed")

        interrupted = self._run(feed, key_path, key)
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertTrue((self.config / "systemd/user/matuto-manager.service").is_file())
        self.assertFalse(self.enabled_state.exists())
        opened = store.open_store(self.config)
        try:
            updater._record_state(opened.connection(), "2026.10.0", "preparing")
        finally:
            opened.close()
        state = json.loads(store.read_update_state(store.store_path(self.config))["phase"])
        self.assertTrue(state["manager_enable_pending"])

        resumed = self._run(feed, key_path, key)

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.enabled_state.read_text(), "enabled")
        self.assertEqual(self.system_log.read_text().splitlines().count(
            "--user enable matuto-manager.service"), 2)

    def test_cli_open_routes_to_the_shared_recovery_action(self):
        from matuto import cli

        command = mock.Mock()
        command.run.return_value = 7
        with mock.patch.object(cli, "load_item", return_value=command) as load:
            self.assertEqual(cli.main(["matuto", "open"]), 7)
        load.assert_called_once_with("commands", "open")
        command.run.assert_called_once_with([])

    def test_bootstrapped_updater_installs_local_release_without_network_access(self):
        key_path, key = self._key("release-key")
        manager = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), HealthyManagerHandler)
        thread = threading.Thread(target=manager.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(manager.server_close)
        self.addCleanup(manager.shutdown)

        first_archive, first_entry = self._migration_release(
            "2026.10.0", schema_version=store.SCHEMA_VERSION)
        first_feed, _ = self._feed(key_path, key, archive=first_archive, entry=first_entry)
        bootstrapped = self._run(first_feed, key_path, key)
        self.assertEqual(bootstrapped.returncode, 0, bootstrapped.stderr)

        stable_updater = updater.independent_updater_script(self.data)
        script = stable_updater.read_text()
        manager_url = f"http://127.0.0.1:{manager.server_port}/"
        stable_updater.write_text(script.replace(
            'MANAGER_URL = "http://127.0.0.1:8890/"', f"MANAGER_URL = {manager_url!r}", 1))
        systemctl = self.bin / "systemctl"
        manager_pid = self.home / "manager.pid"
        self.env["SYSTEMCTL_PID_FILE"] = str(manager_pid)
        systemctl.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
            "case \"$2\" in\n"
            "  is-active) printf inactive ;;\n"
            "  show) cat \"$SYSTEMCTL_PID_FILE\" ;;\n"
            "  start|restart)\n"
            f"    {sys.executable} -c 'import time; time.sleep(120)' "
            "\"$(realpath \"$XDG_DATA_HOME/matuto/releases/current/agents/matuto/cli.py\")\" serve "
            ">/dev/null 2>&1 &\n"
            "    echo $! > \"$SYSTEMCTL_PID_FILE\" ;;\n"
            "esac\n")
        systemctl.chmod(0o700)

        def stop_test_manager():
            if manager_pid.exists():
                try:
                    os.kill(int(manager_pid.read_text()), signal.SIGTERM)
                except (OSError, ValueError):
                    pass
        self.addCleanup(stop_test_manager)

        local_archive, local_entry = self._migration_release(
            "2026.10.1", schema_version=store.SCHEMA_VERSION)
        release_file = self.home / "matuto-2026.10.1.release"
        release_file.write_bytes(local_archive)
        self._feed(key_path, key, number=2, archive=local_archive, entry=local_entry)
        list_file = self.home / "list-2.json"
        requests_before_install = list(FeedHandler.requests)
        cli = updater.active_release(self.data) / "agents/matuto/cli.py"

        installed = subprocess.run(
            [sys.executable, "-B", str(cli), "install-from-file",
             str(release_file), str(list_file)], env=self.env, capture_output=True,
            text=True, timeout=120)

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")
        self.assertEqual(os.readlink(self.data / "updater/current"), "versions/2026.10.1")
        self.assertEqual(FeedHandler.requests, requests_before_install)
        self.assertEqual(bootstrap.trusted_inputs(self.config),
                         (first_feed, key.decode().strip().encode()))

    def test_bootstrap_rerun_repairs_the_active_release_without_upgrading_it(self):
        key_path, key = self._key("release-key")
        first_feed, _ = self._feed(key_path, key, number=1)
        self.assertEqual(self._run(first_feed, key_path, key).returncode, 0)
        newer_entry = {**self.release, "version": "2026.10.1"}
        newer_feed, _ = self._feed(key_path, key, number=2, extra_entries=[newer_entry])

        result = self._run(newer_feed, key_path, key)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertFalse((self.data / "releases/2026.10.1").exists())
        state = json.loads(store.read_update_state(store.store_path(self.config))["phase"])
        self.assertEqual(state["stage"], "complete")
        self.assertEqual(state["list_number"], 2)

    def test_new_trusted_key_replaces_the_old_key_only_after_verification(self):
        first_path, first_key = self._key("first-release-key")
        first_feed, _ = self._feed(first_path, first_key, number=1)
        self.assertEqual(self._run(first_feed, first_path, first_key).returncode, 0)
        second_path, second_key = self._key("second-release-key")
        withdrawn_entry = {**self.release, "withdrawn": True}
        newer_entry = {**self.release, "version": "2026.10.1"}
        second_feed, _ = self._feed(second_path, second_key, number=2,
                                    entry=withdrawn_entry, extra_entries=[newer_entry])
        stable = self.data / "updater/current/agents/matuto/updater.py"
        stable.write_bytes(b"broken updater")
        before = store.store_path(self.config).read_bytes()
        with mock.patch.object(bootstrap.releases, "read_list",
                               side_effect=bootstrap.releases.ReleaseError("fixture rejected list")), \
                self.assertRaisesRegex(bootstrap.BootstrapError, "fixture rejected list"):
            bootstrap.install(second_feed, second_key,
                              confirm_fingerprint=bootstrap._fingerprint(second_key),
                              config_root=self.config, data_root=self.data)
        self.assertEqual(store.store_path(self.config).read_bytes(), before)
        self.assertEqual(stable.read_bytes(), b"broken updater")
        result = self._run(second_feed, second_path, second_key)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertEqual(stable.read_bytes(),
                         (updater.active_release(self.data) / "agents/matuto/updater.py").read_bytes())
        state = json.loads(store.read_update_state(store.store_path(self.config))["phase"])
        self.assertEqual(state["trusted_key"], second_key.decode().strip())
        self.assertEqual(state["release_address"], second_feed)

    def test_interrupted_store_activation_blocks_writers_and_recovers_on_rerun(self):
        old = store.open_store(self.config)
        old.connection().execute(
            "INSERT INTO store_meta (key, value) VALUES ('bootstrap_recovery_probe', 'keep me')")
        old.close()
        archive, entry = self._migration_release("2026.10.1")
        key_path, key = self._key("release-key")
        feed, _ = self._feed(key_path, key, archive=archive, entry=entry)
        with mock.patch.object(updater, "_atomic_current", side_effect=OSError("power loss")):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "power loss"):
                bootstrap.install(feed, key, confirm_fingerprint=bootstrap._fingerprint(key),
                                  config_root=self.config, data_root=self.data)

        state = json.loads(store.read_update_state(store.store_path(self.config))["phase"])
        self.assertEqual(state["stage"], "activating")
        with self.assertRaisesRegex(store.StoreError, "newer than"):
            store.open_store(self.config)
        result = self._run(feed, key_path, key)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")
        state = json.loads(store.read_update_state(store.store_path(self.config))["phase"])
        self.assertEqual(state["stage"], "complete")
        connection = store._readonly_connect(store.store_path(self.config))
        try:
            self.assertEqual(connection.execute(
                "SELECT value FROM store_meta WHERE key = 'bootstrap_recovery_probe'").fetchone()[0],
                "keep me")
        finally:
            connection.close()

    def test_interrupted_empty_store_creation_uses_verified_release_schema(self):
        archive, entry = self._migration_release(
            "2026.10.0", schema_version=store.SCHEMA_VERSION - 1)
        key_path, key = self._key("release-key")
        feed, _ = self._feed(key_path, key, archive=archive, entry=entry)
        store._private_directory(store.store_path(self.config).parent)
        self.assertTrue(store._create_file(store.store_path(self.config)))

        result = self._run(feed, key_path, key)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(store.read_schema_version(store.store_path(self.config)),
                         store.SCHEMA_VERSION - 1)
        state = json.loads(store.read_update_state(store.store_path(self.config))["phase"])
        self.assertEqual(state["stage"], "complete")

    def test_migration_holds_writer_lease_until_release_activation(self):
        old = store.open_store(self.config)
        old.connection().execute(
            "INSERT INTO store_meta (key, value) VALUES ('bootstrap_concurrent_probe', 'keep me')")
        old.close()
        archive, entry = self._migration_release("2026.10.1")
        key_path, key = self._key("release-key")
        feed, _ = self._feed(key_path, key, archive=archive, entry=entry)
        activate = updater._atomic_current
        lease_was_held = []
        writer_was_blocked = []

        def check_activation(data_root, version):
            try:
                descriptor = store.acquire_update_lock(self.config)
            except store.StoreError:
                lease_was_held.append(True)
            else:
                lease_was_held.append(False)
                os.close(descriptor)
            try:
                opened = store.open_store(self.config)
            except store.StoreError:
                writer_was_blocked.append(True)
            else:
                writer_was_blocked.append(False)
                opened.connection().execute(
                    "INSERT INTO store_meta (key, value) VALUES ('raced_writer', 'must survive')")
                opened.close()
            return activate(data_root, version)

        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(updater, "_atomic_current", side_effect=check_activation):
            result = bootstrap.install(feed, key, confirm_fingerprint=bootstrap._fingerprint(key),
                                       config_root=self.config, data_root=self.data)

        self.assertEqual(result[0], "2026.10.1")
        self.assertEqual(lease_was_held, [True])
        self.assertEqual(writer_was_blocked, [True])
        connection = store._readonly_connect(store.store_path(self.config))
        try:
            values = dict(connection.execute(
                "SELECT key, value FROM store_meta WHERE key IN"
                " ('bootstrap_concurrent_probe', 'raced_writer')"))
        finally:
            connection.close()
        self.assertEqual(values, {"bootstrap_concurrent_probe": "keep me"})

    def test_unconfirmed_key_installs_nothing(self):
        trusted_path, trusted_key = self._key("trusted")
        feed, _ = self._feed(trusted_path, trusted_key)
        wrong_confirmation = subprocess.run(
            [str(Path(__file__).resolve().parents[2] / "matuto-bootstrap"), "--feed", feed,
             "--trusted-key", f"{trusted_path}.pub"], input="not-the-fingerprint\n",
            env=self.env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(wrong_confirmation.returncode, 0)
        self.assertFalse((self.data / "releases").exists())
        self.assertFalse(store.store_path(self.config).exists())

    def test_newer_release_snapshots_and_migrates_kept_store_without_touching_credentials(self):
        old = store.open_store(self.config)
        old.connection().execute(
            "INSERT INTO store_meta (key, value) VALUES ('bootstrap_probe', 'preserve me')")
        old.close()
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)}):
            login.create_login("operator", "bootstrap-secret", "bootstrap-secret")
            sessions.start_session("operator", "bootstrap-secret", config_root=self.config)
        login_file = self.config / "ai-agents-sandbox/matuto-manager/login.json"
        login_before = login_file.read_bytes()
        key_path_file = self.config / "ai-agents-sandbox/pi-local/api-key"
        key_path_file.parent.mkdir(parents=True, mode=0o700)
        key_path_file.write_bytes(b"existing-router-key\\n")
        key_path_file.chmod(0o600)
        key_before = key_path_file.read_bytes()
        sessions_before = store.read_sessions_at(store.store_path(self.config))
        archive, entry = self._migration_release("2026.10.1")
        key_path, key = self._key("release-key")
        feed, _ = self._feed(key_path, key, number=1, archive=archive, entry=entry)

        result = self._run(feed, key_path, key)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(store.read_schema_version(store.store_path(self.config)),
                         store.SCHEMA_VERSION + 1)
        migrated = store._readonly_connect(store.store_path(self.config))
        try:
            self.assertEqual(migrated.execute(
                "SELECT value FROM store_meta WHERE key = 'bootstrap_probe'").fetchone()[0],
                "preserve me")
            self.assertEqual(migrated.execute(
                "SELECT value FROM bootstrap_probe").fetchone()[0], "migrated")
            self.assertEqual(migrated.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
        finally:
            migrated.close()
        snapshot_path = updater.snapshot.snapshot_path(store.store_path(self.config))
        self.assertEqual(updater.snapshot._inspect(snapshot_path)[1], "2026.10.1")
        self.assertEqual(login_file.read_bytes(), login_before)
        self.assertEqual(key_path_file.read_bytes(), key_before)
        self.assertEqual(store.read_sessions_at(store.store_path(self.config)), sessions_before)

    def test_failed_staged_migration_keeps_kept_schema_and_does_not_install(self):
        old = store.open_store(self.config)
        old.connection().execute(
            "INSERT INTO store_meta (key, value) VALUES ('bootstrap_probe', 'preserve me')")
        old.close()
        archive, entry = self._migration_release("2026.10.1", fails=True)
        key_path, key = self._key("release-key")
        feed, _ = self._feed(key_path, key, archive=archive, entry=entry)

        result = self._run(feed, key_path, key)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture migration failed", result.stderr)
        self.assertEqual(store.read_schema_version(store.store_path(self.config)),
                         store.SCHEMA_VERSION)
        unchanged = store._readonly_connect(store.store_path(self.config))
        try:
            self.assertEqual(unchanged.execute(
                "SELECT value FROM store_meta WHERE key = 'bootstrap_probe'").fetchone()[0],
                "preserve me")
        finally:
            unchanged.close()
        self.assertFalse((self.data / "releases/current").exists())
        self.assertFalse((self.config / "ai-agents-sandbox/pi-local/api-key").exists())
        phase = store.read_update_state(store.store_path(self.config))["phase"]
        state = json.loads(phase) if phase else {}
        self.assertNotIn("trusted_key", state)

    def test_removed_bootstrap_install_keeps_data_for_a_later_bootstrap(self):
        key_path, key = self._key("release-key")
        feed, _ = self._feed(key_path, key)
        first = self._run(feed, key_path, key)
        self.assertEqual(first.returncode, 0, first.stderr)
        systemctl = self.bin / "systemctl"
        script = systemctl.read_text()
        systemctl.write_text(script.replace(
            "#!/bin/sh\n", "#!/bin/sh\n"
            "if [ \"$2\" = is-active ]; then echo inactive; exit 3; fi\n"
            "if [ \"$2\" = list-units ]; then "
            "if [ -f \"$SYSTEMD_LIST_FILE\" ]; then cat \"$SYSTEMD_LIST_FILE\"; "
            "else echo '0 units files listed.'; fi; exit 0; fi\n", 1))
        systemctl.chmod(0o700)
        systemd_list = self.home / "systemd-list"
        self.env["SYSTEMD_LIST_FILE"] = str(systemd_list)
        systemd_run = self.bin / "systemd-run"
        systemd_run.write_text(
            "#!/bin/sh\nwhile [ $# -gt 0 ]; do case \"$1\" in --*) shift;; *) break;; esac; done\n"
            "unset XDG_CONFIG_HOME XDG_DATA_HOME\nexec \"$@\"\n")
        systemd_run.chmod(0o700)

        opened = store.open_store(self.config)
        opened.connection().execute(
            "INSERT INTO store_meta (key, value) VALUES ('removal_probe', 'keep me')")
        opened.close()
        manager_dir = store.store_directory(self.config)
        login = manager_dir / "login.json"
        login.write_bytes(b"login and sessions stay")
        key_file = self.config / "ai-agents-sandbox/pi-local/api-key"
        key_before = key_file.read_bytes()

        def remove_fixture():
            with mock.patch.dict(os.environ, self.env):
                return removal.remove(confirm=True, config_root=self.config, data_root=self.data)

        for state in (admission.AGENTIC, admission.VOICE, admission.SERVING_UNKNOWN):
            with self.subTest(serving_state=state), mock.patch.object(
                    serving_state, "read", return_value=serving_state.State(state)):
                with self.assertRaises(removal.RemovalError):
                    remove_fixture()

        opened = store.open_store(self.config)
        connection = opened.connection()
        connection.execute(
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at)"
            " VALUES ('bootstrap-build', 'build', 'matuto-build-ab.service', 'build', 'now')")
        connection.commit()
        opened.close()
        with self.assertRaisesRegex(removal.RemovalError, "Stop build bootstrap-build"):
            remove_fixture()
        opened = store.open_store(self.config)
        opened.connection().execute("DELETE FROM jobs WHERE job_id = 'bootstrap-build'")
        opened.close()

        systemd_list.write_text("matuto-build-ab.service loaded UNKNOWN -\n")
        with mock.patch.object(serving_state, "read",
                               return_value=serving_state.State(admission.STOPPED)):
            with self.assertRaisesRegex(removal.RemovalError, "job state is unknown"):
                remove_fixture()
        systemd_list.unlink()

        validate = removal._validate_plan

        def foreign_owned(config, data, connection):
            with mock.patch.object(removal.os, "getuid", return_value=os.getuid() + 1):
                validate(config, data, connection)

        with mock.patch.object(removal, "_validate_plan", side_effect=foreign_owned):
            with self.assertRaisesRegex(removal.RemovalError, "owned by another account"):
                remove_fixture()

        with mock.patch.dict(os.environ, self.env):
            removal._mark_progress(self.data, "final-step")
        release_cli = self.data / "releases/current/agents/matuto/cli.py"
        updater_cli = self.data / "updater/current/agents/matuto/updater.py"

        def interrupt_release(root):
            release_cli.unlink()
            updater_cli.unlink()
            raise removal.RemovalError("interrupted release cleanup")

        with mock.patch.dict(os.environ, self.env), mock.patch.object(
                removal, "_finish_release_tree", side_effect=interrupt_release):
            with self.assertRaisesRegex(removal.RemovalError, "interrupted release cleanup"):
                removal.finish(config_root=self.config, data_root=self.data)

        recovery = self.data / removal.RECOVERY_NAME
        recovery_env = self.env.copy()
        recovery_env.pop("XDG_CONFIG_HOME", None)
        recovery_env.pop("XDG_DATA_HOME", None)
        removed = subprocess.run([sys.executable, str(recovery)], env=recovery_env,
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse((self.data / removal.RECOVERY_NAME).exists())
        self.assertEqual(login.read_bytes(), b"login and sessions stay")
        self.assertEqual(key_file.read_bytes(), key_before)
        self.assertTrue(store.store_path(self.config).is_file())

        later = self._run(feed, key_path, key)
        self.assertEqual(later.returncode, 0, later.stderr)
        self.assertEqual((self.data / "releases/current").resolve().name, "2026.10.0")
        self.assertEqual(login.read_bytes(), b"login and sessions stay")
        self.assertEqual(key_file.read_bytes(), key_before)
        opened = store.open_store(self.config)
        try:
            self.assertEqual(opened.connection().execute(
                "SELECT value FROM store_meta WHERE key = 'removal_probe'"
            ).fetchone()[0], "keep me")
        finally:
            opened.close()

    def test_kept_store_newer_than_release_is_refused_before_install(self):
        opened = store.open_store(self.config)
        opened.connection().execute(
            "UPDATE store_meta SET value = ? WHERE key = 'schema_version'",
            (str(store.SCHEMA_VERSION + 1),))
        opened.close()
        key_path, key = self._key("release-key")
        feed, _ = self._feed(key_path, key)
        result = self._run(feed, key_path, key)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("newer than release", result.stderr)
        self.assertEqual(store.read_schema_version(store.store_path(self.config)),
                         store.SCHEMA_VERSION + 1)
        self.assertFalse((self.data / "releases").exists())


if __name__ == "__main__":
    unittest.main()
