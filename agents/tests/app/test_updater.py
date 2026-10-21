"""Independent release updates through a loopback feed and disposable home."""

import base64
import hashlib
import http.client
import http.server
import json
import os
import shutil
import signal
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from matuto import admission, bootstrap, cli, login, release_build, releases, runner, sessions, store, updater


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


class UpdaterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures = tempfile.TemporaryDirectory(prefix="matuto-updater-fixtures-")
        cls.addClassCleanup(fixtures.cleanup)
        cls.fixture_root = Path(fixtures.name)
        cls.archives = {}
        # Seed real credentials once; every test gets its own closed-store copy.
        cls.seed_config = cls.fixture_root / "config"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(cls.seed_config)}):
            opened = store.open_store(cls.seed_config)
            opened.connection().execute(
                "INSERT INTO store_meta (key, value) VALUES ('updater_preservation_probe', 'keep me')")
            opened.close()
            login.create_login("operator", "updater-session-secret", "updater-session-secret")
            cls.session_token, _expiry = sessions.start_session(
                "operator", "updater-session-secret", config_root=cls.seed_config)

    def setUp(self):
        FeedHandler.requests = []
        self.home = Path(tempfile.mkdtemp(prefix="matuto-updater-test-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.data = self.home / "data" / "matuto"
        self.data.parent.mkdir(mode=0o700)
        shutil.copytree(self.seed_config, self.config)
        self.config.chmod(0o700)
        self.environment = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.config), "XDG_DATA_HOME": str(self.data.parent)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store_path = store.store_path(self.config)
        self.initial_schema = store.SCHEMA_VERSION
        self.target_schema = self.initial_schema + 1
        self.expected_sessions = store.read_sessions_at(self.store_path)
        key_dir = self.config / "ai-agents-sandbox" / "pi-local"
        key_dir.mkdir(parents=True, mode=0o700)
        self.api_key = key_dir / "api-key"
        self.api_key.write_bytes(b"router-api-key\n")
        os.chmod(self.api_key, 0o600)
        self.login = self.config / "ai-agents-sandbox" / "matuto-manager" / "login.json"
        self.initial_login = self.login.read_bytes()
        self._initial_release()
        self._manager_standin()
        self._fake_systemctl()
        self.addCleanup(self._stop_fake_manager)
        self.manager_url = f"http://127.0.0.1:{self.manager_port}/"
        self.manager_host = updater.MANAGER_HOST
        self.serving_reader = lambda: (type("Serving", (), {"state": "stopped"})(), [])

    def _active_session_valid(self):
        agents = updater.active_release(self.data) / "agents"
        script = "from matuto import sessions; import sys; " \
                 "sys.exit(0 if sessions.session_is_valid(sys.argv[1], config_root=sys.argv[2]) else 1)"
        env = {**os.environ, "PYTHONPATH": str(agents)}
        result = subprocess.run([sys.executable, "-B", "-c", script,
                                 self.session_token, str(self.config)],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return True

    def _existing_session_reaches_manager(self):
        address = urllib.parse.urlsplit(self.manager_url)
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=2)
        try:
            connection.request("GET", "/", headers={
                "Host": self.manager_host, "Cookie": f"matuto_session={self.session_token}"})
            response = connection.getresponse()
            page = response.read(1 << 20).decode("utf-8", "replace").lower()
            self.assertEqual(response.status, 200)
            self.assertIn("<!doctype html>", page)
            self.assertNotIn("sign in", page)
        finally:
            connection.close()

    def _manager_standin(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.manager_port = probe.getsockname()[1]
        self.manager_routes = self.home / "manager-route-root"
        route_dir = self.manager_routes / "routes"
        route_dir.mkdir(parents=True)
        # The shared HTTP gate now needs the real signed-out routes for health checks.
        source = Path(__file__).resolve().parents[2] / "matuto"
        for name in ("sign_in.py", "create_login.py", "auth_script.py"):
            shutil.copy2(source / "routes" / name, route_dir / name)
        shutil.copytree(source / "static", self.manager_routes / "static")
        (route_dir / "home.py").write_text(
            "from http.cookies import SimpleCookie\n"
            "from matuto import login,sessions\n"
            "PATH='/'\n"
            "def handle(request):\n"
            " cookie=SimpleCookie(); cookie.load(request.headers.get('Cookie',''))\n"
            " session=cookie.get('matuto_session')\n"
            " if session and sessions.session_is_valid(session.value):\n"
            "  return 200,'<!doctype html><title>Matuto</title>','text/html'\n"
            " page='Sign in' if login.login_generation() else 'Create login'\n"
            " return 200,'<!doctype html><form>'+page+'</form>','text/html'\n")
        self.manager_script = self.home / "manager-stand-in.py"
        self.manager_script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "manager=Path(sys.argv[1]).resolve(); agents=Path(sys.argv[2]); config=Path(sys.argv[3]); port=int(sys.argv[4])\n"
            "sys.path.insert(0,str(agents))\n"
            "from matuto import store,web\n"
            "opened=store.open_store(config); opened.close()\n"
            "web.build_server(port, routes_root=Path(sys.argv[5])).serve_forever()\n")

    def _stop_fake_manager(self):
        pid_file = self.home / "manager.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except (OSError, ValueError):
                pass

    def _initial_release(self):
        self.data.mkdir(mode=0o700)
        root = updater.releases_directory(self.data)
        root.mkdir(mode=0o700)
        old = root / "2026.10.0"
        old.mkdir(mode=0o700)
        (old / "version").write_text("2026.10.0\n")
        shutil.copytree(Path(__file__).resolve().parents[2], old / "agents",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
        os.symlink(old.name, root / "current")

    def _fake_systemctl(self):
        bindir = self.home / "bin"
        bindir.mkdir(mode=0o700)
        self.system_log = self.home / "systemctl.log"
        script = bindir / "systemctl"
        script.write_text("""#!/usr/bin/env python3
import os, socket, subprocess, sys, time
from pathlib import Path
with open(os.environ['SYSTEMCTL_LOG'], 'a') as log:
    log.write(' '.join(sys.argv[1:]) + '\\n')
command = sys.argv[2] if len(sys.argv) > 2 else ''
pid_file = Path(os.environ['MANAGER_PID_FILE'])
def read_pid():
    try: return int(pid_file.read_text())
    except (OSError, ValueError): return 0
def alive(pid):
    if pid <= 0: return False
    try: os.kill(pid, 0); return True
    except OSError: return False
if command == 'is-enabled':
    state = Path(os.environ['MANAGER_ENABLED_FILE'])
    print(state.read_text() if state.exists() else 'enabled')
    sys.exit(0)
if command == 'disable':
    Path(os.environ['MANAGER_ENABLED_FILE']).write_text('disabled')
    sys.exit(0)
if command == 'is-active':
    unit = sys.argv[3] if len(sys.argv) > 3 else ''
    running = unit == 'matuto-manager.service' and alive(read_pid())
    print('active' if running else 'inactive'); sys.exit(0 if running else 3)
if command == 'show':
    pid = read_pid(); print(pid if alive(pid) else 0); sys.exit(0)
if command == 'stop':
    pid = read_pid()
    if alive(pid):
        os.kill(pid, 15)
        for _ in range(100):
            if not alive(pid): break
            time.sleep(.02)
    pid_file.unlink(missing_ok=True)
    sys.exit(0)
if command == 'start':
    data = Path(os.environ['XDG_DATA_HOME']) / 'matuto'
    manager = (data / 'releases/current/agents/matuto/cli.py').resolve()
    error = open(os.environ['MANAGER_ERROR_LOG'], 'a')
    process = subprocess.Popen([sys.executable, '-B', os.environ['MANAGER_STANDIN'], str(manager),
                                str(manager.parent.parent), os.environ['XDG_CONFIG_HOME'],
                                os.environ['MANAGER_PORT'], os.environ['MANAGER_ROUTES_ROOT'],
                                'serve'], stdout=subprocess.DEVNULL, stderr=error)
    pid_file.write_text(str(process.pid))
    for _ in range(200):
        if process.poll() is not None: sys.exit(1)
        try:
            with socket.create_connection(('127.0.0.1', int(os.environ['MANAGER_PORT'])), .05): sys.exit(0)
        except OSError: time.sleep(.025)
    os.kill(process.pid, 15); sys.exit(1)
sys.exit(0)
""")
        os.chmod(script, 0o700)
        self.pid_file = self.home / "manager.pid"
        self.manager_error_log = self.home / "manager-error.log"
        self.manager_enabled_file = self.home / "manager-enabled"
        self.manager_enabled_file.write_text("disabled")
        self.path_environment = mock.patch.dict(os.environ, {
            "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
            "SYSTEMCTL_LOG": str(self.system_log),
            "MANAGER_PID_FILE": str(self.pid_file),
            "MANAGER_ENABLED_FILE": str(self.manager_enabled_file),
            "MANAGER_STANDIN": str(self.manager_script),
            "MANAGER_ERROR_LOG": str(self.manager_error_log),
            "MANAGER_PORT": str(self.manager_port),
            "MANAGER_ROUTES_ROOT": str(self.manager_routes),
        })
        self.path_environment.start()
        self.addCleanup(self.path_environment.stop)

    def _fixture_archive(self, version, *, migration_fails=False, missing_updater=False,
                         same_version=False, extra_files=None):
        extra_files = dict(extra_files or {})
        fixture = (version, migration_fails, missing_updater, same_version,
                   tuple(sorted(extra_files.items())))
        if fixture in self.archives:
            return self.archives[fixture]
        suffix = "-extra" if extra_files else ""
        repo = self.fixture_root / f"fixture-{version}-{migration_fails}-{missing_updater}-{same_version}{suffix}"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
        source_agents = Path(__file__).resolve().parents[2]
        # The installed fixture needs runtime files, not another copy of the test suite.
        shutil.copytree(source_agents, repo / "agents",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
        (repo / "agents" / "matuto" / "updater-release-marker.txt").write_text(version)
        if missing_updater:
            (repo / "agents" / "matuto" / "updater.py").unlink()
        target_store = repo / "agents" / "matuto" / "store.py"
        source = target_store.read_text()
        if not same_version:
            source = source.replace(f"SCHEMA_VERSION = {self.initial_schema}",
                                    f"SCHEMA_VERSION = {self.target_schema}", 1)
        probe = ("    import os\n"
                 "    from matuto import store as target_store\n"
                 "    try:\n"
                 "        candidate = target_store.open_store(os.environ['XDG_CONFIG_HOME'])\n"
                 "        candidate.close()\n"
                 "    except target_store.StoreError as error:\n"
                 "        if 'update is in progress' not in str(error): raise\n"
                 "    else:\n"
                 "        raise RuntimeError('store writer was admitted during update')\n")
        if migration_fails:
            migration = ("def _fixture_migration(connection):\n" + probe +
                         f"    raise RuntimeError('fixture migration failed')\n"
                         f"MIGRATIONS = {{{self.initial_schema}: _fixture_migration}}")
        else:
            migration = ("def _fixture_migration(connection):\n" + probe +
                         "    connection.execute(\"CREATE TABLE updater_migration_probe (value TEXT NOT NULL)\")\n"
                         "    connection.execute(\"INSERT INTO updater_migration_probe VALUES ('migrated')\")\n"
                         f"MIGRATIONS = {{{self.initial_schema}: _fixture_migration}}")
        source = source.replace("MIGRATIONS = {}", "MIGRATIONS = {}" if same_version else migration, 1)
        target_store.write_text(source)
        files = {
            "wq.py": "print('fixture wq')\n",
            "wq.sh": "#!/bin/sh\nexit 0\n",
            "LICENSE": "fixture license\n",
            "docs/MATUTO_LAB.md": "fixture lab guide\n",
            "docs/MATUTO_MANAGER.md": "fixture manager guide\n",
            "docs/RELEASING.md": "fixture release guide\n",
        }
        for name, content in {**files, **extra_files}.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture release"], check=True)
        commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        output = repo / "matuto.release"
        entry = release_build.build_release(commit, version, output, repository=repo)
        self.archives[fixture] = output.read_bytes(), entry
        return self.archives[fixture]

    def _signed_feed(self, *, version="2026.10.1", number=7, migration_fails=False,
                     missing_updater=False, same_version=False, entry_overrides=None,
                     extra_files=None):
        private = self.home / f"release-key-{version}"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "fixture", "-f", str(private)],
                       check=True, capture_output=True)
        archive, entry = self._fixture_archive(
            version, migration_fails=migration_fails, missing_updater=missing_updater,
            same_version=same_version, extra_files=extra_files)
        entry = {**entry, "size": len(archive), "sha256": hashlib.sha256(archive).hexdigest(),
                 **(entry_overrides or {})}
        document = json.dumps({"number": number, "releases": [entry]}).encode()
        list_path = self.home / f"release-list-{version}.json"
        list_path.write_bytes(document)
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(private), "-n", "matuto-release",
                        str(list_path)], check=True, capture_output=True)
        signature = Path(f"{list_path}.sig").read_bytes()
        FeedHandler.assets = {
            "/release-list.json": document,
            "/release-list.json.sig": signature,
            f"/matuto-{version}.release": archive,
        }
        feed = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FeedHandler)
        thread = threading.Thread(target=feed.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(feed.server_close)
        self.addCleanup(feed.shutdown)
        public_key = Path(f"{private}.pub").read_bytes()
        return f"http://127.0.0.1:{feed.server_port}/", public_key, archive

    def _publish_on_feed(self, version, signer, number, entry_overrides=None):
        archive, entry = self._fixture_archive(version)
        entry = {**entry, "size": len(archive), "sha256": hashlib.sha256(archive).hexdigest(),
                 **(entry_overrides or {})}
        document = json.dumps({"number": number, "releases": [entry]}).encode()
        list_path = self.home / f"release-list-{version}-{number}.json"
        list_path.write_bytes(document)
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(signer), "-n",
                        "matuto-release", str(list_path)], check=True, capture_output=True)
        FeedHandler.assets = {
            "/release-list.json": document,
            "/release-list.json.sig": Path(f"{list_path}.sig").read_bytes(),
            f"/matuto-{version}.release": archive,
        }
        return archive

    def apply(self, feed, key, *, serving_reader=None, free_bytes=1 << 30, health_check=None,
              version=None, list_number=None):
        return updater.apply(feed, key, config_root=self.config, data_root=self.data,
                             free_bytes=free_bytes,
                             health_check=health_check or (lambda: updater._health(
                                 2, self.manager_url, self.manager_host, "2026.10.1")),
                             serving_reader=serving_reader or self.serving_reader,
                             version=version, list_number=list_number)

    def apply_local(self, key, archive, *, release_name="matuto-2026.10.1.release"):
        list_path = self.home / "local-release-list.json"
        list_path.write_bytes(FeedHandler.assets["/release-list.json"])
        Path(f"{list_path}.sig").write_bytes(FeedHandler.assets["/release-list.json.sig"])
        release_path = self.home / release_name
        release_path.write_bytes(archive)
        return updater.apply_file(
            release_path, list_path, key, config_root=self.config, data_root=self.data,
            free_bytes=1 << 30, serving_reader=self.serving_reader,
            health_check=lambda: updater._health(2, self.manager_url, self.manager_host, "2026.10.1"))

    def test_local_install_refuses_bad_signature_wrong_digest_and_unlisted_file(self):
        feed, key, archive = self._signed_feed(number=7)
        before = self.store_path.read_bytes()

        FeedHandler.assets["/release-list.json.sig"] = b"invalid signature"
        with self.assertRaises(releases.ReleaseError):
            self.apply_local(key, archive)
        self.assertEqual(self.store_path.read_bytes(), before)

        feed, key, archive = self._signed_feed(version="2026.10.2", number=7)
        with self.assertRaisesRegex(releases.ReleaseError, "SHA-256"):
            self.apply_local(key, archive[:-1] + bytes([archive[-1] ^ 1]),
                             release_name="matuto-2026.10.2.release")
        self.assertEqual(self.store_path.read_bytes(), before)

        feed, key, archive = self._signed_feed(version="2026.10.3", number=7)
        with self.assertRaisesRegex(updater.UpdateError, "not named"):
            self.apply_local(key, archive, release_name="unlisted.release")
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertFalse(self.system_log.exists())
        self.assertEqual(FeedHandler.requests, [])

    def test_withdrawn_local_list_is_remembered_before_refusal(self):
        feed, key, archive = self._signed_feed(number=8, entry_overrides={"withdrawn": True})
        before = self.store_path.read_bytes()
        with self.assertRaisesRegex(updater.UpdateError, "withdrawn"):
            self.apply_local(key, archive)
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertEqual(updater._read_list_number(self.data), 8)

        older = json.loads(FeedHandler.assets["/release-list.json"])
        older["number"] = 7
        older["releases"][0]["withdrawn"] = False
        list_path = self.home / "local-release-list.json"
        document = json.dumps(older).encode()
        list_path.write_bytes(document)
        signature_path = Path(f"{list_path}.sig")
        signature_path.unlink()
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f",
                        str(self.home / "release-key-2026.10.1"),
                        "-n", "matuto-release", str(list_path)],
                       check=True, capture_output=True)
        FeedHandler.assets["/release-list.json"] = document
        FeedHandler.assets["/release-list.json.sig"] = signature_path.read_bytes()
        with self.assertRaisesRegex(updater.UpdateError, "stale"):
            self.apply_local(key, archive)
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertFalse(self.system_log.exists())
        self.assertEqual(FeedHandler.requests, [])

    def test_recovered_app_install_success_requires_the_pinned_release_and_list(self):
        pending = {"version": "2026.10.1", "list_number": 7}
        state = {"version": "2026.10.1", "list_number": 7,
                 "app_install_pending": pending}
        self.assertEqual(updater._recovered_install_outcome(
            state, "complete", installed="2026.10.1")["install_result"]["stage"], "complete")
        changed = {**state, "list_number": 8}
        outcome = updater._recovered_install_outcome(
            changed, "complete", installed="2026.10.1")
        self.assertEqual(outcome["install_result"]["stage"], "failed")
        self.assertIsNone(outcome["app_install_pending"])
        self.assertIn("installed release did not match", outcome["install_result"]["reason"])

    def test_repair_retries_the_pinned_offer_after_an_unconfirmed_app_handoff(self):
        feed, key, _archive = self._signed_feed(number=7)
        unit = "matuto-update-" + "a" * 32 + ".service"
        store.write_update_state(
            self.store_path, "2026.10.0", "complete", list_number=6,
            details={"version": "2026.10.0", "trusted_key": key.decode("ascii"),
                     "release_address": feed,
                     "app_install_pending": {"version": "2026.10.1", "list_number": 7,
                                                "unit_name": unit}})
        with mock.patch.object(updater, "app_install_unit_status", return_value="inactive"):
            result = updater.repair(config_root=self.config, data_root=self.data,
                                    health_check=lambda: None)
        self.assertTrue(result.recovered_update)
        self.assertEqual(result.version, "2026.10.1")
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")
        phase = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertIsNone(phase.get("app_install_pending"))
        self.assertEqual(phase["install_result"]["stage"], "complete")

    def test_app_update_lock_refusal_records_failure_and_clears_terminal_reservation(self):
        feed, key, _archive = self._signed_feed(number=7)
        unit = "matuto-update-" + "a" * 32 + ".service"
        store.write_update_state(
            self.store_path, "2026.10.0", "complete", list_number=7,
            details={"version": "2026.10.0", "trusted_key": key.decode("ascii"),
                     "release_address": feed,
                     "app_install_pending": {"version": "2026.10.1", "list_number": 7,
                                                "unit_name": unit}})
        real_lock = updater._updater_lock
        attempts = 0

        def busy_then_available(data_root, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise updater.UpdateError("another Matuto update is already running")
            return real_lock(data_root, **kwargs)

        with mock.patch.object(updater, "_updater_lock", side_effect=busy_then_available):
            status = updater.main(["update", "--version", "2026.10.1", "--list-number", "7",
                                   "--install-unit", unit])
        phase = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(status, 1)
        self.assertEqual(phase["stage"], "complete")
        self.assertIsNone(phase.get("app_install_pending"))
        self.assertEqual(phase["app_install_result_unit"], unit)
        self.assertEqual(phase["install_result"]["stage"], "failed")
        self.assertEqual(phase["install_result"]["reason"], "another Matuto update is already running")

    def test_old_app_updater_finalizer_preserves_a_newer_reservation(self):
        feed, key, _archive = self._signed_feed(number=7)
        old_unit = "matuto-update-" + "a" * 32 + ".service"
        new_unit = "matuto-update-" + "b" * 32 + ".service"
        store.write_update_state(
            self.store_path, "2026.10.0", "complete", list_number=7,
            details={"version": "2026.10.0", "trusted_key": key.decode("ascii"),
                     "release_address": feed,
                     "app_install_pending": {"version": "2026.10.1", "list_number": 7,
                                                "unit_name": old_unit}})
        real_apply = updater.apply

        def reserve_next_install(address, trusted_key, **kwargs):
            result = real_apply(
                address, trusted_key, config_root=self.config, data_root=self.data,
                free_bytes=1 << 30,
                health_check=lambda: updater._health(
                    2, self.manager_url, self.manager_host, "2026.10.1"), **kwargs)
            phase = json.loads(store.read_update_state(self.store_path)["phase"])
            self.assertEqual(phase["stage"], "complete")
            self.assertIsNone(phase.get("app_install_pending"))
            store.record_update_check(
                self.store_path, {"update_offer": {"version": "2026.10.2"}}, list_number=8)
            self.assertTrue(store.begin_app_install(
                self.store_path, "2026.10.2", 8, new_unit))
            return result

        with mock.patch.object(updater, "apply", side_effect=reserve_next_install):
            status = updater.main(["update", "--version", "2026.10.1", "--list-number", "7",
                                   "--install-unit", old_unit])
        phase = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(status, 0)
        self.assertEqual(phase["app_install_pending"], {
            "version": "2026.10.2", "list_number": 8, "unit_name": new_unit})
        self.assertEqual(phase["app_install_result_unit"], old_unit)

        status = updater.main(["update", "--version", "2026.10.2", "--list-number", "8",
                               "--install-unit", new_unit])
        phase = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(status, 1)
        self.assertIsNone(phase.get("app_install_pending"))
        self.assertEqual(phase["app_install_result_unit"], new_unit)
        self.assertEqual(phase["install_result"]["version"], "2026.10.2")
        self.assertEqual(phase["install_result"]["stage"], "failed")
        self.assertIn("stale", phase["install_result"]["reason"].lower())

    def test_repair_clears_a_pre_activation_failure_that_lost_its_final_result(self):
        unit = "matuto-update-" + "a" * 32 + ".service"
        store.write_update_state(
            self.store_path, "2026.10.1", "failed", list_number=7,
            details={"version": "2026.10.1", "reason": "disk full",
                     "app_install_pending": {"version": "2026.10.1", "list_number": 7,
                                                "unit_name": unit}})
        with mock.patch.object(updater, "app_install_unit_status", return_value="inactive"):
            result = updater._repair_pending_app_install(
                self.config, self.data, "https://feed.invalid/", b"unused", None, None)
        self.assertIsNone(result)
        phase = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(phase["stage"], "complete")
        self.assertIsNone(phase.get("app_install_pending"))
        self.assertEqual(phase["install_result"]["stage"], "failed")
        self.assertEqual(phase["install_result"]["reason"], "disk full")

    def test_app_install_refuses_a_changed_signed_list_or_release(self):
        feed, key, _archive = self._signed_feed(number=7)
        with self.assertRaisesRegex(updater.UpdateError, "signed release list changed"):
            self.apply(feed, key, version="2026.10.1", list_number=6)
        with self.assertRaisesRegex(updater.UpdateError, "reviewed release 2026.10.2 is absent"):
            self.apply(feed, key, version="2026.10.2", list_number=7)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertEqual(updater._read_list_number(self.data), 7)
        self.assertFalse(self.system_log.exists())

    def test_pinned_update_can_read_the_store_before_it_takes_the_exclusive_install_lock(self):
        feed, key, _archive = self._signed_feed(number=7)
        unit = "matuto-update-" + "a" * 32 + ".service"
        store.write_update_state(
            self.store_path, "2026.10.0", "complete", list_number=7,
            details={"app_install_pending": {"version": "2026.10.1", "list_number": 7,
                                               "unit_name": unit}})
        result = self.apply(feed, key, version="2026.10.1", list_number=7)
        self.assertEqual(result.version, "2026.10.1")
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")

    def test_manager_store_probe_can_read_verifying_state_with_install_reservation(self):
        unit = "matuto-update-" + "a" * 32 + ".service"
        store.write_update_state(
            self.store_path, "2026.10.1", "verifying", list_number=7,
            details={"app_install_pending": {"version": "2026.10.1", "list_number": 7,
                                               "unit_name": unit}})
        updater._manager_store_ready(updater.active_release(self.data), self.config)

    def test_local_install_refuses_nonnewer_release(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=9)
        before = self.store_path.read_bytes()
        with self.assertRaisesRegex(updater.UpdateError, "not newer than installed"):
            self.apply_local(key, archive, release_name="matuto-2026.10.0.release")
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertFalse(self.system_log.exists())

    def test_cli_installs_local_release_without_contacting_the_feed(self):
        feed, key, archive = self._signed_feed(number=7)
        list_path = self.home / "release-list.json"
        list_path.write_bytes(FeedHandler.assets["/release-list.json"])
        Path(f"{list_path}.sig").write_bytes(FeedHandler.assets["/release-list.json.sig"])
        release_path = self.home / "matuto-2026.10.1.release"
        release_path.write_bytes(archive)
        root, _stable_agents = self._install_stable_updater(manager_url=self.manager_url)
        store.write_update_state(
            self.store_path, "2026.10.1", "preparing", list_number=6,
            details={"trusted_key": key.decode("ascii"), "release_address": "https://feed.invalid/"})
        script = ("import sys; from matuto.cli import main; "
                  "sys.exit(main(['matuto', 'install-from-file', sys.argv[1], sys.argv[2]]))")
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(release_path), str(list_path)],
            env=env, cwd=self.home, capture_output=True, text=True, timeout=120)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installed Matuto 2026.10.1", result.stdout)
        self.assertEqual(FeedHandler.requests, [])
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")
        self.assertEqual(store.read_schema_version(self.store_path), self.target_schema)
        self.assertEqual(updater.trusted_key(self.config), key)
        self.assertEqual(os.readlink(root / "current"), "versions/2026.10.1")
        self.assertEqual((root / "current/agents/matuto/updater-release-marker.txt").read_text(),
                         "2026.10.1")
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["release_address"], "https://feed.invalid/")
        self.assertFalse(state["updater_swap_pending"])

    def test_cli_install_from_file_fifo_refuses_without_hanging(self):
        _feed, key, archive = self._signed_feed()
        self._install_stable_updater()
        list_path = self.home / "release-list.fifo"
        os.mkfifo(list_path)
        release_path = self.home / "matuto-2026.10.1.release"
        release_path.write_bytes(archive)
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode("ascii"), "release_address": "https://feed.invalid/"})
        before = self.store_path.read_bytes()
        script = ("import sys; from matuto.cli import main; "
                  "sys.exit(main(['matuto', 'install-from-file', sys.argv[1], sys.argv[2]]))")
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(release_path), str(list_path)],
            env=env, cwd=self.home, capture_output=True, text=True, timeout=3)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ordinary file", result.stderr)
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertFalse(self.system_log.exists())

    def test_cli_install_from_file_without_bootstrap_key_refuses(self):
        _feed, key, archive = self._signed_feed()
        list_path = self.home / "release-list.json"
        list_path.write_bytes(FeedHandler.assets["/release-list.json"])
        Path(f"{list_path}.sig").write_bytes(FeedHandler.assets["/release-list.json.sig"])
        release_path = self.home / "matuto-2026.10.1.release"
        release_path.write_bytes(archive)
        script = ("import sys; from matuto.cli import main; "
                  "sys.exit(main(['matuto', 'install-from-file', sys.argv[1], sys.argv[2]]))")
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(release_path), str(list_path)],
            env=env, cwd=self.home, capture_output=True, text=True, timeout=10)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bootstrap", result.stderr)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertFalse(self.system_log.exists())
        self.assertEqual(FeedHandler.requests, [])

    def test_verified_update_migrates_forward_and_preserves_sessions_and_credentials(self):
        feed, key, _archive = self._signed_feed()
        store.write_update_state(self.store_path, "2026.10.0", "complete",
                                 details={"trusted_key": key.decode().strip(),
                                          "release_address": feed})
        before_login = hashlib.sha256(self.login.read_bytes()).digest()
        before_key = hashlib.sha256(self.api_key.read_bytes()).digest()

        script = """
import base64, sys
from types import SimpleNamespace
from unittest import mock
from matuto import updater
updater.MANAGER_URL = sys.argv[5]
updater.MANAGER_HOST = sys.argv[6]
check = updater._check_manager
def check_with_other_ambient(*args, **kwargs):
    with mock.patch.dict(updater.os.environ, {"XDG_CONFIG_HOME": sys.argv[7]}):
        return check(*args, **kwargs)
updater._check_manager = check_with_other_ambient
updater.apply(sys.argv[1], base64.b64decode(sys.argv[2]),
              config_root=sys.argv[3], data_root=sys.argv[4],
              serving_reader=lambda: (SimpleNamespace(state="stopped"), []))
"""
        env = os.environ.copy()
        agents_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = agents_root + os.pathsep + env.get("PYTHONPATH", "")
        process = subprocess.run(
            [sys.executable, "-B", "-c", script, feed,
             base64.b64encode(key).decode(), str(self.config), str(self.data),
             self.manager_url, self.manager_host, str(self.home / "ambient-config")], env=env, cwd=self.home,
            capture_output=True, text=True, timeout=120)
        self.assertEqual(process.returncode, 0, process.stderr + "\\n" +
                         self.system_log.read_text() + "\\n" +
                         (self.manager_error_log.read_text() if self.manager_error_log.exists() else ""))
        self.assertEqual(store.read_schema_version(self.store_path), self.target_schema)
        self.assertEqual(store.read_sessions_at(self.store_path), self.expected_sessions)
        self.assertTrue(self._active_session_valid())
        self._existing_session_reaches_manager()
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")
        installed_manifest = json.loads((updater.active_release(self.data) / "RELEASE.json").read_text())
        self.assertIn("agents/matuto/store.py", installed_manifest["files"])
        snapshot = updater.snapshot.snapshot_path(self.store_path)
        self.assertEqual(updater.snapshot._inspect(snapshot)[1], "2026.10.1")
        migrated = store._readonly_connect(self.store_path)
        try:
            self.assertEqual(migrated.execute(
                "SELECT value FROM updater_migration_probe").fetchone()[0], "migrated")
            self.assertEqual(migrated.execute(
                "SELECT value FROM store_meta WHERE key = 'updater_preservation_probe'").fetchone()[0],
                "keep me")
        finally:
            migrated.close()
        self.assertEqual(hashlib.sha256(self.login.read_bytes()).digest(), before_login)
        self.assertEqual(hashlib.sha256(self.api_key.read_bytes()).digest(), before_key)
        calls = self.system_log.read_text().splitlines()
        self.assertEqual(calls, ["--user stop matuto-manager.service", "--user is-active matuto-manager.service",
                                 "--user start matuto-manager.service",
                                 "--user show --property=MainPID --value matuto-manager.service"])
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["stage"], "complete")
        self.assertEqual(state["version"], "2026.10.1")
        self.assertEqual(state["list_number"], 7)
        self.assertEqual(state["schema_version"], self.target_schema)
        self.assertTrue(state["expected_sessions"])
        self.assertEqual(len(state["expected_credentials"]), 2)
        self.assertEqual(bootstrap.trusted_inputs(self.config), (feed, key.decode().strip().encode()))

    def assert_refused_unchanged(self, feed, key, error, message=None, *,
                                 list_number=None, free_bytes=1 << 30, serving_reader=None):
        before = self.store_path.read_bytes()
        calls = self.system_log.read_bytes() if self.system_log.exists() else None
        login_before = self.login.read_bytes()
        key_before = self.api_key.read_bytes()
        sessions = store.read_sessions_at(self.store_path)
        schema_version = store.read_schema_version(self.store_path)
        previous_snapshot = updater.snapshot.snapshot_path(self.store_path)
        if previous_snapshot.exists():
            snapshot_digest = hashlib.sha256(previous_snapshot.read_bytes()).digest()
        else:
            snapshot_digest = None
        with self.assertRaisesRegex(error, message) if message else self.assertRaises(error):
            self.apply(feed, key, free_bytes=free_bytes, serving_reader=serving_reader)
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertEqual(store.read_sessions_at(self.store_path), sessions)
        self.assertEqual(store.read_schema_version(self.store_path), schema_version)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        opened = store.open_store(self.config)
        try:
            self.assertEqual(updater._highest_list_number(opened.connection(), self.data),
                             list_number)
        finally:
            opened.close()
        if snapshot_digest is not None:
            self.assertEqual(hashlib.sha256(previous_snapshot.read_bytes()).digest(), snapshot_digest)
        else:
            self.assertFalse(previous_snapshot.exists())
        self.assertEqual(self.login.read_bytes(), login_before)
        self.assertEqual(self.api_key.read_bytes(), key_before)
        self.assertEqual(self.system_log.read_bytes() if self.system_log.exists() else None, calls)

    def test_repair_restores_same_release_files_and_preserves_disabled_login_start(self):
        feed, key, archive = self._signed_feed(
            version="2026.10.0", number=7, same_version=True,
            extra_files={"agents/matuto/repair-fixture/nested/probe.txt": "nested release file"})
        release_dir = updater.active_release(self.data).resolve()
        (release_dir / "version").unlink()
        nested_directory = release_dir / "agents/matuto/repair-fixture"
        nested_file = nested_directory / "nested/probe.txt"
        nested_file.parent.mkdir(parents=True)
        nested_file.write_text("outdated nested release file")
        shutil.rmtree(nested_directory)
        damaged = release_dir / "agents/matuto/updater.py"
        damaged.write_text("corrupted updater\\n")
        (release_dir / "agents/matuto/cli.py").unlink()
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        def non_update_store_tables():
            connection = sqlite3.connect(self.store_path.as_uri() + "?mode=ro", uri=True)
            try:
                names = [row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name != 'update_state'")]
                return {name: connection.execute(f'SELECT * FROM "{name}"').fetchall()
                        for name in names}
            finally:
                connection.close()

        before_store_tables = non_update_store_tables()
        before_sessions = store.read_sessions_at(self.store_path)
        before_login = self.login.read_bytes()
        before_key = self.api_key.read_bytes()
        from matuto.bootstrap import _manager_unit
        manager_unit = self.config / "systemd/user/matuto-manager.service"
        manager_unit.parent.mkdir(parents=True)
        expected_unit = _manager_unit(self.data)
        manager_unit.write_text(expected_unit.replace("Restart=on-failure", "Restart=no"))
        key_directory = self.api_key.parent
        key_directory.chmod(0o755)
        store.store_directory(self.config).chmod(0o755)
        self.data.chmod(0o755)
        (release_dir / "agents/matuto").chmod(0o755)
        untouched = {}
        for name in ("weights/model.gguf", "builds/tree/bin/llama-server",
                     "runs/run.json", "consumer/models.json"):
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"keep {name}\\n".encode())
            untouched[path] = path.read_bytes()
        self._install_stable_updater(manager_url=self.manager_url)

        self.assertEqual(cli.main(["matuto", "repair"]), 0)

        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertIn("agents/matuto/updater.py", state["repair_files"])
        self.assertEqual(damaged.read_bytes(), (release_dir / "agents/matuto/updater.py").read_bytes())
        self.assertTrue((release_dir / "agents/matuto/cli.py").is_file())
        nested_file = nested_directory / "nested/probe.txt"
        self.assertEqual(nested_file.read_text(), "nested release file")
        self.assertEqual(nested_directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((nested_directory / "nested").stat().st_mode & 0o777, 0o700)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertFalse(updater.snapshot.snapshot_path(self.store_path).exists())
        self.assertEqual(non_update_store_tables(), before_store_tables)
        self.assertEqual(store.read_sessions_at(self.store_path), before_sessions)
        self.assertEqual(self.login.read_bytes(), before_login)
        self.assertEqual(self.api_key.read_bytes(), before_key)
        self.assertEqual(self.manager_enabled_file.read_text(), "disabled")
        self.assertEqual(manager_unit.read_text(), expected_unit)
        self.assertEqual(key_directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(store.store_directory(self.config).stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.data.stat().st_mode & 0o777, 0o700)
        self.assertEqual((release_dir / "agents/matuto").stat().st_mode & 0o777, 0o700)
        self.assertEqual({path: path.read_bytes() for path in untouched}, untouched)
        preset = self.config / "ai-agents-sandbox/matuto-manager/router-presets.ini"
        self.assertEqual(preset.read_text(), "version = 1\n")
        self.assertEqual(preset.stat().st_mode & 0o777, 0o600)
        from matuto.bootstrap import _desktop_entry
        updater_script = self.data / "updater/current/agents/matuto/updater.py"
        for filename, name, action in (("matuto.desktop", "Matuto", "open"),
                                       ("repair-matuto.desktop", "Repair Matuto", "repair"),
                                       ("update-matuto.desktop", "Update Matuto", "update")):
            entry = self.home / "data/applications" / filename
            self.assertEqual(entry.read_text(), _desktop_entry(name, updater_script, action))
        expected_calls = [
            "--user is-enabled matuto-manager.service",
            "--user stop matuto-manager.service",
            "--user daemon-reload",
            "--user start matuto-manager.service",
            "--user disable matuto-manager.service",
            "--user show --property=MainPID --value matuto-manager.service"]
        self.assertEqual(self.system_log.read_text().splitlines(), expected_calls)
        self._existing_session_reaches_manager()

        generated = [manager_unit, preset] + [
            self.home / "data/applications" / filename for filename in (
                "matuto.desktop", "repair-matuto.desktop", "update-matuto.desktop")]
        verified, _expanded_size = updater._archive_members(archive)
        all_repaired = generated + [release_dir / name for name, _size, _mode in verified]
        files_before_rerun = {path: (path.read_bytes(), path.stat().st_mode & 0o777,
                                     path.stat().st_mtime_ns) for path in all_repaired}
        self.assertEqual(cli.main(["matuto", "repair"]), 0)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mode & 0o777,
                                 path.stat().st_mtime_ns) for path in all_repaired},
                         files_before_rerun)
        self.assertEqual(non_update_store_tables(), before_store_tables)
        self.assertEqual(store.read_sessions_at(self.store_path), before_sessions)
        self.assertEqual(self.login.read_bytes(), before_login)
        self.assertEqual(self.api_key.read_bytes(), before_key)
        self._existing_session_reaches_manager()

    def test_repair_refuses_an_invalid_release_signature_without_stopping_or_mutating_files(self):
        feed, key, _archive = self._signed_feed(version="2026.10.0", number=7,
                                                same_version=True)
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        damaged = updater.active_release(self.data) / "agents/matuto/updater.py"
        damaged.write_text("damaged but untouched\\n")
        FeedHandler.assets["/release-list.json.sig"] = b"invalid signature"

        self.assertEqual(updater.main(["repair"]), 1)

        self.assertEqual(damaged.read_text(), "damaged but untouched\\n")
        self.assertFalse(self.system_log.exists())
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["repair_result"], "failed")
        self.assertIn("signature does not verify", state["repair_error"])
        from matuto import error_page
        self.assertIn("signature does not verify", error_page.page_file().read_text())

    def test_repair_offline_refuses_without_changing_installed_files(self):
        _feed, key, _archive = self._signed_feed(version="2026.10.0", number=7,
                                                 same_version=True)
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": "http://127.0.0.1:1/"})
        damaged = updater.active_release(self.data) / "agents/matuto/updater.py"
        damaged.write_text("damaged but untouched\\n")
        self.data.chmod(0o755)
        from matuto.discovery import load_item
        route = load_item("routes", "maintenance")
        route._record_repair("in_progress")

        self.assertEqual(updater.main(["repair"]), 1)

        self.assertEqual(damaged.read_text(), "damaged but untouched\\n")
        self.assertEqual(self.data.stat().st_mode & 0o777, 0o755)
        self.assertFalse(self.system_log.exists())
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["stage"], "maintenance")
        self.assertEqual(state["repair_result"], "failed")
        self.assertFalse(state["repair_recovered_update"])
        self.assertIn("needs the network", state["repair_error"])
        self.assertEqual(route._repair_result(), {
            "stage": "failed", "error": state["repair_error"]})
        from matuto import error_page
        self.assertIn("needs the network", error_page.page_file().read_text())

    def test_repair_refuses_checkout_manager_unit_without_writing(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        damaged = updater.active_release(self.data) / "agents/matuto/updater.py"
        damaged.write_text("damaged but untouched\\n")
        (updater.active_release(self.data) / "version").unlink()
        unit = self.config / "systemd/user/matuto-manager.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("\n".join((
            "[Service]",
            f"# {self.data / 'releases/current/agents/matuto/cli.py'}",
            "ExecStart=/home/operator/.local/bin/matuto-manager serve", "")))
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)
        before = damaged.read_bytes()

        with self.assertRaisesRegex(updater.UpdateError, "manager unit Repair did not write"):
            updater.repair(config_root=self.config, data_root=self.data,
                           fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive))

        self.assertEqual(damaged.read_bytes(), before)
        self.assertFalse(self.system_log.exists())

    def test_repair_reconciles_interrupted_preparation_then_repairs_installed_release(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        (updater.active_release(self.data) / "version").unlink()
        store.write_update_state(
            self.store_path, "2026.10.1", "preparing",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)

        result = updater.repair(
            config_root=self.config, data_root=self.data,
            fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive),
            health_check=lambda: updater._health(2, self.manager_url, self.manager_host,
                                                  "2026.10.0"))

        self.assertFalse(result.recovered_update)
        self.assertEqual(result.version, "2026.10.0")
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["repair_result"], "complete")
        self.assertFalse(state["repair_recovered_update"])
        self.assertFalse((updater.active_release(self.data) / "version").exists())
        self.assertEqual(FeedHandler.requests, [])

    def test_retry_of_failed_forward_repair_keeps_forward_recovery(self):
        _feed, _key, archive = self._signed_feed()
        verified, _size = updater._archive_members(archive)
        updater._extract(self.data, "2026.10.1", archive, verified)
        updater._atomic_current(self.data, "2026.10.1")
        store.write_update_state(
            self.store_path, "2026.10.1", "maintenance",
            details={"repair_result": "failed", "repair_recovered_update": True})

        updater._record_repair_started(self.config)
        started = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(started["repair_result"], "in_progress")
        self.assertEqual(started["repair_prior_result"], "failed")
        self.assertTrue(started["repair_recovered_update"])

        recovered = updater._recover(self.config, self.data, retry_failed_repair=True)
        self.assertEqual(recovered["_recovery"], "forward")

    def test_repair_records_old_recovery_health_failure(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        (updater.active_release(self.data) / "version").unlink()
        store.write_update_state(
            self.store_path, "2026.10.1", "preparing",
            details={"trusted_key": key.decode().strip(), "release_address": feed})

        def fail_fetch(*_args):
            self.fail("same-version repair must not start after recovery health fails")

        def failed_health_check():
            raise updater.UpdateError("fixture old manager health failure")

        with self.assertRaisesRegex(updater.UpdateError, "forward recovery manager check failed"):
            updater.repair(config_root=self.config, data_root=self.data,
                           fetcher=fail_fetch, health_check=failed_health_check)

        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["stage"], "maintenance")
        self.assertEqual(state["repair_result"], "failed")
        self.assertIn("fixture old manager health failure", state["repair_error"])
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")

    def test_repair_finishes_interrupted_update_forward_and_only_once(self):
        _feed, _key, archive = self._signed_feed()
        verified, _size = updater._archive_members(archive)
        release_dir = updater._extract(self.data, "2026.10.1", archive, verified)
        store.write_update_state(self.store_path, "2026.10.1", "preparing")
        opened = store.open_store(self.config, exclusive=True)
        try:
            staged = updater._stage_store(release_dir, self.config)
        finally:
            opened.close()
        sessions_before = store.read_sessions_at(self.store_path)
        credentials = (updater._copy_identity(self.login), updater._copy_identity(self.api_key))
        store.write_update_state(
            self.store_path, "2026.10.1", "activating", list_number=7,
            details={"staged_store": str(staged), "release_path": str(release_dir),
                     "schema_version": store.read_schema_version(staged),
                     "updater_manifest_sha256": updater._release_manifest_digest(archive),
                     **updater._preservation_state(sessions_before, credentials)})

        with mock.patch.object(updater, "MANAGER_URL", self.manager_url):
            self.assertEqual(updater.main(["repair"]), 0)

        self.assertEqual(updater.installed_version(self.data), "2026.10.1")
        self.assertEqual(store.read_schema_version(self.store_path), self.target_schema)
        self.assertEqual(store.read_sessions_at(self.store_path), sessions_before)
        self.assertEqual(FeedHandler.requests, [])
        connection = store._readonly_connect(self.store_path)
        try:
            self.assertEqual(connection.execute(
                "SELECT value FROM store_meta WHERE key='updater_preservation_probe'").fetchone()[0],
                "keep me")
        finally:
            connection.close()

    def test_repair_failure_cannot_overwrite_a_later_update_owner(self):
        original_repair = updater.repair
        original_record = updater._record_repair_failure
        record_count = 0

        def record_while_locked(*args, **kwargs):
            nonlocal record_count
            record_count += 1
            with self.assertRaises(updater.UpdaterBusyError):
                updater._updater_lock(self.data)
            return original_record(*args, **kwargs)

        def fail_then_start_next_owner(**_kwargs):
            try:
                return original_repair(config_root=self.config, data_root=self.data)
            except updater.UpdateError:
                failed = json.loads(store.read_update_state(self.store_path)["phase"])
                self.assertEqual(failed["repair_result"], "failed")
                lock_fd = updater._updater_lock(self.data)
                try:
                    store.write_update_state(self.store_path, "2026.11.0", "preparing")
                finally:
                    os.close(lock_fd)
                raise

        with (mock.patch.object(updater, "_repair_locked",
                                side_effect=updater.UpdateError("offline repair preflight")),
              mock.patch.object(updater, "_record_repair_failure", side_effect=record_while_locked),
              mock.patch.object(updater, "repair", side_effect=fail_then_start_next_owner)):
            self.assertEqual(updater.main(["repair"]), 1)

        phase = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(record_count, 1)
        self.assertEqual(phase["version"], "2026.11.0")
        self.assertEqual(phase["stage"], "preparing")

    def test_repair_refuses_while_another_updater_holds_the_lock(self):
        before = (updater.active_release(self.data) / "agents/matuto/updater.py").read_bytes()
        store.write_update_state(self.store_path, "2026.10.1", "preparing")
        phase_before = store.read_update_state(self.store_path)
        lock_fd = updater._updater_lock(self.data)
        try:
            with self.assertRaisesRegex(updater.UpdateError, "another Matuto update"):
                updater.repair(config_root=self.config, data_root=self.data)
            self.assertEqual(updater.main(["repair"]), 1)
            self.assertEqual(store.read_update_state(self.store_path), phase_before)
        finally:
            os.close(lock_fd)

        self.assertEqual(
            (updater.active_release(self.data) / "agents/matuto/updater.py").read_bytes(), before)
        self.assertFalse(self.system_log.exists())

    def test_repair_creates_only_a_missing_router_key(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        (updater.active_release(self.data) / "version").unlink()
        self.api_key.unlink()
        from matuto.bootstrap import _manager_unit
        manager_unit = self.config / "systemd/user/matuto-manager.service"
        manager_unit.parent.mkdir(parents=True)
        manager_unit.write_text(_manager_unit(self.data))
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)

        with mock.patch.object(updater, "MANAGER_URL", self.manager_url):
            result = updater.repair(
                config_root=self.config, data_root=self.data,
                fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive),
                health_check=lambda: updater._health(2, self.manager_url, self.manager_host,
                                                      "2026.10.0"))

        self.assertEqual(result.version, "2026.10.0")
        self.assertTrue(self.api_key.is_file())
        self.assertEqual(self.api_key.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.api_key.read_bytes(), b"router-api-key\\n")

    def test_repair_step_three_failure_marks_maintenance_if_manager_cannot_restart(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        (updater.active_release(self.data) / "version").unlink()
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        from matuto.bootstrap import _manager_unit
        manager_unit = self.config / "systemd/user/matuto-manager.service"
        manager_unit.parent.mkdir(parents=True)
        manager_unit.write_text(_manager_unit(self.data))
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)
        calls = []

        def systemctl(arguments):
            calls.append(arguments)
            if arguments[0] == "is-enabled":
                return subprocess.CompletedProcess(arguments, 0, "disabled")
            if arguments[0] in ("daemon-reload", "start"):
                return subprocess.CompletedProcess(arguments, 1, "fixture failure")
            return subprocess.CompletedProcess(arguments, 0, "")

        with mock.patch.object(updater, "_systemctl", side_effect=systemctl):
            with self.assertRaisesRegex(updater.UpdateError, "did not restart"):
                updater.repair(
                    config_root=self.config, data_root=self.data,
                    fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive))

        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["stage"], "maintenance")
        self.assertEqual(state["repair_result"], "failed")
        self.assertEqual([arguments[0] for arguments in calls],
                         ["is-enabled", "stop", "daemon-reload", "start"])

    def test_repair_does_not_enable_login_start_when_unit_and_link_are_missing(self):
        feed, key, _archive = self._signed_feed(version="2026.10.0", number=7,
                                                same_version=True)
        (updater.active_release(self.data) / "version").unlink()
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        self._install_stable_updater(manager_url=self.manager_url)

        with mock.patch.object(updater, "MANAGER_URL", self.manager_url):
            self.assertEqual(updater.main(["repair"]), 0)

        self.assertEqual(self.manager_enabled_file.read_text(), "disabled")
        calls = self.system_log.read_text().splitlines()
        self.assertNotIn("--user enable matuto-manager.service", calls)
        self.assertIn("--user disable matuto-manager.service", calls)

    def test_repair_preserves_enabled_login_start(self):
        feed, key, _archive = self._signed_feed(version="2026.10.0", number=7,
                                                same_version=True)
        (updater.active_release(self.data) / "version").unlink()
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        from matuto.bootstrap import _manager_unit
        manager_unit = self.config / "systemd/user/matuto-manager.service"
        manager_unit.parent.mkdir(parents=True)
        manager_unit.write_text(_manager_unit(self.data))
        self.manager_enabled_file.write_text("enabled")
        self._install_stable_updater(manager_url=self.manager_url)

        result = subprocess.run(
            [sys.executable, str(updater.active_release(self.data) / "agents/matuto/cli.py"),
             "repair"], cwd=self.home, env=os.environ, capture_output=True, text=True, timeout=120)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((updater.active_release(self.data) / "agents/matuto/__pycache__").exists())
        self.assertEqual(self.manager_enabled_file.read_text(), "enabled")
        self.assertNotIn("--user disable matuto-manager.service",
                         self.system_log.read_text().splitlines())

    def test_repair_manager_health_failure_is_recorded_without_reporting_success(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        (updater.active_release(self.data) / "version").unlink()
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        from matuto.bootstrap import _manager_unit
        manager_unit = self.config / "systemd/user/matuto-manager.service"
        manager_unit.parent.mkdir(parents=True)
        manager_unit.write_text(_manager_unit(self.data))
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)
        before_login = self.login.read_bytes()
        before_key = self.api_key.read_bytes()

        def failed_health_check():
            raise updater.UpdateError("fixture manager check failed")

        with mock.patch.object(updater, "MANAGER_URL", self.manager_url):
            with self.assertRaisesRegex(updater.UpdateError, "fixture manager check failed"):
                updater.repair(
                    config_root=self.config, data_root=self.data,
                    fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive),
                    health_check=failed_health_check)

        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertEqual(self.login.read_bytes(), before_login)
        self.assertEqual(self.api_key.read_bytes(), before_key)
        failure_state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(failure_state["stage"], "maintenance")
        self.assertEqual(failure_state["repair_result"], "failed")
        self.assertFalse(failure_state["repair_recovered_update"])
        self.assertIn("fixture manager check failed", failure_state["repair_error"])
        self.assertEqual(self.system_log.read_text().splitlines(), [
            "--user is-enabled matuto-manager.service", "--user stop matuto-manager.service",
            "--user daemon-reload", "--user start matuto-manager.service",
            "--user disable matuto-manager.service",
            "--user show --property=MainPID --value matuto-manager.service",
            "--user stop matuto-manager.service"])

        from matuto.discovery import load_item
        route = load_item("routes", "maintenance")
        route._record_repair("in_progress")
        retry_state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(retry_state["repair_result"], "in_progress")
        self.assertEqual(retry_state["repair_prior_result"], "failed")

        with mock.patch.object(updater, "MANAGER_URL", self.manager_url):
            retry = updater.repair(
                config_root=self.config, data_root=self.data,
                fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive),
                health_check=lambda: updater._health(
                    2, self.manager_url, self.manager_host, "2026.10.0"))

        self.assertEqual(retry.version, "2026.10.0")
        final_state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(final_state["stage"], "complete")
        self.assertEqual(final_state["repair_result"], "complete")
        self.assertFalse(final_state["repair_recovered_update"])
        self._existing_session_reaches_manager()

    def test_repair_manager_stop_failure_records_failed_outcome(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        (updater.active_release(self.data) / "version").unlink()
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)

        def systemctl(arguments):
            if arguments[0] == "stop":
                return subprocess.CompletedProcess(arguments, 1, "fixture stop failure")
            return subprocess.CompletedProcess(arguments, 0, "")

        with mock.patch.object(updater, "_systemctl", side_effect=systemctl):
            with self.assertRaisesRegex(updater.UpdateError, "cannot restart the manager"):
                updater.repair(
                    config_root=self.config, data_root=self.data,
                    fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive))

        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["stage"], "maintenance")
        self.assertEqual(state["repair_result"], "failed")
        self.assertIn("fixture stop failure", state["repair_error"])

    def test_repair_manager_start_failure_records_failed_outcome(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        (updater.active_release(self.data) / "version").unlink()
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        manager_unit = self.config / "systemd/user/matuto-manager.service"
        manager_unit.parent.mkdir(parents=True)
        manager_unit.write_text(bootstrap._manager_unit(self.data))
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)

        def systemctl(arguments):
            if arguments[0] == "is-enabled":
                return subprocess.CompletedProcess(arguments, 0, "disabled")
            if arguments[0] == "start":
                return subprocess.CompletedProcess(arguments, 1, "fixture start failure")
            return subprocess.CompletedProcess(arguments, 0, "")

        with mock.patch.object(updater, "_systemctl", side_effect=systemctl):
            with self.assertRaisesRegex(updater.UpdateError, "manager did not start"):
                updater.repair(
                    config_root=self.config, data_root=self.data,
                    fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive))

        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["stage"], "maintenance")
        self.assertEqual(state["repair_result"], "failed")
        self.assertIn("fixture start failure", state["repair_error"])

    def test_repair_login_start_failure_records_failed_outcome(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        (updater.active_release(self.data) / "version").unlink()
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        manager_unit = self.config / "systemd/user/matuto-manager.service"
        manager_unit.parent.mkdir(parents=True)
        manager_unit.write_text(bootstrap._manager_unit(self.data))
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)

        def systemctl(arguments):
            if arguments[0] == "is-enabled":
                return subprocess.CompletedProcess(arguments, 0, "disabled")
            if arguments[0] == "disable":
                return subprocess.CompletedProcess(arguments, 1, "fixture disable failure")
            return subprocess.CompletedProcess(arguments, 0, "")

        with mock.patch.object(updater, "_systemctl", side_effect=systemctl):
            with self.assertRaisesRegex(updater.UpdateError, "cannot preserve manager login start"):
                updater.repair(
                    config_root=self.config, data_root=self.data,
                    fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive))

        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["stage"], "maintenance")
        self.assertEqual(state["repair_result"], "failed")
        self.assertIn("fixture disable failure", state["repair_error"])

    def test_repair_refuses_a_checkout_menu_entry_without_writing(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        release_dir = updater.active_release(self.data).resolve()
        (release_dir / "version").unlink()
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        entry = self.home / "data/applications/repair-matuto.desktop"
        entry.parent.mkdir(parents=True)
        entry.write_text("\n".join(("[Desktop Entry]", "Name=Repair Matuto",
                                    "Exec=/home/operator/.local/bin/matuto-manager repair", "")))
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)
        before = entry.read_bytes()

        with self.assertRaisesRegex(updater.UpdateError, "menu entry Repair did not write"):
            updater.repair(config_root=self.config, data_root=self.data,
                           fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive))

        self.assertEqual(entry.read_bytes(), before)
        self.assertFalse(self.system_log.exists())
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")

    def test_repair_refuses_indented_foreign_manager_directive_without_writing(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        damaged = updater.active_release(self.data) / "agents/matuto/updater.py"
        damaged.write_text("damaged but untouched\\n")
        (updater.active_release(self.data) / "version").unlink()
        unit = self.config / "systemd/user/matuto-manager.service"
        unit.parent.mkdir(parents=True)
        expected_start = next(line for line in bootstrap._manager_unit(self.data).splitlines()
                              if line.startswith("ExecStart="))
        unit.write_text("[Service]\n" + expected_start +
                        "\n ExecStartPre=/checkout/manager\n")
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)
        before = damaged.read_bytes()

        with self.assertRaisesRegex(updater.UpdateError, "manager unit Repair did not write"):
            updater.repair(config_root=self.config, data_root=self.data,
                           fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive))

        self.assertEqual(damaged.read_bytes(), before)
        self.assertFalse(self.system_log.exists())

    def test_repair_preflights_release_parent_before_changing_permissions(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        (updater.active_release(self.data) / "version").unlink()
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        releases_dir = updater.releases_directory(self.data)
        external = self.home / "external-releases"
        releases_dir.rename(external)
        releases_dir.symlink_to(external, target_is_directory=True)
        self.data.chmod(0o755)
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)

        with self.assertRaisesRegex(updater.UpdateError, "unsafe Matuto directory.*releases"):
            updater.repair(
                config_root=self.config, data_root=self.data,
                fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive))

        self.assertEqual(self.data.stat().st_mode & 0o777, 0o755)
        self.assertFalse(self.system_log.exists())

    def test_repair_fails_closed_on_an_unreadable_release_directory(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        release_dir = updater.active_release(self.data).resolve()
        (release_dir / "version").unlink()
        directory = release_dir / "agents/matuto"
        foreign = directory / "operator-file"
        foreign.write_text("leave alone")
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)

        real_walk = updater.os.walk

        def deny_release_walk(root, *args, **kwargs):
            if Path(root) == release_dir:
                error = PermissionError("fixture directory access denied")
                error.filename = str(directory)
                kwargs["onerror"](error)
                return
            yield from real_walk(root, *args, **kwargs)

        with mock.patch.object(updater.os, "walk", deny_release_walk):
            with self.assertRaisesRegex(updater.UpdateError, "cannot inspect Matuto directory"):
                updater.repair(
                    config_root=self.config, data_root=self.data,
                    fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive))

        self.assertEqual(foreign.read_text(), "leave alone")
        self.assertFalse(self.system_log.exists())

    def test_repair_refuses_an_unowned_empty_release_directory(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        release_dir = updater.active_release(self.data).resolve()
        (release_dir / "version").unlink()
        foreign = release_dir / "agents/matuto/operator-notes"
        foreign.mkdir()
        mode_before = foreign.stat().st_mode & 0o777
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)

        with self.assertRaisesRegex(updater.UpdateError, "unowned installed directory.*operator-notes"):
            updater.repair(
                config_root=self.config, data_root=self.data,
                fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive))

        self.assertEqual(foreign.stat().st_mode & 0o777, mode_before)
        self.assertFalse(self.system_log.exists())

    def test_repair_refuses_an_untracked_repair_temporary_file(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        release_dir = updater.active_release(self.data).resolve()
        (release_dir / "version").unlink()
        foreign = release_dir / "agents/matuto/.matuto-repair-notes"
        foreign.write_text("operator notes")
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)

        with self.assertRaisesRegex(updater.UpdateError, "unowned installed file.*matuto-repair-notes"):
            updater.repair(
                config_root=self.config, data_root=self.data,
                fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive))

        self.assertEqual(foreign.read_text(), "operator notes")
        self.assertFalse(self.system_log.exists())

    def test_repair_resumes_only_a_recorded_temporary_file(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        release_dir = updater.active_release(self.data).resolve()
        (release_dir / "version").unlink()
        temporary = release_dir / "agents/matuto/.matuto-repair-0123456789abcdef0123456789abcdef"
        temporary.write_text("partial release file")
        damaged = release_dir / "agents/matuto/updater.py"
        damaged.write_text("damaged")
        relative_temporary = temporary.relative_to(release_dir).as_posix()
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed,
                     "repair_temporary_files": [relative_temporary]})
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)

        result = updater.repair(
            config_root=self.config, data_root=self.data,
            fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive),
            health_check=lambda: updater._health(2, self.manager_url, self.manager_host,
                                                  "2026.10.0"))

        self.assertEqual(result.version, "2026.10.0")
        self.assertFalse(temporary.exists())
        self.assertEqual(damaged.read_bytes(), (release_dir / "agents/matuto/updater.py").read_bytes())
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["repair_temporary_files"], [])

    def test_repair_refuses_an_unowned_release_file_without_mutating_the_install(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7)
        release_dir = updater.active_release(self.data).resolve()
        (release_dir / "version").unlink()
        foreign = release_dir / "checkout-manager.service"
        foreign.write_text("belongs to the checkout install")
        store.write_update_state(
            self.store_path, "2026.10.0", "complete",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)
        release = reading.release_list.releases[0]
        before = {path.relative_to(release_dir): path.read_bytes()
                  for path in release_dir.rglob("*") if path.is_file()}

        with self.assertRaisesRegex(updater.UpdateError, "unowned installed file.*checkout-manager.service"):
            updater.repair(config_root=self.config, data_root=self.data,
                           fetcher=lambda *_args: (reading, release, archive))

        self.assertEqual({path.relative_to(release_dir): path.read_bytes()
                          for path in release_dir.rglob("*") if path.is_file()}, before)
        self.assertFalse(self.system_log.exists())
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")

    def test_installed_updater_imports_its_stable_dependencies_when_active_store_is_broken(self):
        stable_agents = self.home / "updater-install" / "agents"
        shutil.copytree(Path(__file__).resolve().parents[2], stable_agents,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (updater.active_release(self.data) / "agents" / "matuto" / "store.py").unlink()
        result = subprocess.run([sys.executable, str(stable_agents / "matuto/updater.py"), "--help"],
                                env=os.environ.copy(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("trusted release-feed base URL", result.stdout)

    def _install_stable_updater(self, version="2026.10.0", *, manager_url=None):
        root = self.data / "updater"
        root.mkdir(mode=0o700)
        versions = root / "versions"
        versions.mkdir(mode=0o700)
        target = versions / version
        target.mkdir(mode=0o700)
        shutil.copytree(Path(__file__).resolve().parents[2], target / "agents",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if manager_url is not None:
            script = target / "agents" / "matuto" / "updater.py"
            source = script.read_text()
            source = source.replace('MANAGER_URL = "http://127.0.0.1:8890/"',
                                    f'MANAGER_URL = "{manager_url}"', 1)
            self.assertIn(f'MANAGER_URL = "{manager_url}"', source)
            script.write_text(source)
        (root / "current").symlink_to(f"versions/{version}")
        return root, target / "agents"

    def test_independent_updater_script_rejects_a_pointer_outside_its_versions(self):
        root = self.data / "updater"
        (root / "versions").mkdir(parents=True, mode=0o700)
        os.chmod(root, 0o700)
        os.chmod(root / "versions", 0o700)
        outside = self.home / "outside-updater"
        outside.mkdir(mode=0o700)
        (root / "current").symlink_to(outside)

        with self.assertRaisesRegex(updater.UpdateError, "missing or unsafe"):
            updater.independent_updater_script(self.data)

    def test_successful_update_atomically_replaces_the_verified_external_updater(self):
        root, stable_agents = self._install_stable_updater()
        feed, key, _archive = self._signed_feed()
        before_login = self.login.read_bytes()
        before_key = self.api_key.read_bytes()
        script = """
import base64, os, sys
from pathlib import Path
from types import SimpleNamespace
from matuto import updater
updater.MANAGER_URL = sys.argv[5]
updater.MANAGER_HOST = sys.argv[6]
def health_check():
    assert os.readlink(Path(sys.argv[4]) / 'updater/current') == 'versions/' + sys.argv[8]
    updater._health(2, sys.argv[5], sys.argv[6], sys.argv[7])
result = updater.apply(sys.argv[1], base64.b64decode(sys.argv[2]),
    config_root=sys.argv[3], data_root=sys.argv[4], health_check=health_check,
    serving_reader=lambda: (SimpleNamespace(state='stopped'), []))
assert not result.updater_warning, result.updater_warning
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root / "current" / "agents")
        def run_update(expected_version, old_version):
            return subprocess.run(
                [sys.executable, "-B", "-c", script, feed, base64.b64encode(key).decode(),
                 str(self.config), str(self.data), self.manager_url, self.manager_host,
                 expected_version, old_version], env=env, cwd=self.home,
                capture_output=True, text=True, timeout=120)

        result = run_update("2026.10.1", "2026.10.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        current = root / "current"
        self.assertEqual(os.readlink(current), "versions/2026.10.1")
        self.assertEqual((current / "agents/matuto/updater-release-marker.txt").read_text(),
                         "2026.10.1")
        self._publish_on_feed("2026.10.2", self.home / "release-key-2026.10.1", 8)
        result = run_update("2026.10.2", "2026.10.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(current), "versions/2026.10.2")
        self.assertEqual((current / "agents/matuto/updater-release-marker.txt").read_text(),
                         "2026.10.2")
        self.assertEqual(self.login.read_bytes(), before_login)
        self.assertEqual(self.api_key.read_bytes(), before_key)
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertFalse(state["updater_swap_pending"])

    def test_updater_from_an_equal_or_older_release_never_replaces_the_installed_copy(self):
        root, stable_agents = self._install_stable_updater("2026.10.1")
        before = {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                  for path in stable_agents.rglob("*") if path.is_file()}
        original_file = updater.__file__
        updater.__file__ = str(root / "current/agents/matuto/updater.py")
        try:
            self.assertEqual(updater._replace_installed_updater(
                updater.active_release(self.data), self.data, "2026.10.0", "unused"), "")
        finally:
            updater.__file__ = original_file
        after = {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                 for path in stable_agents.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(os.readlink(root / "current"), "versions/2026.10.1")

    def test_altered_activated_updater_keeps_old_copy_and_reports_success(self):
        root, stable_agents = self._install_stable_updater()
        before = {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                  for path in stable_agents.rglob("*") if path.is_file()}
        feed, key, _archive = self._signed_feed()
        original_file = updater.__file__
        original_extract = updater._extract
        updater.__file__ = str(root / "current/agents/matuto/updater.py")

        def alter_updater(*args, **kwargs):
            release_path = original_extract(*args, **kwargs)
            altered = release_path / "agents/matuto/updater.py"
            altered.write_text("altered after verification")
            manifest_path = release_path / "RELEASE.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"]["agents/matuto/updater.py"] = hashlib.sha256(
                altered.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            return release_path

        try:
            with mock.patch.object(updater, "_extract", side_effect=alter_updater):
                result = self.apply(feed, key)
        finally:
            updater.__file__ = original_file
        self.assertEqual(result.version, "2026.10.1")
        self.assertIn("checksum list was altered", result.updater_warning)
        after = {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                 for path in stable_agents.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(os.readlink(root / "current"), "versions/2026.10.0")
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")

    def test_release_without_an_updater_does_not_block_a_later_forward_update(self):
        root, _stable_agents = self._install_stable_updater()
        feed, key, _archive = self._signed_feed(missing_updater=True)
        original_file = updater.__file__
        updater.__file__ = str(root / "current/agents/matuto/updater.py")
        try:
            first = self.apply(feed, key)
            self.assertIn("no checksummed updater", first.updater_warning)
            pending = json.loads(store.read_update_state(self.store_path)["phase"])
            self.assertTrue(pending["updater_swap_pending"])

            self._publish_on_feed("2026.10.2", self.home / "release-key-2026.10.1", 8)
            second = self.apply(
                feed, key, health_check=lambda: updater._health(
                    2, self.manager_url, self.manager_host, "2026.10.2"))
        finally:
            updater.__file__ = original_file
        self.assertEqual(second.version, "2026.10.2")
        self.assertFalse(second.updater_warning)
        self.assertEqual(os.readlink(root / "current"), "versions/2026.10.2")
        self.assertEqual((root / "current/agents/matuto/updater-release-marker.txt").read_text(),
                         "2026.10.2")
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertFalse(state["updater_swap_pending"])

    def test_release_without_an_updater_keeps_the_old_copy_and_reports_success(self):
        root, stable_agents = self._install_stable_updater()
        before = {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                  for path in stable_agents.rglob("*") if path.is_file()}
        feed, key, _archive = self._signed_feed(missing_updater=True)
        original_file = updater.__file__
        updater.__file__ = str(root / "current/agents/matuto/updater.py")
        try:
            result = self.apply(feed, key)
        finally:
            updater.__file__ = original_file
        self.assertEqual(result.version, "2026.10.1")
        self.assertIn("no checksummed updater", result.updater_warning)
        after = {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                 for path in stable_agents.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(os.readlink(root / "current"), "versions/2026.10.0")
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")

    def test_copied_bytes_are_the_verified_release_bytes(self):
        root, _stable_agents = self._install_stable_updater()
        feed, key, _archive = self._signed_feed()
        original_file = updater.__file__
        original_updater_files = updater._updater_files
        altered = b"broken after checksum verification\\n"
        updater.__file__ = str(root / "current/agents/matuto/updater.py")

        def alter_source_after_verification(release_path, manifest_digest):
            verified = original_updater_files(release_path, manifest_digest)
            (release_path / "agents/matuto/updater.py").write_bytes(altered)
            return verified

        try:
            with mock.patch.object(updater, "_updater_files",
                                   side_effect=alter_source_after_verification):
                result = self.apply(feed, key)
        finally:
            updater.__file__ = original_file
        self.assertEqual(result.version, "2026.10.1")
        self.assertFalse(result.updater_warning)
        installed = root / "current/agents/matuto/updater.py"
        manifest = json.loads((updater.active_release(self.data) / "RELEASE.json").read_text())
        self.assertEqual(hashlib.sha256(installed.read_bytes()).hexdigest(),
                         manifest["files"]["agents/matuto/updater.py"])
        self.assertNotEqual(installed.read_bytes(), altered)

    def test_refusal_and_migration_failure_keep_the_external_updater_unchanged(self):
        root, stable_agents = self._install_stable_updater()
        before = {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                  for path in stable_agents.rglob("*") if path.is_file()}
        original_file = updater.__file__
        updater.__file__ = str(root / "current/agents/matuto/updater.py")
        try:
            feed, key, _archive = self._signed_feed()
            with mock.patch.object(releases, "read_list",
                                   side_effect=releases.ReleaseError("fixture rejected list")), \
                    self.assertRaises(releases.ReleaseError):
                self.apply(feed, key)
            self.assertEqual(os.readlink(root / "current"), "versions/2026.10.0")
            self.assertEqual(
                {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                 for path in stable_agents.rglob("*") if path.is_file()}, before)

            feed, key, _archive = self._signed_feed(
                version="2026.10.2", migration_fails=True)
            with self.assertRaisesRegex(updater.UpdateError, "migration"):
                self.apply(feed, key)
        finally:
            updater.__file__ = original_file
        after = {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                 for path in stable_agents.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(os.readlink(root / "current"), "versions/2026.10.0")
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")

    def test_manager_health_failure_keeps_the_external_updater_unchanged(self):
        root, stable_agents = self._install_stable_updater()
        before = {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                  for path in stable_agents.rglob("*") if path.is_file()}
        original_file = updater.__file__
        updater.__file__ = str(root / "current/agents/matuto/updater.py")
        try:
            feed, key, _archive = self._signed_feed()
            with self.assertRaisesRegex(updater.UpdateError, "remains in maintenance"):
                self.apply(feed, key, health_check=lambda: (_ for _ in ()).throw(
                    updater.UpdateError("manager did not serve")))
        finally:
            updater.__file__ = original_file
        after = {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                 for path in stable_agents.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(os.readlink(root / "current"), "versions/2026.10.0")
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")

    def test_failed_newer_release_preserves_the_previous_pending_swap(self):
        root, _stable_agents = self._install_stable_updater()
        feed, key, _archive = self._signed_feed()
        original_file = updater.__file__
        original_replace = os.replace
        updater.__file__ = str(root / "current/agents/matuto/updater.py")

        def stop_before_pointer(source, destination):
            if Path(destination) == root / "current":
                raise OSError("fixture interruption before pointer replacement")
            return original_replace(source, destination)

        try:
            with mock.patch.object(os, "replace", side_effect=stop_before_pointer):
                first = self.apply(feed, key)
            self.assertIn("fixture interruption", first.updater_warning)
            pending = json.loads(store.read_update_state(self.store_path)["phase"])
            self.assertTrue(pending["updater_swap_pending"])
            self.assertEqual(pending["updater_swap_version"], "2026.10.1")
            first_manifest = pending["updater_swap_manifest_sha256"]

            self._publish_on_feed("2026.10.2", self.home / "release-key-2026.10.1", 8)
            with mock.patch.object(os, "replace", side_effect=stop_before_pointer), \
                    mock.patch.object(updater, "_stage_store",
                                      side_effect=updater.UpdateError("fixture migration failed")):
                with self.assertRaisesRegex(updater.UpdateError, "migration"):
                    self.apply(feed, key)
            failed = json.loads(store.read_update_state(self.store_path)["phase"])
            self.assertEqual(failed["stage"], "failed")
            self.assertEqual(failed["version"], "2026.10.2")
            self.assertTrue(failed["updater_swap_pending"])
            self.assertEqual(failed["updater_swap_version"], "2026.10.1")
            self.assertEqual(failed["updater_swap_manifest_sha256"], first_manifest)
            self.assertEqual(os.readlink(root / "current"), "versions/2026.10.0")
            self.assertEqual(updater.installed_version(self.data), "2026.10.1")

            def no_feed(*_args):
                raise updater.UpdateError("fixture feed stop")

            with self.assertRaisesRegex(updater.UpdateError, "fixture feed stop"):
                updater.apply(feed, key, config_root=self.config, data_root=self.data,
                              serving_reader=self.serving_reader, fetcher=no_feed)
        finally:
            updater.__file__ = original_file
        self.assertEqual(os.readlink(root / "current"), "versions/2026.10.1")
        recovered = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertFalse(recovered["updater_swap_pending"])
        self.assertEqual(recovered["updater_swap_version"], "2026.10.1")

    def test_failed_pointer_switch_keeps_old_updater_and_pending_retry_finishes(self):
        root, stable_agents = self._install_stable_updater()
        archive, _entry = self._fixture_archive("2026.10.1")
        verified, _size = updater._archive_members(archive)
        release_path = updater._extract(self.data, "2026.10.1", archive, verified)
        updater._atomic_current(self.data, "2026.10.1")
        manifest_digest = updater._release_manifest_digest(archive)
        store.write_update_state(
            self.store_path, "2026.10.1", "complete",
            details={"updater_swap_pending": True,
                     "updater_manifest_sha256": manifest_digest})
        old_files = {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                     for path in stable_agents.rglob("*") if path.is_file()}
        original_file = updater.__file__
        updater.__file__ = str(root / "current/agents/matuto/updater.py")
        original_replace = os.replace

        def stop_before_pointer(source, destination):
            if Path(destination) == root / "current":
                raise OSError("fixture interruption before pointer replacement")
            return original_replace(source, destination)

        try:
            with mock.patch.object(os, "replace", side_effect=stop_before_pointer):
                warning = updater._replace_installed_updater(
                    release_path, self.data, "2026.10.1", manifest_digest)
            self.assertIn("fixture interruption", warning)
            self.assertEqual(os.readlink(root / "current"), "versions/2026.10.0")
            self.assertEqual(
                {path.relative_to(stable_agents): hashlib.sha256(path.read_bytes()).digest()
                 for path in stable_agents.rglob("*") if path.is_file()}, old_files)

            def no_feed(*_args):
                raise updater.UpdateError("fixture feed stop")

            with self.assertRaisesRegex(updater.UpdateError, "fixture feed stop"):
                updater.apply("https://feed.invalid/", b"unused", config_root=self.config,
                              data_root=self.data, serving_reader=self.serving_reader,
                              fetcher=no_feed)
        finally:
            updater.__file__ = original_file
        self.assertEqual(os.readlink(root / "current"), "versions/2026.10.1")
        self.assertEqual((root / "current/agents/matuto/updater-release-marker.txt").read_text(),
                         "2026.10.1")
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertFalse(state["updater_swap_pending"])

    def test_pending_swap_is_completed_before_the_next_update_check(self):
        root, _stable_agents = self._install_stable_updater()
        archive, _entry = self._fixture_archive("2026.10.1")
        verified, _size = updater._archive_members(archive)
        updater._extract(self.data, "2026.10.1", archive, verified)
        updater._atomic_current(self.data, "2026.10.1")
        store.write_update_state(
            self.store_path, "2026.10.1", "complete",
            details={"updater_swap_pending": True,
                     "updater_manifest_sha256": updater._release_manifest_digest(archive)})

        def no_feed(*_args):
            raise updater.UpdateError("fixture feed stop")

        original_file = updater.__file__
        updater.__file__ = str(root / "current/agents/matuto/updater.py")
        try:
            with self.assertRaisesRegex(updater.UpdateError, "fixture feed stop"):
                updater.apply("https://feed.invalid/", b"unused", config_root=self.config,
                              data_root=self.data, serving_reader=self.serving_reader,
                              fetcher=no_feed)
        finally:
            updater.__file__ = original_file
        self.assertEqual(os.readlink(root / "current"), "versions/2026.10.1")
        self.assertEqual((root / "current/agents/matuto/updater-release-marker.txt").read_text(),
                         "2026.10.1")
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertFalse(state["updater_swap_pending"])

    def test_stable_updater_can_install_again_after_a_schema_change(self):
        connection = sqlite3.connect(self.store_path)
        try:
            connection.execute("UPDATE store_meta SET value=? WHERE key='schema_version'",
                               (str(self.target_schema),))
            connection.execute("CREATE TABLE updater_migration_probe (value TEXT NOT NULL)")
            connection.execute("INSERT INTO updater_migration_probe VALUES ('migrated')")
            connection.commit()
        finally:
            connection.close()
        feed, key, _archive = self._signed_feed(version="2026.10.1")
        result = updater.apply(
            feed, key, config_root=self.config, data_root=self.data,
            health_check=lambda: updater._health(
                2, self.manager_url, self.manager_host, "2026.10.1"))
        self.assertEqual(result.version, "2026.10.1")
        self.assertEqual(store.read_schema_version(self.store_path), self.target_schema)
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")

    def test_default_admission_settles_a_completed_job_before_refusing(self):
        launched = runner.Runner(
            self.config, serving_state=lambda: admission.STOPPED,
            card_holder=lambda: admission.Card(admission.CARD_FREE))
        try:
            launched._answers = lambda: True
            launched.launch = lambda *_args: type("Result", (), {"returncode": 0})()
            result = launched.start(runner.Request.job(
                admission.BUILD, "completed build", ("/bin/true",)))
            self.assertEqual(result.decision, admission.ADMIT)
            answer = {runner.LOAD_STATE: "loaded", runner.ACTIVE_STATE: "inactive",
                      runner.SUB_STATE: "dead", runner.RESULT: "success",
                      runner.EXIT_STATUS: "0", runner.END_MOMENT: "100"}
            with mock.patch.object(runner.Runner, "_unit_state", return_value=answer):
                updater._admit(self.config)
            self.assertEqual(launched.work(result.work.work_id).outcome, runner.COMPLETED)
            later = launched.start(runner.Request.job(
                admission.BUILD, "finished during admission", ("/bin/true",)))
            self.assertEqual(later.decision, admission.ADMIT)
        finally:
            launched.close()
        with mock.patch.object(runner.Runner, "_unit_state", return_value=answer):
            opened = store.open_store(self.config, exclusive=True)
            try:
                updater._admit_under_lock(opened, self.config, None)
                self.assertEqual(runner.unfinished(opened.connection()), [])
            finally:
                opened.close()

    def test_expected_manager_sign_in_does_not_hide_a_broken_store_opener(self):
        release = updater.active_release(self.data)
        started = updater._systemctl(["start", updater.MANAGER_UNIT])
        self.assertEqual(started.returncode, 0)
        store_file = release / "agents" / "matuto" / "store.py"
        source = store_file.read_text()
        source = source.replace(
            "def open_store(config_root=None, *, exclusive=False, allow_newer=False,\n"
            "               allow_update_read=False):",
            "def open_store(config_root=None, *, exclusive=False, allow_newer=False,\n"
            "               allow_update_read=False):\n"
            "    raise StoreError('fixture expected manager cannot open its database')", 1)
        store_file.write_text(source)
        with mock.patch.object(updater, "MANAGER_URL", self.manager_url), \
                mock.patch.object(updater, "MANAGER_HOST", self.manager_host):
            with self.assertRaisesRegex(
                    updater.UpdateError, "fixture expected manager cannot open its database"):
                updater._check_manager(None, release, "2026.10.0", self.config)

    def test_unrelated_sign_in_server_cannot_pass_manager_identity_check(self):
        class Unrelated(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<h1>Sign in</h1>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Unrelated)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            with mock.patch.object(
                    updater, "MANAGER_URL", f"http://127.0.0.1:{server.server_port}/"):
                with self.assertRaisesRegex(updater.UpdateError, "expected manager from the active release"):
                    updater._check_manager(None, updater.active_release(self.data), "2026.10.0",
                                           self.config)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_live_store_write_does_not_stop_the_manager(self):
        feed, key, _archive = self._signed_feed()
        started = updater._systemctl(["start", updater.MANAGER_UNIT])
        self.assertEqual(started.returncode, 0)
        writer = store.open_store(self.config)
        connection = writer.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO store_meta (key, value) VALUES ('pending_writer_probe', 'uncommitted')")
            with self.assertRaisesRegex(store.StoreError, "update is in progress"):
                self.apply(feed, key)
            self.assertTrue(self.pid_file.exists())
            os.kill(int(self.pid_file.read_text()), 0)
            self.assertEqual(self.system_log.read_text().splitlines(),
                             ["--user start matuto-manager.service"])
        finally:
            connection.execute("ROLLBACK")
            writer.close()

    def test_release_checks_refuse_before_stopping_or_changing_the_install(self):
        feed, key, _archive = self._signed_feed()
        self.assertEqual(updater._systemctl(["start", updater.MANAGER_UNIT]).returncode, 0)
        for module, check, number in ((releases, "read_list", None),
                                      (releases, "check_release_file", 7),
                                      (updater, "_archive_members", 7)):
            with self.subTest(check=check), mock.patch.object(
                    module, check, side_effect=releases.ReleaseError("fixture rejected release")):
                self.assert_refused_unchanged(feed, key, releases.ReleaseError,
                                             list_number=number)
                os.kill(int(self.pid_file.read_text()), 0)

    def test_legacy_snapshot_phase_keeps_the_previously_accepted_list_number(self):
        opened = store.open_store(self.config)
        try:
            connection = opened.connection()
            connection.execute(
                "INSERT INTO update_state (state_id, release_version, phase, updated_at)"
                " VALUES (1, '2026.10.0', 'snapshot verified', 'now')")
            connection.execute(
                "INSERT INTO store_meta (key, value) VALUES ('highest_release_list_number', '8')")
            self.assertEqual(updater._highest_list_number(connection, self.data), 8)
        finally:
            opened.close()

    def test_verified_list_number_survives_a_failed_archive_download(self):
        feed, key, _archive = self._signed_feed(number=8)
        before = self.store_path.read_bytes()
        FeedHandler.assets.pop("/matuto-2026.10.1.release")
        with self.assertRaisesRegex(updater.UpdateError, "HTTP 404"):
            self.apply(feed, key)
        opened = store.open_store(self.config)
        try:
            self.assertEqual(updater._highest_list_number(opened.connection(), self.data), 8)
        finally:
            opened.close()
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertFalse(updater.snapshot.snapshot_path(self.store_path).exists())
        self.assertFalse(self.system_log.exists())

    def test_release_not_newer_than_installed_is_refused_unchanged(self):
        feed, key, _archive = self._signed_feed(version="2026.10.0")
        self.assert_refused_unchanged(feed, key, updater.UpdateError, "not newer than installed",
                                      list_number=7)

    def test_repair_replays_a_hot_update_journal_before_reading_state(self):
        feed, key, archive = self._signed_feed(version="2026.10.0", number=7,
                                               same_version=True)
        (updater.active_release(self.data) / "version").unlink()
        store.write_update_state(
            self.store_path, "2026.10.1", "preparing",
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        script = """
import os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from matuto import store
class Crash(sqlite3.Connection):
    def execute(self, sql, *args):
        if sql == 'COMMIT': os._exit(94)
        return super().execute(sql, *args)
def connect(path):
    connection = sqlite3.connect(str(path), isolation_level=None, factory=Crash)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute('PRAGMA cache_size = 1')
    return connection
store._connect = connect
rows = [[str(index).zfill(64), 'operator', 'generation', 'created', 'expires']
        for index in range(50000)]
store.write_update_state(Path(sys.argv[2]), '2026.10.1', 'activating',
                         details={'expected_sessions': rows})
"""
        process = subprocess.run(
            [sys.executable, "-B", "-c", script,
             str(Path(__file__).resolve().parents[2]), str(self.store_path)],
            env=os.environ.copy(), cwd=self.home, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 94, process.stderr)
        journal = Path(f"{self.store_path}-journal")
        self.assertTrue(journal.exists())
        reading = releases.read_list(FeedHandler.assets["/release-list.json"],
                                     FeedHandler.assets["/release-list.json.sig"], key)
        with mock.patch.object(updater, "MANAGER_URL", self.manager_url):
            result = updater.repair(
                config_root=self.config, data_root=self.data,
                fetcher=lambda *_args: (reading, reading.release_list.releases[0], archive),
                health_check=lambda: updater._health(
                    2, self.manager_url, self.manager_host, "2026.10.0"))
        self.assertEqual(result.version, "2026.10.0")
        self.assertFalse(journal.exists())
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertEqual(store.read_schema_version(self.store_path), self.initial_schema)
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["stage"], "complete")
        self.assertEqual(state["repair_result"], "complete")

    def test_interrupted_schema_change_recovers_forward_after_a_second_crash(self):
        feed, key, _archive = self._signed_feed()
        env = os.environ.copy()
        agents_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = agents_root + os.pathsep + env.get("PYTHONPATH", "")
        args = [feed, base64.b64encode(key).decode(), str(self.config), str(self.data),
                self.manager_url, self.manager_host]
        crash_before_staged_ledger = """
import base64, os, sys
from pathlib import Path
from types import SimpleNamespace
from matuto import store, updater
original = store.write_update_state
def crash(path, version, phase, **kwargs):
    if phase == 'activating' and Path(path).name.startswith(store.MIGRATION_TEMP_PREFIX):
        os._exit(91)
    return original(path, version, phase, **kwargs)
store.write_update_state = crash
updater.apply(sys.argv[1], base64.b64decode(sys.argv[2]), config_root=sys.argv[3],
              data_root=sys.argv[4],
              health_check=lambda: updater._health(2, sys.argv[5], sys.argv[6], '2026.10.1'),
              serving_reader=lambda: (SimpleNamespace(state='stopped'), []))
"""
        first = subprocess.run([sys.executable, "-B", "-c", crash_before_staged_ledger, *args],
                               env=env, cwd=self.home, capture_output=True, text=True, timeout=120)
        self.assertEqual(first.returncode, 91, first.stderr)

        crash_during_recovery = """
import os, sys
from pathlib import Path
from matuto import store, updater
original = os.replace
def crash(src, dst):
    result = original(src, dst)
    if Path(src).name.startswith(store.MIGRATION_TEMP_PREFIX) and Path(dst) == store.store_path(sys.argv[1]):
        os._exit(92)
    return result
os.replace = crash
updater._recover(sys.argv[1], sys.argv[2])
"""
        second = subprocess.run([sys.executable, "-B", "-c", crash_during_recovery,
                                 str(self.config), str(self.data)], env=env, cwd=self.home,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 92, second.stderr)
        self.assertEqual(store.read_schema_version(self.store_path), self.target_schema)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")

        def should_not_fetch(*_args):
            self.fail("forward recovery must finish before fetching another release")

        result = updater.apply(
            feed, key, config_root=self.config, data_root=self.data,
            health_check=lambda: updater._health(2, self.manager_url, self.manager_host, "2026.10.1"),
            serving_reader=self.serving_reader, fetcher=should_not_fetch)
        self.assertEqual(result.version, "2026.10.1")
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")
        self.assertEqual(store.read_schema_version(self.store_path), self.target_schema)
        self.assertEqual(store.read_sessions_at(self.store_path), self.expected_sessions)
        self.assertTrue(self._active_session_valid())
        self._existing_session_reaches_manager()
        self.assertEqual(self.login.read_bytes(), self.initial_login)
        self.assertEqual(self.api_key.read_bytes(), b"router-api-key\n")
        connection = sqlite3.connect(f"file:{self.store_path}?mode=ro", uri=True)
        try:
            self.assertEqual(connection.execute(
                "SELECT value FROM updater_migration_probe").fetchone()[0], "migrated")
            state = json.loads(store.read_update_state(self.store_path)["phase"])
            self.assertEqual(state["stage"], "complete")
        finally:
            connection.close()

    def test_lost_staged_database_before_replacement_keeps_the_old_release(self):
        staged = store.stage_migrations(self.config)
        target = updater.releases_directory(self.data) / "2026.10.1"
        target.mkdir(mode=0o700)
        staged.unlink()
        store.write_update_state(
            self.store_path, "2026.10.1", "activating",
            details={"staged_store": str(staged), "release_path": str(target),
                     "schema_version": self.target_schema})

        def stop_fetch(*_args):
            raise updater.UpdateError("fixture fetch stop")

        with self.assertRaisesRegex(updater.UpdateError, "fixture fetch stop"):
            updater.apply("https://feed.invalid/", b"unused", config_root=self.config,
                          data_root=self.data,
                          health_check=lambda: updater._health(
                              2, self.manager_url, self.manager_host, "2026.10.0"),
                          serving_reader=self.serving_reader, fetcher=stop_fetch)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertEqual(store.read_schema_version(self.store_path), self.initial_schema)
        self.assertFalse(target.exists())
        self.assertEqual(json.loads(store.read_update_state(self.store_path)["phase"])["stage"],
                         "failed")

    def test_interrupted_snapshot_stage_keeps_original_snapshot_and_removes_partial_files(self):
        opened = store.open_store(self.config)
        saved = updater.snapshot.take(opened, "2026.10.1")
        opened.close()
        staged = store.stage_migrations(self.config)
        partial = updater.releases_directory(self.data) / ".staging-2026.10.1"
        partial.mkdir(mode=0o700)
        (partial / "partial").write_text("interrupted")
        store.write_update_state(self.store_path, "2026.10.1", "preparing")
        snapshot_digest = hashlib.sha256(saved.path.read_bytes()).digest()

        def stop_fetch(*_args):
            raise updater.UpdateError("fixture fetch stop")

        with self.assertRaisesRegex(updater.UpdateError, "fixture fetch stop"):
            updater.apply("https://feed.invalid/", b"unused", config_root=self.config,
                          data_root=self.data, health_check=lambda: updater._health(
                              2, self.manager_url, self.manager_host, "2026.10.0"),
                          serving_reader=self.serving_reader, fetcher=stop_fetch)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertEqual(store.read_schema_version(self.store_path), self.initial_schema)
        self.assertFalse(partial.exists())
        self.assertFalse(staged.exists())
        self.assertEqual(hashlib.sha256(saved.path.read_bytes()).digest(), snapshot_digest)
        self.assertEqual(json.loads(store.read_update_state(self.store_path)["phase"])["stage"], "failed")

    def test_interrupted_preparation_keeps_old_release_and_cleans_staging(self):
        staged = store.stage_migrations(self.config)
        target = updater.releases_directory(self.data) / "2026.10.1"
        target.mkdir(mode=0o700)
        store.write_update_state(self.store_path, "2026.10.1", "prepared",
                                details={"staged_store": str(staged), "release_path": str(target)})

        def stop_fetch(*_args):
            raise updater.UpdateError("fixture fetch stop")

        with self.assertRaisesRegex(updater.UpdateError, "fixture fetch stop"):
            updater.apply("https://feed.invalid/", b"unused", config_root=self.config,
                          data_root=self.data, health_check=lambda: updater._health(
                              2, self.manager_url, self.manager_host, "2026.10.0"),
                          serving_reader=self.serving_reader, fetcher=stop_fetch)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertEqual(store.read_schema_version(self.store_path), self.initial_schema)
        self.assertFalse(target.exists())
        self.assertFalse(staged.exists())
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["stage"], "failed")
        self.assertEqual(self.system_log.read_text().splitlines(), [
            "--user stop matuto-manager.service", "--user is-active matuto-manager.service",
            "--user start matuto-manager.service",
            "--user show --property=MainPID --value matuto-manager.service"])

    def test_second_updater_is_refused_before_stopping_the_manager(self):
        lock_fd = updater._updater_lock(self.data)
        try:
            with self.assertRaisesRegex(updater.UpdateError, "another Matuto update is already running"):
                updater.apply("https://feed.invalid/", b"unused", config_root=self.config,
                              data_root=self.data, serving_reader=self.serving_reader)
        finally:
            os.close(lock_fd)
        self.assertFalse(self.system_log.exists())

    def test_work_started_after_admission_is_seen_under_the_update_lock(self):
        feed, key, _archive = self._signed_feed()
        original_stop = updater._stop_before_update

        admitted_store = []

        def start_work_after_stop():
            original_stop()
            launched = runner.Runner(
                self.config, serving_state=lambda: admission.STOPPED,
                card_holder=lambda: admission.Card(admission.CARD_FREE))
            try:
                launched._answers = lambda: True
                launched.launch = lambda *_args: type("Result", (), {"returncode": 0})()
                result = launched.start(runner.Request.job(
                    admission.BUILD, "late build", ("/bin/true",)))
                self.assertEqual(result.decision, admission.ADMIT)
                admitted_store.append(self.store_path.read_bytes())
            finally:
                launched.close()

        with mock.patch.object(updater, "_stop_before_update", start_work_after_stop):
            with self.assertRaisesRegex(updater.UpdateError, "build:"):
                self.apply(feed, key)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertEqual(store.read_schema_version(self.store_path), self.initial_schema)
        self.assertEqual(self.store_path.read_bytes(), admitted_store[0])
        self.assertEqual(self.system_log.read_text().splitlines(), [
            "--user stop matuto-manager.service", "--user is-active matuto-manager.service",
            "--user start matuto-manager.service"])

    def test_pinned_install_reconciles_ended_work_after_entering_the_install_phase(self):
        from matuto import serving_state
        feed, key, _archive = self._signed_feed(version="2026.10.1", number=7)
        unit = updater.app_install_unit_name()
        store.write_update_state(
            self.store_path, "2026.10.0", "complete", list_number=6,
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        job_unit = "matuto-build-ended-fixture.service"
        opened = store.open_store(self.config)
        try:
            opened.connection().execute(
                "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at) "
                "VALUES ('ended-fixture', 'build', ?, 'fixture build', '2026-10-14T12:00:00Z')",
                (job_unit,))
        finally:
            opened.close()
        self.assertTrue(store.begin_app_install(
            self.store_path, "2026.10.1", 7, unit,
            verified_offer={"version": "2026.10.1", "withdrawn": False}))

        original_unit_state = runner.Runner._unit_state

        def unit_state(reader, name):
            if name == job_unit:
                return runner._properties(f"{runner.LOAD_STATE}={runner.NOT_FOUND}")
            return original_unit_state(reader, name)

        with (mock.patch.object(serving_state, "read", return_value=serving_state.State("stopped")),
              mock.patch.object(runner.Runner, "_unit_state", new=unit_state),
              mock.patch.object(runner.Runner, "_launching", return_value=False),
              mock.patch.object(updater, "_health")):
            status = updater.main(["update", "--version", "2026.10.1", "--list-number", "7",
                                   "--install-unit", unit])

        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(status, 0)
        self.assertIsNone(state.get("app_install_pending"))
        self.assertEqual(state["install_result"]["stage"], "complete")
        self.assertEqual(state["install_result"]["version"], "2026.10.1")
        connection = sqlite3.connect(self.store_path)
        try:
            job = connection.execute(
                "SELECT outcome FROM jobs WHERE job_id = 'ended-fixture'").fetchone()
        finally:
            connection.close()
        self.assertEqual(job[0], "interrupted")

    def test_running_work_blocks_update_without_stopping_the_manager(self):
        feed, key, _archive = self._signed_feed()
        serving_reader = lambda: (type("Serving", (), {"state": "stopped"})(), [object()])
        self.assert_refused_unchanged(feed, key, updater.UpdateError, "must stop",
                                      list_number=7, serving_reader=serving_reader)

    def test_manager_health_failure_leaves_the_new_release_in_maintenance(self):
        feed, key, _archive = self._signed_feed()

        def failed_health():
            raise updater.UpdateError("fixture manager did not open the database")

        with self.assertRaisesRegex(updater.UpdateError, "remains in maintenance"):
            self.apply(feed, key, health_check=failed_health)
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")
        self.assertEqual(store.read_schema_version(self.store_path), self.target_schema)
        maintenance = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(maintenance["stage"], "maintenance")
        self.assertEqual(maintenance["reason"], "fixture manager did not open the database")
        self.assertEqual(self.system_log.read_text().splitlines(), [
            "--user stop matuto-manager.service", "--user is-active matuto-manager.service",
            "--user start matuto-manager.service",
            "--user show --property=MainPID --value matuto-manager.service",
            "--user stop matuto-manager.service"] )

    def test_forward_recovery_never_completes_after_a_session_is_lost(self):
        feed, key, _archive = self._signed_feed()

        def remove_session_then_check_manager():
            connection = sqlite3.connect(self.store_path)
            try:
                connection.execute("DELETE FROM sessions")
                connection.commit()
            finally:
                connection.close()
            updater._health(2, self.manager_url, self.manager_host, "2026.10.1")

        with self.assertRaisesRegex(updater.UpdateError, "one or more browser sessions were lost"):
            self.apply(feed, key, health_check=remove_session_then_check_manager)
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["stage"], "maintenance")
        self.assertEqual(state["expected_sessions"],
                         [list(session) for session in self.expected_sessions])

        with self.assertRaisesRegex(updater.UpdateError, "forward recovery manager check failed"):
            updater.apply(feed, key, config_root=self.config, data_root=self.data,
                          health_check=lambda: updater._health(
                              2, self.manager_url, self.manager_host, "2026.10.1"),
                          serving_reader=self.serving_reader)
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["stage"], "maintenance")
        self.assertEqual(state["reason"], "one or more browser sessions were lost during the update")
        self.assertEqual(updater.installed_version(self.data), "2026.10.1")

        def fail_recovery_start(arguments):
            if arguments == ["start", updater.MANAGER_UNIT]:
                return subprocess.CompletedProcess(arguments, 1,
                                                   "fixture: unit file has an invalid setting")
            if arguments == ["is-active", updater.MANAGER_UNIT]:
                return subprocess.CompletedProcess(arguments, 0, "inactive")
            return subprocess.CompletedProcess(arguments, 0, "")

        with mock.patch.object(updater, "_systemctl", side_effect=fail_recovery_start):
            with self.assertRaisesRegex(updater.UpdateError, "fixture: unit file has an invalid setting"):
                updater.apply(feed, key, config_root=self.config, data_root=self.data,
                              serving_reader=self.serving_reader)
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["stage"], "maintenance")
        self.assertIn("fixture: unit file has an invalid setting", state["reason"])

    def test_failed_migration_keeps_v1_active_and_the_original_store(self):
        feed, key, _archive = self._signed_feed(migration_fails=True)
        original_sessions = store.read_sessions_at(self.store_path)
        with self.assertRaisesRegex(updater.UpdateError, "migration"):
            self.apply(feed, key)
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertEqual(store.read_schema_version(self.store_path), self.initial_schema)
        self.assertEqual(store.read_sessions_at(self.store_path), original_sessions)
        self.assertFalse((updater.releases_directory(self.data) / "2026.10.1").exists())
        snapshot = updater.snapshot.snapshot_path(self.store_path)
        self.assertTrue(snapshot.exists())
        self.assertEqual(updater.snapshot._inspect(snapshot)[1], "2026.10.1")
        self.assertEqual(json.loads(store.read_update_state(self.store_path)["phase"])["stage"], "failed")
        self.assertEqual(self.system_log.read_text().splitlines(), [
            "--user stop matuto-manager.service", "--user is-active matuto-manager.service",
            "--user start matuto-manager.service"])

    def test_unknown_serving_state_refuses_before_stopping_the_manager(self):
        feed, key, _archive = self._signed_feed()
        state = type("Serving", (), {"state": "unknown"})()
        with self.assertRaisesRegex(updater.UpdateError, "serving and all runs and jobs must stop"):
            self.apply(feed, key, serving_reader=lambda: (state, []))
        self.assertEqual(updater.installed_version(self.data), "2026.10.0")
        self.assertFalse(self.system_log.exists())

    def test_low_space_refuses_before_stopping_or_changing_the_install(self):
        feed, key, _archive = self._signed_feed()
        self.assert_refused_unchanged(feed, key, updater.UpdateError,
                                      "not enough free space", list_number=7, free_bytes=0)

    def test_update_menu_entry_with_no_offer_opens_maintenance_after_owner_check(self):
        store.write_update_state(self.store_path, "2026.10.0", "complete")
        with (
            mock.patch.object(updater, "_resolved_loopback_addresses", return_value={"127.0.0.1"}),
            mock.patch.object(updater, "_listening_owners", return_value=set()) as owners,
            mock.patch.object(updater, "_error_page", return_value=0) as error_page,
            mock.patch.object(updater.subprocess, "run",
                             return_value=subprocess.CompletedProcess([], 0, "")) as run,
        ):
            self.assertEqual(updater.main(["update"]), 0)
        owners.assert_called_once_with({"127.0.0.1", updater._manager_health_address()})
        self.assertIn("There are no public releases yet.", error_page.call_args.args[0])
        self.assertEqual(run.call_args.args[0], [
            "xdg-open", updater.MANAGER_OPEN_URL + "#/settings/maintenance"])

    def test_recovery_page_checks_prerequisites_from_the_fresh_offer(self):
        from matuto import updates
        offer = {"version": "2026.10.1", "prerequisites": ["fresh-package"],
                 "withdrawn": False}
        with (mock.patch.object(updates, "check", return_value={
                  "offer": offer, "listNumber": 8, "installedVersion": "2026.10.0"}),
              mock.patch.object(updates, "install_status", return_value={"blockers": []}) as status,
              mock.patch.object(updater, "_error_page", return_value=0)):
            updater._recovery_error_page("manager unavailable")
        status.assert_called_once_with(offer=offer, list_number=8, settle_work=False)

    def test_recovery_page_list_number_prevents_replay_of_a_withdrawn_release(self):
        from matuto import error_page, updates
        feed, key, _archive = self._signed_feed(version="2026.10.1", number=8)
        store.write_update_state(
            self.store_path, "2026.10.0", "complete", list_number=6,
            details={"trusted_key": key.decode().strip(), "release_address": feed,
                     "update_offer": {"version": "2026.10.1"}})
        document = json.loads(FeedHandler.assets["/release-list.json"])
        document["releases"][0]["withdrawn"] = True
        list_path = self.home / "withdrawn-release-list.json"
        list_path.write_text(json.dumps(document))
        signer = self.home / "release-key-2026.10.1"
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(signer), "-n",
                        "matuto-release", str(list_path)], check=True, capture_output=True)
        FeedHandler.assets["/release-list.json"] = list_path.read_bytes()
        FeedHandler.assets["/release-list.json.sig"] = Path(f"{list_path}.sig").read_bytes()
        error_page.write("manager unavailable", "", "router-api-key", list_number=6)
        def write_page(cause, **fields):
            error_page.write(cause, "", "router-api-key", **fields)
            return 0

        with mock.patch.object(updater, "_error_page", side_effect=write_page):
            updater._recovery_error_page("manager unavailable")
        consent = error_page.read_update_consent()
        self.assertEqual(consent["listNumber"], 8)
        self.assertIsNone(consent["offer"])

        self._publish_on_feed("2026.10.1", signer, number=7)
        with (mock.patch.object(updater, "_resolved_loopback_addresses", return_value={"127.0.0.1"}),
              mock.patch.object(updater, "_listening_owners", return_value=None),
              mock.patch.object(updater, "_error_page", side_effect=write_page),
              mock.patch.object(updates, "install") as install):
            self.assertEqual(updater._update_from_menu(), 0)
        install.assert_not_called()
        consent = error_page.read_update_consent()
        self.assertEqual(consent["listNumber"], 8)
        self.assertIsNone(consent["offer"])
        self.assertIn("signed release list is stale", error_page.page_file().read_text())
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertNotIn("last_update_check", state)
        self.assertFalse((self.home / "systemd-run-args.json").exists())

    def test_update_menu_reservation_refuses_consent_replaced_by_a_withdrawal(self):
        from matuto import error_page, updates
        feed, key, _archive = self._signed_feed(version="2026.10.1", number=7)
        signer = self.home / "release-key-2026.10.1"
        store.write_update_state(
            self.store_path, "2026.10.0", "complete", list_number=6,
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        checked = updates.check(self.config, persist=False)
        error_page.write("manager unavailable", "", "router-api-key",
                         offer=checked["offer"], list_number=checked["listNumber"])

        def write_page(cause, **fields):
            error_page.write(cause, "", "router-api-key", **fields)
            return 0

        def withdraw_during_blocker_check(*_args, **_kwargs):
            self._publish_on_feed("2026.10.1", signer, 8, {"withdrawn": True})
            with mock.patch.object(updater, "_error_page", side_effect=write_page):
                updater._recovery_error_page("recovery refreshed the withdrawal")
            consent = error_page.read_update_consent()
            self.assertEqual(consent["listNumber"], 8)
            self.assertIsNone(consent["offer"])
            return {"blockers": []}

        with (mock.patch.object(updates, "install_status", side_effect=withdraw_during_blocker_check),
              mock.patch.object(updater, "_error_page", side_effect=write_page),
              mock.patch.object(updater, "_open_update_screen", return_value=0),
              mock.patch.object(updates, "install") as install):
            self.assertEqual(updater._update_from_menu(), 0)

        install.assert_not_called()
        consent = error_page.read_update_consent()
        self.assertEqual(consent["listNumber"], 8)
        self.assertIsNone(consent["offer"])
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertIsNone(state.get("app_install_pending"))
        self.assertNotIn("last_update_check", state)

    def test_update_menu_holds_page_consent_lock_through_install_reservation(self):
        from matuto import error_page, updates
        feed, key, _archive = self._signed_feed(version="2026.10.1", number=7)
        store.write_update_state(
            self.store_path, "2026.10.0", "complete", list_number=6,
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        checked = updates.check(self.config, persist=False)
        error_page.write("manager unavailable", "", "router-api-key",
                         offer=checked["offer"], list_number=checked["listNumber"])
        waiting, finished = threading.Event(), threading.Event()
        errors, writers = [], []

        def concurrent_page_rewrite():
            try:
                waiting.set()
                error_page.write("recovery withdrawal", "", "router-api-key", list_number=8)
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        def reserve(*_args, **_kwargs):
            writer = threading.Thread(target=concurrent_page_rewrite)
            writers.append(writer)
            writer.start()
            self.assertTrue(waiting.wait(5))
            self.assertFalse(finished.wait(0.05))
            return {"started": True, "version": "2026.10.1"}

        with (mock.patch.object(updates, "install_status", return_value={"blockers": []}),
              mock.patch.object(updates, "install", side_effect=reserve) as install):
            self.assertEqual(updater._update_from_menu(), 0)
        for writer in writers:
            writer.join(5)
        self.assertTrue(finished.is_set())
        install.assert_called_once_with("2026.10.1", 7, open_after=True, persist_check=False)
        self.assertEqual(errors, [])
        consent = error_page.read_update_consent()
        self.assertEqual(consent["listNumber"], 8)
        self.assertIsNone(consent["offer"])

    def test_update_menu_parent_does_not_overwrite_a_fast_install_failure_page(self):
        from matuto import error_page, updates
        offer = {"version": "2026.10.1", "notes": "Fixture", "size": 1,
                 "prerequisites": [], "changesDatabase": False, "withdrawn": False}
        store.write_update_state(self.store_path, "2026.10.0", "complete", list_number=7)
        error_page.write("manager unavailable", "", "router-api-key", offer=offer, list_number=7)

        child_started, child_done = threading.Event(), threading.Event()

        def fast_child_failure(*_args, **_kwargs):
            def record_failure():
                child_started.set()
                try:
                    error_page.write("fixture bundle digest is wrong", "", "router-api-key")
                finally:
                    child_done.set()
            threading.Thread(target=record_failure).start()
            self.assertTrue(child_started.wait(5))
            return {"started": True, "version": "2026.10.1"}

        with (mock.patch.object(updates, "check", return_value={
                  "offer": offer, "listNumber": 7, "installedVersion": "2026.10.0"}),
              mock.patch.object(updates, "install_status", return_value={"blockers": []}),
              mock.patch.object(updates, "install", side_effect=fast_child_failure),
              mock.patch.object(updater, "_error_page") as parent_write):
            self.assertEqual(updater._update_from_menu(), 0)
        self.assertTrue(child_done.wait(5))
        parent_write.assert_not_called()
        page = error_page.page_file().read_text()
        self.assertIn("fixture bundle digest is wrong", page)
        self.assertNotIn("is starting", page)

    def test_update_menu_entry_rewrites_a_changed_signed_offer_without_installing(self):
        feed, key, _archive = self._signed_feed(version="2026.10.1", number=7)
        store.write_update_state(
            self.store_path, "2026.10.0", "complete", list_number=6,
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        from matuto import error_page
        error_page.write("manager unavailable", "", "router-api-key",
                         offer={"version": "2026.10.0"}, list_number=6)
        opener = self.home / "bin" / "xdg-open"
        open_log = self.home / "menu-open.log"
        opener.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$OPEN_LOG\"\n")
        opener.chmod(0o700)
        env = {**os.environ, "OPEN_LOG": str(open_log),
               "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
               "PATH": str(opener.parent) + os.pathsep + os.environ["PATH"]}
        process = subprocess.run(
            [sys.executable, "-B", "-c",
             "from matuto import updater; raise SystemExit(updater.main(['update']))"],
            env=env, capture_output=True, text=True, timeout=20)

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(FeedHandler.requests, ["/release-list.json", "/release-list.json.sig"])
        self.assertEqual(open_log.read_text().strip(), str(self.config / "ai-agents-sandbox" /
                                                            "matuto-manager" / "error.html"))
        page = open_log.read_text().strip()
        page = Path(page).read_text()
        self.assertIn("2026.10.1", page)
        for detail in ("Release notes", "Download size", "Prerequisites", "Database migration"):
            self.assertIn(detail, page)
        consent = error_page.read_update_consent()
        self.assertEqual(consent["offer"]["version"], "2026.10.1")
        self.assertEqual(consent["listNumber"], 7)
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertNotIn("last_update_check", state)
        self.assertFalse((self.home / "systemd-run-args.json").exists())

    def test_update_menu_entry_refuses_when_the_page_named_work_that_has_since_stopped(self):
        from matuto import updates
        offer = {"version": "2026.10.1", "notes": "Fixture update", "size": 12,
                 "prerequisites": [], "changesDatabase": False, "withdrawn": False}
        old_blocker = {"text": "Run: smoke (Stop)", "command":
                       "systemctl --user stop matuto-run-fixture.service",
                       "href": "#/runs?work=fixture", "label": "Runs & jobs — Stop"}
        store.write_update_state(self.store_path, "2026.10.0", "complete", list_number=7)
        from matuto import error_page
        error_page.write("manager unavailable", "", "router-api-key", offer=offer,
                         list_number=7, blockers=[old_blocker])
        def write_page(cause, **fields):
            error_page.write(cause, "", "router-api-key", **fields)
            return 0

        with (
            mock.patch.object(updates, "check", return_value={
                "offer": offer, "listNumber": 7, "installedVersion": "2026.10.0"}),
            mock.patch.object(updates, "install_status", return_value={"blockers": []}),
            mock.patch.object(updates, "install") as install,
            mock.patch.object(updater, "_error_page", side_effect=write_page),
        ):
            self.assertEqual(updater.main(["update"]), 0)
        install.assert_not_called()
        self.assertIn("What must stop has changed", error_page.page_file().read_text())
        consent = error_page.read_update_consent()
        self.assertEqual(consent["blockers"], [])

    def test_update_menu_entry_installs_only_the_release_previously_named_on_the_page(self):
        from matuto import updates
        feed, key, _archive = self._signed_feed(version="2026.10.1", number=7)
        store.write_update_state(
            self.store_path, "2026.10.0", "complete", list_number=6,
            details={"trusted_key": key.decode().strip(), "release_address": feed})
        checked = updates.check(self.config, persist=False)
        self.assertEqual(checked["offer"]["version"], "2026.10.1")
        from matuto import error_page
        error_page.write("manager unavailable", "", "router-api-key",
                         offer=checked["offer"], list_number=checked["listNumber"])
        runner = self.home / "bin" / "systemd-run"
        install_log = self.home / "menu-install.log"
        runner.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$INSTALL_LOG\"\n")
        runner.chmod(0o700)
        opener = runner.parent / "xdg-open"
        opener.write_text("#!/bin/sh\nexit 0\n")
        opener.chmod(0o700)
        env = {**os.environ, "INSTALL_LOG": str(install_log),
               "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
               "PATH": str(runner.parent) + os.pathsep + os.environ["PATH"]}
        process = subprocess.run(
            [sys.executable, "-B", "-c",
             "from pathlib import Path; from matuto import updater; "
             "updater.independent_updater_script=lambda: Path('/fixture/updater.py'); "
             "raise SystemExit(updater.main(['update']))"],
            env=env, capture_output=True, text=True, timeout=20)

        page_path = self.config / "ai-agents-sandbox" / "matuto-manager" / "error.html"
        self.assertEqual(process.returncode, 0,
                         process.stderr + process.stdout + (page_path.read_text() if page_path.exists() else ""))
        args = install_log.read_text().splitlines()
        self.assertEqual(args[args.index("--version") + 1], "2026.10.1")
        self.assertEqual(args[args.index("--list-number") + 1], "7")
        self.assertIn("--open-after-install", args)
        state = json.loads(store.read_update_state(self.store_path)["phase"])
        self.assertEqual(state["app_install_pending"]["version"], "2026.10.1")
        self.assertEqual(state["list_number"], 7)
        self.assertNotIn("update_offer", state)
        self.assertNotIn("last_update_check", state)
        self.assertEqual(FeedHandler.requests, ["/release-list.json", "/release-list.json.sig"] * 3)

    def test_open_manager_starts_and_opens_only_after_health_and_owner_checks(self):
        opener_log = self.home / "open.log"
        opener = self.home / "bin" / "xdg-open"
        opener.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$OPEN_LOG\"\n")
        opener.chmod(0o700)
        driver = self.home / "open-action.py"
        driver.write_text(
            "import os, sys\n"
            "from matuto import updater\n"
            "updater.MANAGER_URL = sys.argv[1]\n"
            "updater._resolved_loopback_addresses = lambda: {'127.0.0.1'}\n"
            "updater._listening_owners = lambda _addresses: {updater.pwd.getpwuid(os.getuid()).pw_name}\n"
            "sys.exit(updater.main(['open']))\n")
        env = {**os.environ, "OPEN_LOG": str(opener_log),
               "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        result = subprocess.run([sys.executable, str(driver), self.manager_url], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.system_log.read_text().splitlines(), [
            "--user reset-failed matuto-manager.service", "--user start matuto-manager.service"])
        self.assertEqual(opener_log.read_text().splitlines(), [updater.MANAGER_OPEN_URL])

    def test_health_probe_refuses_an_endpoint_not_covered_by_the_owner_check(self):
        with (
            mock.patch.object(updater, "MANAGER_URL", "http://127.0.0.1:8890/"),
            mock.patch("http.client.HTTPConnection") as connection,
        ):
            with self.assertRaisesRegex(updater.UpdateError, "not covered by the owner check"):
                updater._manager_answers({"::1"})
        connection.assert_not_called()

    def test_open_manager_restarts_at_most_once_when_the_manager_does_not_answer(self):
        results = [subprocess.CompletedProcess([], 0, "") for _ in range(3)]
        calls = []
        own_user = updater.pwd.getpwuid(os.getuid()).pw_name

        def systemctl(args):
            calls.append(args)
            return results.pop(0)

        with (
            mock.patch.object(updater, "_systemctl", side_effect=systemctl),
            mock.patch.object(updater, "_resolved_loopback_addresses", return_value={"127.0.0.1"}),
            mock.patch.object(updater, "_listening_owners", return_value={own_user}),
            mock.patch.object(updater, "_manager_answers", side_effect=[False, False]),
            mock.patch.object(updater, "_error_page", return_value=0) as error_page,
        ):
            self.assertEqual(updater.open_manager(), 0)
        self.assertEqual(calls, [["reset-failed", updater.MANAGER_UNIT],
                                 ["start", updater.MANAGER_UNIT],
                                 ["restart", updater.MANAGER_UNIT]])
        self.assertIn("after one restart", error_page.call_args.args[0])

    def test_open_manager_shared_lock_prevents_an_update_from_starting_mid_recovery(self):
        exclusive_attempts = []
        own_user = updater.pwd.getpwuid(os.getuid()).pw_name

        def health(_addresses):
            with self.assertRaisesRegex(updater.UpdateError, "another Matuto update is already running"):
                updater._updater_lock(self.data)
            exclusive_attempts.append(True)
            return True

        with (
            mock.patch.object(updater, "_resolved_loopback_addresses", return_value={"::1"}),
            mock.patch.object(updater, "_listening_owners", return_value={own_user}),
            mock.patch.object(updater, "_systemctl", return_value=subprocess.CompletedProcess([], 0, "")),
            mock.patch.object(updater, "_manager_answers", side_effect=health),
            mock.patch.object(updater.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")),
        ):
            self.assertEqual(updater.open_manager(), 0)
        self.assertEqual(exclusive_attempts, [True])

    def test_open_manager_leaves_unit_alone_when_an_update_lock_is_held(self):
        store.write_update_state(self.store_path, "2026.10.1", "preparing")
        lock_fd = updater._updater_lock(self.data)
        try:
            with (
                mock.patch.object(updater, "_systemctl") as systemctl,
                mock.patch.object(updater, "_error_page", return_value=0) as error_page,
            ):
                self.assertEqual(updater.open_manager(), 0)
            systemctl.assert_not_called()
            self.assertIn("is in progress (preparing)", error_page.call_args.args[0])
        finally:
            os.close(lock_fd)
        store.write_update_state(self.store_path, "2026.10.0", "complete")

    def test_open_manager_refuses_a_foreign_or_unattributed_listener_before_contact(self):
        for owner in ("fixture-account", None):
            with self.subTest(owner=owner):
                with (
                    mock.patch.object(updater, "_resolved_loopback_addresses", return_value={"127.0.0.1"}),
                    mock.patch.object(updater, "_listening_owners", return_value={owner}),
                    mock.patch.object(updater, "_systemctl") as systemctl,
                    mock.patch.object(updater, "_manager_answers", side_effect=AssertionError("sent request")),
                    mock.patch.object(updater, "_error_page", return_value=0) as error_page,
                ):
                    self.assertEqual(updater.open_manager(), 0)
                systemctl.assert_not_called()
                self.assertIn("holds the manager address", error_page.call_args.args[0])

    def test_open_manager_leaves_manager_alone_during_update_and_reports_maintenance_error(self):
        for stage, details, expected in (
            ("preparing", {"progress": "extracting"}, "is in progress (preparing)"),
            ("maintenance", {"reason": "fixture activation failed"}, "fixture activation failed"),
        ):
            with self.subTest(stage=stage):
                store.write_update_state(self.store_path, "2026.10.1", stage, details=details)
                with (
                    mock.patch.object(updater, "_systemctl") as systemctl,
                    mock.patch.object(updater, "_error_page", return_value=0) as error_page,
                ):
                    self.assertEqual(updater.open_manager(), 0)
                systemctl.assert_not_called()
                self.assertIn(expected, error_page.call_args.args[0])
        store.write_update_state(self.store_path, "2026.10.0", "complete")

    def test_open_manager_opens_the_error_page_after_start_failure(self):
        with (
            mock.patch.object(updater, "_resolved_loopback_addresses", return_value={"127.0.0.1"}),
            mock.patch.object(updater, "_listening_owners", return_value=set()),
            mock.patch.object(updater, "_systemctl", side_effect=[
                subprocess.CompletedProcess([], 0, ""),
                subprocess.CompletedProcess([], 1, "fixture start failed")]) as systemctl,
            mock.patch.object(updater, "_error_page", return_value=0) as error_page,
        ):
            self.assertEqual(updater.open_manager(), 0)
        self.assertEqual([call.args[0] for call in systemctl.call_args_list], [
            ["reset-failed", updater.MANAGER_UNIT], ["start", updater.MANAGER_UNIT]])
        self.assertIn("fixture start failed", error_page.call_args.args[0])

    def test_open_failure_writes_redacted_journal_to_private_page_and_opens_it(self):
        opener_log = self.home / "error-page-open.log"
        opener = self.home / "bin" / "xdg-open"
        opener.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$OPEN_LOG\"\n")
        opener.chmod(0o700)
        journal = self.home / "bin" / "journalctl"
        journal.write_text("#!/bin/sh\nprintf 'api_key=router-api-key\\nmanager failed\\n'\n")
        journal.chmod(0o700)
        with (
            mock.patch.dict(os.environ, {"OPEN_LOG": str(opener_log)}),
            mock.patch.object(updater, "_resolved_loopback_addresses", return_value={"127.0.0.1"}),
            mock.patch.object(updater, "_listening_owners", return_value=set()),
            mock.patch.object(updater, "_systemctl", side_effect=[
                subprocess.CompletedProcess([], 0, ""),
                subprocess.CompletedProcess([], 1, "fixture start failed")]),
        ):
            self.assertEqual(updater.open_manager(), 0)
        page = (self.config / "ai-agents-sandbox/matuto-manager/error.html")
        text = page.read_text()
        self.assertIn("Could not start the manager: fixture start failed", text)
        self.assertIn("[redacted]", text)
        self.assertIn("manager failed", text)
        self.assertNotIn("router-api-key", text)
        self.assertEqual(page.stat().st_mode & 0o777, 0o600)
        self.assertEqual(opener_log.read_text().splitlines(), [str(page)])

    def test_error_page_withholds_details_when_the_router_key_cannot_be_read(self):
        self.api_key.unlink()
        with mock.patch.object(updater.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, "password=fixture secret"),
                subprocess.CompletedProcess([], 0, "")]):
            self.assertEqual(updater._error_page("secret-cause-481", config_root=self.config), 0)
        text = (self.config / "ai-agents-sandbox/matuto-manager/error.html").read_text()
        self.assertIn("[redacted]", text)
        self.assertNotIn("secret-cause-481", text)
        self.assertNotIn("fixture secret", text)

    def test_proc_socket_owner_check_covers_ipv4_and_ipv6_loopback(self):
        proc = self.home / "proc"
        net = proc / "net"
        net.mkdir(parents=True)
        header = "sl local_address rem_address st tx_queue:rx_queue tr:tm->when retrnsmt uid timeout inode\n"
        (net / "tcp").write_text(header +
                                 " 0: 0100007F:22BA 00000000:0000 0A 0 0 0 1001 0 1\n" +
                                 " 1: 00000000:22BA 00000000:0000 0A 0 0 0 1003 0 3\n")
        (net / "tcp6").write_text(header + " 0: 00000000000000000000000001000000:22BA 00000000000000000000000000000000:0000 0A 0 0 0 1002 0 2\n")
        for pid, inode in ((101, 1), (102, 2), (103, 3), (104, 1)):
            descriptors = proc / str(pid) / "fd"
            descriptors.mkdir(parents=True)
            (descriptors / "3").symlink_to(f"socket:[{inode}]")
        unrelated_fd_dir = proc / "105" / "fd"
        unrelated_fd_dir.mkdir(parents=True)
        unrelated_fd_dir.chmod(0)
        with mock.patch.object(updater.pwd, "getpwuid", side_effect=lambda uid: type(
                "Account", (), {"pw_name": f"uid-{uid}"})()):
            owners = updater._listening_owners({"127.0.0.1", "::1"}, proc)
        unrelated_fd_dir.chmod(0o700)
        self.assertEqual(owners, {"uid-1001", "uid-1002", "uid-1003", f"uid-{os.getuid()}"})


class DownloadTest(unittest.TestCase):
    def test_a_github_latest_address_fetches_each_release_file_from_its_own_tag(self):
        keys = tempfile.TemporaryDirectory(prefix="matuto-tag-feed-")
        self.addCleanup(keys.cleanup)
        signer = Path(keys.name) / "release-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(signer)],
                       check=True, capture_output=True)
        public_key = Path(f"{signer}.pub").read_bytes()
        files = {"1.0.0": b"first release", "1.1.0": b"second release"}

        def publish(withdrawn=()):
            entries = [{"version": version, "commit": "a" * 40, "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(), "notes": "", "prerequisites": [],
                        "changes_database": False, "withdrawn": version in withdrawn}
                       for version, data in files.items()]
            document = Path(keys.name) / "release-list.json"
            document.write_bytes(json.dumps({"number": 1, "releases": entries}).encode())
            Path(f"{document}.sig").unlink(missing_ok=True)
            subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(signer), "-n", "matuto-release",
                            str(document)], check=True, capture_output=True, stdin=subprocess.DEVNULL)
            FeedHandler.requests = []
            FeedHandler.assets = {
                "/o/r/releases/latest/download/release-list.json": document.read_bytes(),
                "/o/r/releases/latest/download/release-list.json.sig":
                    Path(f"{document}.sig").read_bytes(),
                # Only the newest release's file sits beside the list, as on GitHub.
                "/o/r/releases/latest/download/matuto-1.1.0.release": files["1.1.0"],
                **{f"/o/r/releases/download/v{version}/matuto-{version}.release": data
                   for version, data in files.items()}}

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FeedHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        feed = f"http://127.0.0.1:{server.server_port}/o/r/releases/latest/download/"

        publish()
        self.assertEqual(updater._fetch(feed, None, public_key)[2], files["1.1.0"])
        self.assertIn("/o/r/releases/download/v1.1.0/matuto-1.1.0.release", FeedHandler.requests)
        # Repair and a bootstrap rerun fetch the installed version, which is not the newest.
        self.assertEqual(updater._fetch(feed, None, public_key, version="1.0.0",
                                        allow_withdrawn=True)[2], files["1.0.0"])
        # After a list-only withdrawal, the newest installable release is an older one.
        publish(withdrawn=("1.1.0",))
        self.assertEqual(updater._fetch(feed, None, public_key)[2], files["1.0.0"])
        # A file missing at its tag fails; the copy beside the list is never used instead.
        publish()
        del FeedHandler.assets["/o/r/releases/download/v1.1.0/matuto-1.1.0.release"]
        with self.assertRaisesRegex(updater.UpdateError, "HTTP 404"):
            updater._fetch(feed, None, public_key)

    def test_release_download_cannot_redirect_away_from_the_trusted_host(self):
        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{server.server_port}/asset")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            with self.assertRaisesRegex(updater.UpdateError, "redirected away from the trusted address"):
                updater._http_bytes(f"http://127.0.0.1:{server.server_port}/asset", 1024)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_an_https_release_address_follows_a_redirect_to_another_https_host_only(self):
        # GitHub answers a release download with a redirect to a separate asset host.
        class Asset(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "5")
                self.end_headers()
                self.wfile.write(b"asset")

            def log_message(self, *_args):
                pass

        class Redirect(Asset):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"{self.server.target}://localhost:{asset.server_port}/asset")
                self.end_headers()

        class PlainTLS:
            """Both servers speak plain HTTP; only the redirect rule is under test."""

            def wrap_socket(self, sock, server_hostname=None):
                return sock

        asset = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Asset)
        redirect = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threads = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                    daemon=True) for server in (asset, redirect)]
        for thread in threads:
            thread.start()
        try:
            with mock.patch.object(updater.ssl, "create_default_context", return_value=PlainTLS()):
                redirect.target = "https"
                self.assertEqual(
                    updater._http_bytes(f"https://127.0.0.1:{redirect.server_port}/asset", 1024),
                    b"asset")
                redirect.target = "http"
                with self.assertRaisesRegex(updater.UpdateError, "redirected away"):
                    updater._http_bytes(f"https://127.0.0.1:{redirect.server_port}/asset", 1024)
        finally:
            for server in (asset, redirect):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)

    def test_dns_resolution_counts_against_the_overall_download_deadline(self):
        getaddrinfo = socket.getaddrinfo

        def slow_lookup(*args, **kwargs):
            time.sleep(.3)
            return getaddrinfo(*args, **kwargs)

        with mock.patch.object(updater, "FETCH_TIMEOUT", .05), \
                mock.patch.object(socket, "getaddrinfo", side_effect=slow_lookup):
            started = time.monotonic()
            with self.assertRaisesRegex(updater.UpdateError, "overall time limit"):
                updater._http_bytes("http://127.0.0.1:1/", 8)
            self.assertLess(time.monotonic() - started, .25)

    def test_feed_body_has_an_overall_deadline_not_a_per_read_timeout(self):
        class Slow(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "8")
                self.end_headers()
                for _ in range(8):
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(.04)
                    except OSError:
                        return

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            with mock.patch.object(updater, "FETCH_TIMEOUT", .1):
                started = time.monotonic()
                with self.assertRaisesRegex(updater.UpdateError, "overall time limit"):
                    updater._http_bytes(f"http://127.0.0.1:{server.server_port}/", 8)
                self.assertLess(time.monotonic() - started, .4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
