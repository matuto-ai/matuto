"""Install a verified Matuto release forward, independently of the manager.

Bootstrap places this program outside the versioned release tree. It stays alive
when it stops the manager, and never overwrites its own file.
"""

import contextlib
import fcntl
import hashlib
import http.client
import io
import ipaddress
import json
import os
import pwd
import re
import shutil
import secrets
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    # Bootstrap keeps this package outside the versioned release tree.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matuto import releases, serving_state, snapshot, store

MANAGER_UNIT = "matuto-manager.service"
MANAGER_HOST = "matuto.localhost:8890"
MANAGER_URL = "http://127.0.0.1:8890/"
MANAGER_OPEN_URL = "http://matuto.localhost:8890/"
MANAGER_OPEN_TIMEOUT = 5
UPDATE_STAGES = ("preparing", "prepared", "activating", "verifying")
MAX_LIST_BYTES = 1 << 20
MAX_RELEASE_BYTES = 1 << 30
MAX_EXPANDED_BYTES = 2 << 30
APP_INSTALL_UNIT_PATTERN = re.compile(r"matuto-update-[0-9a-f]{32}\.service")
FETCH_TIMEOUT = 30
HEALTH_TIMEOUT = 60


class UpdateError(Exception):
    """One refused update or a forward-recovery failure."""


class UpdaterLaunchUncertain(UpdateError):
    """The systemd-run client timed out before confirming its transient unit."""


class UpdaterBusyError(UpdateError):
    """Another updater owns the lifecycle lock."""


@dataclass(frozen=True)
class UpdateResult:
    version: str
    updater_warning: str = ""


@dataclass(frozen=True)
class RepairResult:
    version: str
    repaired_files: tuple = ()
    recovered_update: bool = False
    updater_warning: str = ""


def data_directory():
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "matuto"


def releases_directory(data_root=None):
    return Path(data_root or data_directory()) / "releases"


def active_release(data_root=None):
    return releases_directory(data_root) / "current"


def _safe_dir(path, *, create=False):
    path = Path(path)
    if create and not path.exists():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        status = path.lstat()
    except OSError:
        raise UpdateError(f"cannot read the private release directory: {path}") from None
    if (not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid()
            or status.st_mode & 0o077):
        raise UpdateError(f"the directory is not private to this account: {path}")


def installed_version(data_root=None):
    current = active_release(data_root)
    try:
        target = current.resolve(strict=True)
        root = releases_directory(data_root).resolve(strict=True)
        target.relative_to(root)
    except (OSError, ValueError):
        raise UpdateError(f"the active Matuto release is missing or outside {releases_directory(data_root)}") from None
    if not target.is_dir() or current.is_symlink() is False:
        raise UpdateError(f"the active Matuto release pointer is unsafe: {current}")
    return target.name


def _version(value):
    try:
        parts = tuple(int(part) for part in value.split("."))
    except (AttributeError, ValueError):
        raise UpdateError(f"invalid Matuto release version: {value!r}") from None
    if len(parts) != 3:
        raise UpdateError(f"invalid Matuto release version: {value!r}")
    return parts


def _record_state(connection, version, stage, **fields):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    connection.execute("BEGIN IMMEDIATE")
    try:
        previous = connection.execute(
            "SELECT phase FROM update_state WHERE state_id = 1").fetchone()
        try:
            prior_state = json.loads(previous[0]) if previous and previous[0] else {}
        except (TypeError, ValueError):
            prior_state = {}
        carried = {key: prior_state[key] for key in (
            "trusted_key", "release_address", "manager_enable_pending",
            "manager_daemon_reload_pending", "updater_swap_pending",
            "updater_swap_version", "updater_swap_manifest_sha256",
            "list_number", "last_update_check", "last_update_check_state",
            "last_update_check_error", "update_offer", "withdrawn_offer",
            "app_install_pending")
                   if isinstance(prior_state, dict) and key in prior_state}
        state = {"version": version, "stage": stage, **fields, **carried}
        if "list_number" in fields:
            state["list_number"] = fields["list_number"]
        raw = json.dumps(state, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "INSERT INTO update_state (state_id, release_version, phase, updated_at)"
            " VALUES (1, ?, ?, ?) ON CONFLICT(state_id) DO UPDATE SET"
            " release_version=excluded.release_version, phase=excluded.phase, updated_at=excluded.updated_at",
            (version, raw, now))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return state


def _list_number_path(data_root):
    return data_root / ".accepted-release-list-number"


def _read_list_number(data_root):
    path = _list_number_path(data_root)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        raise UpdateError(f"cannot read the accepted release-list number: {path}") from None
    try:
        status = os.fstat(descriptor)
        if (not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
                or status.st_uid != os.getuid() or status.st_mode & 0o077
                or status.st_size > 32):
            raise UpdateError(f"the accepted release-list number is unsafe: {path}")
        raw = os.read(descriptor, 33)
    finally:
        os.close(descriptor)
    try:
        text = raw.decode("ascii").strip()
        number = int(text)
        if number < 0 or str(number) != text:
            raise ValueError
        return number
    except (UnicodeDecodeError, ValueError):
        raise UpdateError(f"the accepted release-list number is unreadable: {path}") from None


def _record_list_number(data_root, number):
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        raise UpdateError("the signed release list has an invalid number")
    destination = _list_number_path(data_root)
    if os.path.lexists(destination):
        store._safe_file(destination, "Accepted release-list number")
    descriptor, temporary = tempfile.mkstemp(prefix=".accepted-release-", dir=data_root)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(f"{number}\n".encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        _sync_directory(data_root)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
        raise


def _highest_list_number(connection, data_root=None):
    row = connection.execute(
        "SELECT phase FROM update_state WHERE state_id = 1").fetchone()
    try:
        try:
            state = json.loads(row[0]) if row and row[0] else {}
        except (TypeError, ValueError):
            if row and row[0] == "snapshot verified":
                state = {}
            else:
                raise
        number = state.get("list_number") if isinstance(state, dict) else None
        legacy = connection.execute(
            "SELECT value FROM store_meta WHERE key = 'highest_release_list_number'").fetchone()
        numbers = [int(value) for value in (number, legacy[0] if legacy else None,
                                             _read_list_number(data_root) if data_root else None)
                   if value is not None]
        return max(numbers) if numbers else None
    except (sqlite3.Error, TypeError, ValueError):
        raise UpdateError("the store's accepted release-list number is unreadable") from None


def _resolve_addresses(host, port, deadline):
    addresses, errors = [], []

    def resolve():
        try:
            addresses.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as error:
            errors.append(error)

    # Socket timeouts do not include DNS resolution.
    resolver = threading.Thread(target=resolve, daemon=True)
    resolver.start()
    resolver.join(max(0, deadline - time.monotonic()))
    if resolver.is_alive():
        raise UpdateError("release download exceeded its overall time limit")
    if errors or not addresses:
        raise UpdateError(f"cannot resolve the release server: {errors[0] if errors else host}")
    return addresses


def _http_bytes(url, limit):
    deadline = time.monotonic() + FETCH_TIMEOUT
    initial = urllib.parse.urlsplit(url)
    current = url
    for redirects in range(6):
        parsed = urllib.parse.urlsplit(current)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment):
            raise UpdateError("the release feed address is not a safe HTTP(S) URL")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise UpdateError("the release feed must use HTTPS")
        if initial.scheme == "https" and parsed.scheme != "https":
            raise UpdateError("the release asset redirected away from HTTPS")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UpdateError("release download exceeded its overall time limit")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = _resolve_addresses(parsed.hostname, port, deadline)

        def open_socket(connection):
            last_error = None
            for family, socktype, protocol, _canonical, address in addresses:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise socket.timeout("overall download deadline expired")
                sock = socket.socket(family, socktype, protocol)
                sock.settimeout(timeout)
                try:
                    error = sock.connect_ex(address)
                    if error:
                        raise OSError(error, os.strerror(error))
                    if parsed.scheme == "https":
                        sock = connection._context.wrap_socket(sock, server_hostname=parsed.hostname)
                    connection.sock = sock
                    return
                except OSError as error:
                    sock.close()
                    last_error = error
            if last_error:
                raise last_error
            raise OSError("the release server has no usable address")

        context = ssl.create_default_context() if parsed.scheme == "https" else None
        connection_type = type("DeadlineConnection", (http.client.HTTPConnection,),
                               {"connect": open_socket})
        connection = connection_type(parsed.hostname, port, timeout=remaining)
        connection._context = context

        def interrupt():
            if connection.sock is not None:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

        timer = threading.Timer(remaining, interrupt)
        timer.daemon = True
        timer.start()
        try:
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request("GET", path, headers={"Accept": "application/octet-stream"})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or redirects == 5:
                    raise UpdateError("the release asset used too many or an invalid redirect")
                redirected = urllib.parse.urlsplit(urllib.parse.urljoin(current, location))
                origin = (initial.scheme, initial.hostname, initial.port or (443 if initial.scheme == "https" else 80))
                target = (redirected.scheme, redirected.hostname,
                          redirected.port or (443 if redirected.scheme == "https" else 80))
                # GitHub serves release files from another HTTPS host; the signed list and the
                # file's signed digest check every byte, so only a plain-HTTP feed must stay put.
                if target != origin and not (initial.scheme == redirected.scheme == "https"):
                    raise UpdateError("the release asset redirected away from the trusted address")
                current = urllib.parse.urlunsplit(redirected)
                continue
            if response.status != 200:
                raise UpdateError(f"the release server returned HTTP {response.status}")
            length = response.getheader("Content-Length")
            if length is not None and (int(length) < 0 or int(length) > limit):
                raise UpdateError(f"release download exceeds its {limit}-byte limit")
            data = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpdateError("release download exceeded its overall time limit")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65_536, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    raise UpdateError(f"release download exceeds its {limit}-byte limit")
            if time.monotonic() >= deadline:
                raise UpdateError("release download exceeded its overall time limit")
            return bytes(data)
        except (OSError, http.client.HTTPException, ValueError) as error:
            if time.monotonic() >= deadline:
                raise UpdateError("release download exceeded its overall time limit") from None
            raise UpdateError(f"cannot download the release asset: {error}") from None
        finally:
            timer.cancel()
            connection.close()
    raise UpdateError("the release asset used too many redirects")


def _fetch(feed_url, accepted_number, trusted_key, *, on_verified=None, version=None,
           allow_withdrawn=False):
    base = feed_url.rstrip("/") + "/"
    document = _http_bytes(urllib.parse.urljoin(base, "release-list.json"), MAX_LIST_BYTES)
    signature = _http_bytes(urllib.parse.urljoin(base, "release-list.json.sig"), MAX_LIST_BYTES)
    reading = releases.read_list(document, signature, trusted_key, accepted_number=accepted_number)
    if reading.stale:
        raise UpdateError("the signed release list is stale; no update was installed")
    if on_verified is not None:
        on_verified(reading)
    releases_to_consider = (reading.release_list.releases if allow_withdrawn
                            else reading.offered())
    choices = sorted(releases_to_consider, key=lambda item: _version(item.version))
    if version is not None:
        choices = [item for item in choices if item.version == version]
        if not choices:
            label = "installed" if allow_withdrawn else "reviewed"
            raise UpdateError(f"{label} release {version} is absent or withdrawn from the signed list")
    if not choices:
        return reading, None, None
    newest = choices[-1]
    name = f"matuto-{newest.version}.release"
    if base.endswith("/releases/latest/download/"):
        # GitHub's latest address serves only the newest release's files; each release
        # file stays under its own v<version> tag, which the release workflow creates.
        address = f"{base[:-len('latest/download/')]}download/v{newest.version}/{name}"
    else:
        address = urllib.parse.urljoin(base, name)
    archive = _http_bytes(address, min(MAX_RELEASE_BYTES, newest.size))
    return reading, newest, archive


def _local_bytes(path, limit, description):
    path = Path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise UpdateError(f"cannot read {description}: {path}") from None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise UpdateError(f"{description} is not an ordinary file: {path}")
        if status.st_size > limit:
            raise UpdateError(f"{description} exceeds its {limit}-byte limit: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise UpdateError(f"{description} exceeds its {limit}-byte limit: {path}")
        return data
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fetch_files(release_path, list_path, accepted_number, trusted_key, *, on_verified=None):
    document = _local_bytes(list_path, MAX_LIST_BYTES, "signed release list")
    signature = _local_bytes(f"{list_path}.sig", MAX_LIST_BYTES, "release-list signature")
    reading = releases.read_list(document, signature, trusted_key, accepted_number=accepted_number)
    if reading.stale:
        raise UpdateError("the signed release list is stale; no update was installed")
    if on_verified is not None:
        on_verified(reading)
    release_path = Path(release_path)
    archive = _local_bytes(release_path, MAX_RELEASE_BYTES, "release file")
    matches = []
    for release in reading.release_list.releases:
        if release_path.name == f"matuto-{release.version}.release":
            if len(archive) != release.size:
                raise releases.ReleaseError(
                    f"the release file is {len(archive)} bytes, but the list says {release.size}: {release_path}")
            if hashlib.sha256(archive).hexdigest() != release.sha256:
                raise releases.ReleaseError(
                    f"the release file's SHA-256 does not match the list for {release.version}: {release_path}")
            matches.append(release)
    if not matches:
        raise UpdateError("the release file is not named by the signed release list")
    if len(matches) != 1:
        raise UpdateError("the release file matches more than one signed release entry")
    release = matches[0]
    if release.withdrawn:
        raise UpdateError(f"release {release.version} is withdrawn")
    return reading, release, archive


def _check_archive(release, archive_data):
    with tempfile.TemporaryDirectory(prefix="matuto-update-archive-") as directory:
        path = Path(directory) / f"matuto-{release.version}.release"
        path.write_bytes(archive_data)
        os.chmod(path, 0o600)
        releases.check_release_file(path, release)


def missing_prerequisites(prerequisites):
    package_commands = {"openssh-client": "ssh-keygen"}
    return [name for name in prerequisites
            if shutil.which(package_commands.get(name, name)) is None]


def _prerequisites(release):
    missing = missing_prerequisites(release.prerequisites)
    if missing:
        raise UpdateError("missing release prerequisites: " + ", ".join(missing))


def app_install_unit_name():
    """A transient user service, outside the manager service's control group."""
    return f"matuto-update-{secrets.token_hex(16)}.service"


def app_install_unit_status(pending):
    """Return active, inactive, or unknown for a persisted app-install handoff."""
    unit = pending.get("unit_name") if isinstance(pending, dict) else None
    if not isinstance(unit, str) or not APP_INSTALL_UNIT_PATTERN.fullmatch(unit):
        return "unknown"
    try:
        result = _systemctl(["show", "--property=LoadState", "--property=ActiveState",
                             "--value", unit])
    except UpdateError:
        return "unknown"
    values = result.stdout.splitlines()
    if result.returncode or len(values) != 2:
        return "unknown"
    load_state, active_state = values
    if active_state in ("active", "activating", "reloading", "deactivating"):
        return "active"
    if load_state in ("loaded", "not-found") and active_state in ("inactive", "failed"):
        return "inactive"
    return "unknown"


def start_app_install(version, list_number, unit_name, *, open_after=False):
    """Start the pinned updater in a transient user service, not the manager cgroup."""
    if not isinstance(unit_name, str) or not APP_INSTALL_UNIT_PATTERN.fullmatch(unit_name):
        raise UpdateError("the independent updater unit name is invalid")
    script = independent_updater_script()
    command = ["systemd-run", "--user", "--collect", "--unit", unit_name, "--",
               sys.executable, "-B", str(script), "update", "--version", version,
               "--list-number", str(list_number), "--install-unit", unit_name]
    if open_after:
        command.append("--open-after-install")
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        raise UpdaterLaunchUncertain(
            f"systemd did not confirm whether the independent updater started as {unit_name}") from None
    except OSError as error:
        raise UpdateError(f"the independent updater could not start: {error}") from None
    if result.returncode:
        detail = result.stdout.strip() or f"systemd-run exited {result.returncode}"
        raise UpdateError(f"the independent updater could not start: {detail}")
    return unit_name


def _stop_before_update():
    try:
        stopped = _systemctl(["stop", MANAGER_UNIT])
        if stopped.returncode:
            raise UpdateError(f"cannot stop the manager before updating: {stopped.stdout.strip()}")
        inactive = _systemctl(["is-active", MANAGER_UNIT])
        if inactive.stdout.strip() not in ("inactive", "failed"):
            raise UpdateError("systemd did not confirm the manager stopped; no update was applied")
    except BaseException:
        _systemctl(["start", MANAGER_UNIT])
        raise


def _systemctl(args, *, timeout=20):
    try:
        result = subprocess.run(["systemctl", "--user", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"systemd did not answer for {MANAGER_UNIT}: {error}") from None
    return result


def _work_refusal(work):
    labels = {"run": "Stop", "build": "Stop", "download": "Cancel download"}
    blockers = ", ".join(f"{item.kind}: {item.purpose} ({labels.get(item.kind, 'Stop work')})"
                          for item in work)
    return UpdateError(f"finish or stop current work before updating: {blockers}")


def _admit(config_root):
    from matuto.runner import Runner
    opened = store.open_store(config_root, allow_newer=True)
    try:
        connection = opened.connection()
        state = serving_state.read(config_root=config_root, store_connection=connection)
        reader = Runner(config_root, opened_store=opened)
        try:
            work = [item for item in reader.works() if not item.outcome]
        finally:
            reader.close()
    finally:
        opened.close()
    if state.state != "stopped":
        raise UpdateError("serving must be stopped before Update Matuto; use Stop serving first")
    if work:
        raise _work_refusal(work)


def _admit_under_lock(opened, config_root, serving_reader, *, check_stored_work=False,
                      settle_work=True):
    connection = opened.connection()
    if serving_reader is None:
        from matuto.runner import Runner
        state = serving_state.read(config_root=config_root, store_connection=connection)
        reader = Runner(config_root, opened_store=opened)
        try:
            observed = reader.works() if settle_work else reader.live_works()
            work = [item for item in observed if not item.outcome]
        finally:
            reader.close()
    else:
        state, reported_work = serving_reader()
        work = list(reported_work)
        if check_stored_work:
            from matuto import runner
            work = runner.unfinished(connection) + work
    if state.state != "stopped":
        if serving_reader is None:
            raise UpdateError("serving must be stopped before Update Matuto; use Stop serving first")
        raise UpdateError("serving and all runs and jobs must stop before updating")
    if work:
        if serving_reader is not None and not check_stored_work:
            raise UpdateError("serving and all runs and jobs must stop before updating")
        raise _work_refusal(work)


def _space_check(config_root, data_root, release_size, expanded_size, free_bytes=None):
    store_path = store.store_path(config_root)
    release_root = releases_directory(data_root)
    try:
        store_size = store_path.stat().st_size
        locations = ((store_path.parent, 2 * store_size),
                     (release_root, release_size + expanded_size))
        requirements = {}
        for path, required in locations:
            device = path.stat().st_dev
            if device in requirements:
                requirements[device][1] += required
            else:
                free = shutil.disk_usage(path).free if free_bytes is None else free_bytes
                requirements[device] = [free, required]
    except OSError as error:
        raise UpdateError(f"cannot check free space for the update: {error}") from None
    short = [(need, free) for free, need in requirements.values() if free < need]
    if short:
        need, free = short[0]
        raise UpdateError(f"not enough free space for the release, snapshot and migration staging "
                          f"(need {need} bytes; have {free})")


def _archive_members(archive_data):
    try:
        archive = tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz")
    except (tarfile.TarError, OSError):
        raise UpdateError("the verified release is not a readable Matuto archive") from None
    with archive:
        members = archive.getmembers()
        names = [item.name for item in members]
        if (len(members) > 10_000 or len(names) != len(set(names))
                or "RELEASE.json" not in names):
            raise UpdateError("the release archive has duplicate files or no RELEASE.json manifest")
        manifest_member = next(item for item in members if item.name == "RELEASE.json")
        if not manifest_member.isfile() or manifest_member.size > MAX_LIST_BYTES:
            raise UpdateError("the release manifest is not a bounded ordinary file")
        try:
            manifest = json.load(archive.extractfile(manifest_member))
            files = manifest["files"]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise UpdateError("the release manifest is malformed") from None
        if not isinstance(files, dict) or set(names) != set(files) | {"RELEASE.json"}:
            raise UpdateError("the release archive does not match its file manifest")
        total = 0
        verified = []
        for item in members:
            name = item.name
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or not name or not item.isfile():
                raise UpdateError(f"the release contains an unsafe path or non-file: {name!r}")
            if item.size < 0 or (total := total + item.size) > MAX_EXPANDED_BYTES:
                raise UpdateError("the expanded release exceeds its size limit")
            if name == "RELEASE.json":
                verified.append((name, item.size, item.mode & 0o755 or 0o644))
                continue
            handle = archive.extractfile(item)
            digest = hashlib.sha256()
            remaining = item.size
            while handle and remaining:
                chunk = handle.read(min(1 << 20, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            if remaining or digest.hexdigest() != files.get(name):
                raise UpdateError(f"the release manifest digest does not match {name}")
            verified.append((name, item.size, item.mode & 0o755 or 0o644))
        return verified, total


def _stage_store(release_path, config_root):
    """Run the verified target release's migration registry, never the old schema code."""
    agents = release_path / "agents"
    script = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from matuto import store; "
              "print(store.stage_migrations(sys.argv[2] or None))")
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(agents), str(config_root or "")],
            cwd=release_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"the target release could not stage its database migration: {error}") from None
    if result.returncode:
        reason = result.stderr.strip().splitlines()
        raise UpdateError("the target release could not stage its database migration: " +
                          (reason[-1] if reason else "migration process failed"))
    staged = Path(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else None
    expected_parent = store.store_directory(config_root)
    if (staged is None or staged.parent != expected_parent
            or not staged.name.startswith(store.MIGRATION_TEMP_PREFIX)):
        raise UpdateError("the target release returned an unsafe staged store path")
    store._safe_file(staged, "Staged Matuto store")
    return staged


def _sync_file(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise UpdateError(f"cannot make the staged file durable at {path}: {error}") from None


def _sync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                             | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise UpdateError(f"cannot make the release change durable in {path}: {error}") from None


def _extract(data_root, version, archive_data, verified):
    root = releases_directory(data_root)
    _safe_dir(root, create=True)
    final = root / version
    if final.exists() or final.is_symlink():
        raise UpdateError(f"the release destination already exists: {final}")
    staging = root / f".staging-{version}"
    if staging.exists() or staging.is_symlink():
        shutil.rmtree(staging)
    staging.mkdir(mode=0o700)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as archive:
            members = {item.name: item for item in archive.getmembers()}
            for name, size, mode in verified:
                handle = archive.extractfile(members[name])
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("xb") as output:
                    os.fchmod(output.fileno(), mode)
                    remaining = size
                    while remaining:
                        chunk = handle.read(min(1 << 20, remaining))
                        if not chunk:
                            raise UpdateError(f"the verified release ended while extracting {name}")
                        output.write(chunk)
                        remaining -= len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
        for directory, _subdirs, _files in os.walk(staging, topdown=False):
            _sync_directory(directory)
        os.replace(staging, final)
        try:
            _sync_directory(root)
        except BaseException:
            shutil.rmtree(final, ignore_errors=True)
            raise
        return final
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _atomic_current(data_root, version):
    root = releases_directory(data_root)
    final = root / version
    try:
        final_status = final.lstat()
    except OSError:
        raise UpdateError(f"the verified release directory is missing: {final}") from None
    if not stat.S_ISDIR(final_status.st_mode) or final_status.st_uid != os.getuid():
        raise UpdateError(f"the verified release directory is unsafe: {final}")
    pointer = active_release(data_root)
    temporary = root / f".current-{os.getpid()}"
    try:
        temporary.unlink(missing_ok=True)
        os.symlink(version, temporary)
        os.replace(temporary, pointer)
        _sync_directory(root)
    except (OSError, UpdateError) as error:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"cannot activate release {version}: {error}") from None


def _updater_install(data_root):
    root = Path(data_root) / "updater"
    agents = Path(__file__).absolute().parent.parent
    if agents != root / "current" / "agents":
        return None
    return root


def independent_updater_script(data_root=None):
    """Find the protected updater entrypoint outside the active release tree."""
    root = Path(data_root) if data_root is not None else data_directory()
    root = root / "updater"
    _safe_dir(root)
    versions = root / "versions"
    _safe_dir(versions)
    current = root / "current"
    if not current.is_symlink():
        raise UpdateError(f"the installed updater pointer is unsafe: {current}")
    try:
        version_root = versions.resolve(strict=True)
        target = current.resolve(strict=True)
        if target.parent != version_root:
            raise ValueError
        _safe_dir(target)
        script = current / "agents" / "matuto" / "updater.py"
        expected = target / "agents" / "matuto" / "updater.py"
        if script.resolve(strict=True) != expected:
            raise ValueError
        for directory in (target / "agents", target / "agents" / "matuto"):
            status = directory.lstat()
            if (not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid()
                    or status.st_mode & 0o022):
                raise OSError
        status = script.lstat()
        if (not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
                or status.st_uid != os.getuid() or status.st_mode & 0o022):
            raise OSError
    except (OSError, RuntimeError, ValueError):
        raise UpdateError(f"the installed updater is missing or unsafe: {current}") from None
    return script


def _release_manifest_digest(archive_data):
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as archive:
            member = archive.getmember("RELEASE.json")
            if not member.isfile() or member.size > MAX_LIST_BYTES:
                raise UpdateError("the verified release manifest is not bounded")
            content = archive.extractfile(member).read(MAX_LIST_BYTES + 1)
            if len(content) != member.size:
                raise UpdateError("the verified release manifest was truncated")
            return hashlib.sha256(content).hexdigest()
    except (OSError, KeyError, tarfile.TarError):
        raise UpdateError("the verified release manifest cannot be read") from None


def _updater_files(release_path, expected_manifest_digest):
    """Return a verified updater tree from the authenticated release manifest."""
    manifest_path = release_path / "RELEASE.json"
    try:
        manifest_status = manifest_path.lstat()
        if (not stat.S_ISREG(manifest_status.st_mode) or manifest_status.st_nlink != 1
                or manifest_status.st_uid != os.getuid()
                or manifest_status.st_size > MAX_LIST_BYTES):
            raise OSError
        manifest_bytes = manifest_path.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_digest:
            raise UpdateError("the activated release checksum list was altered")
        manifest = json.loads(manifest_bytes)
        files = manifest["files"]
        if not isinstance(files, dict):
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError):
        raise UpdateError("the activated release has no readable per-file checksum list") from None
    updater_files = {}
    for name, digest in files.items():
        if not isinstance(name, str) or not name.startswith("agents/"):
            continue
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise UpdateError(f"the activated release has an unsafe updater path: {name!r}")
        path = release_path / relative
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(Path(release_path).resolve(strict=True))
            status = path.lstat()
            if (resolved != path or not stat.S_ISREG(status.st_mode)
                    or status.st_uid != os.getuid()):
                raise OSError
            content = path.read_bytes()
        except (OSError, ValueError):
            raise UpdateError(f"the activated release updater file is missing or unsafe: {name}") from None
        if hashlib.sha256(content).hexdigest() != digest:
            raise UpdateError(f"the activated release updater file does not match its signed checksum: {name}")
        updater_files[relative.relative_to("agents")] = (content, stat.S_IMODE(status.st_mode))
    if Path("matuto/updater.py") not in updater_files:
        raise UpdateError("the activated release has no checksummed updater")
    return updater_files


def _sync_tree(root):
    for directory, _subdirs, _files in os.walk(root, topdown=False):
        _sync_directory(directory)


def _replace_installed_updater(release_path, data_root, version, manifest_digest):
    """Atomically point the external updater at a verified release copy."""
    root = _updater_install(data_root)
    if root is None:
        return ""
    try:
        if installed_version(data_root) != version:
            raise UpdateError(f"release {version} is not the active Matuto release")
        _safe_dir(root)
        versions = root / "versions"
        _safe_dir(versions)
        current = root / "current"
        if not current.is_symlink():
            raise UpdateError(f"the installed updater pointer is unsafe: {current}")
        try:
            target = current.resolve(strict=True)
            target.relative_to(versions.resolve(strict=True))
        except (OSError, ValueError):
            raise UpdateError(f"the installed updater pointer escapes its private versions: {current}") from None
        current_version = target.name
        if _version(version) <= _version(current_version):
            return ""
        files = _updater_files(Path(release_path), manifest_digest)
        staging = versions / f".staging-{version}"
        destination = versions / version
        if destination.exists() or destination.is_symlink():
            if not destination.is_dir() or destination.is_symlink():
                raise UpdateError(f"the staged updater destination is unsafe: {destination}")
            shutil.rmtree(destination)
        if staging.exists() or staging.is_symlink():
            if staging.is_symlink() or not staging.is_dir():
                raise UpdateError(f"the staged updater is unsafe: {staging}")
            shutil.rmtree(staging)
        staging.mkdir(mode=0o700)
        agents = staging / "agents"
        agents.mkdir(mode=0o700)
        for relative, (content, mode) in files.items():
            output = agents / relative
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with output.open("xb") as outgoing:
                os.fchmod(outgoing.fileno(), mode)
                outgoing.write(content)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        _sync_tree(staging)
        os.replace(staging, destination)
        _sync_directory(versions)
        temporary = root / f".current-{os.getpid()}"
        temporary.unlink(missing_ok=True)
        os.symlink(f"versions/{version}", temporary)
        os.replace(temporary, current)
        _sync_directory(root)
        return ""
    except (OSError, UpdateError) as error:
        return str(error)


def _credential_paths(config_root):
    root = Path(config_root) if config_root is not None else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return (root / store.CONFIG_ROOT_NAME / store.MANAGER_DIR_NAME / "login.json",
            root / store.CONFIG_ROOT_NAME / "pi-local" / "api-key")


def _copy_identity(path):
    try:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_uid != os.getuid():
            raise UpdateError(f"credential is not a private regular file: {path}")
        if status.st_mode & 0o077:
            raise UpdateError(f"credential is readable by group or others: {path}")
        return hashlib.sha256(path.read_bytes()).digest()
    except FileNotFoundError:
        return None
    except OSError:
        raise UpdateError(f"cannot read credential: {path}") from None


def _manager_identity(release_path):
    result = _systemctl(["show", "--property=MainPID", "--value", MANAGER_UNIT])
    try:
        pid = int(result.stdout.strip())
        process = Path(f"/proc/{pid}")
        args = process.joinpath("cmdline").read_bytes().split(b"\0")
        command = [os.fsdecode(argument) for argument in args if argument]
        expected = (Path(release_path) / "agents" / "matuto" / "cli.py").resolve(strict=True)
        matches = False
        for argument in command:
            if not argument.startswith("/"):
                continue
            try:
                if Path(argument).resolve(strict=True) == expected:
                    matches = True
                    break
            except OSError:
                continue
        same_user = process.stat().st_uid == os.getuid()
    except (OSError, ValueError) as error:
        matches = same_user = False
        command = []
        process_error = str(error)
    else:
        process_error = ""
    if result.returncode or not same_user or not matches or "serve" not in command:
        raise UpdateError("systemd is not running the expected manager from the active release "
                          f"(MainPID={result.stdout.strip()!r}, user={same_user}, "
                          f"release={matches}, serving={('serve' in command)}, "
                          f"process error={process_error!r})")


def _manager_store_ready(release_path, config_root):
    script = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from matuto import store; "
              "opened = store.open_store(sys.argv[2] or None); opened.close()")
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script,
             str(Path(release_path) / "agents"), str(config_root or "")],
            cwd=release_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"the expected manager release could not open its database: {error}") from None
    if result.returncode:
        reason = result.stderr.strip().splitlines()
        raise UpdateError("the expected manager release could not open its database: " +
                          (reason[-1] if reason else "store opener failed"))


def _check_manager(health_check, release_path, version, config_root=None):
    _manager_identity(release_path)
    _manager_store_ready(release_path, config_root)
    if health_check is None:
        _health(expected_version=version, config_root=config_root)
    else:
        health_check()


def _has_sign_in_form(page):
    from html.parser import HTMLParser

    class SignInForm(HTMLParser):
        found = False

        def handle_starttag(self, tag, attributes):
            if tag == "form" and dict(attributes).get("action") == "/sign-in":
                self.found = True

    parser = SignInForm()
    parser.feed(page)
    return parser.found


def _health(timeout=HEALTH_TIMEOUT, url=None, host=None,
            expected_version=None, config_root=None):
    from http.client import HTTPConnection
    url = url or MANAGER_URL
    host = host or MANAGER_HOST
    if not expected_version:
        raise UpdateError("manager health verification needs the expected release version")
    address = urllib.parse.urlsplit(url)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = None
        try:
            connection = HTTPConnection(address.hostname, address.port or 80, timeout=2)
            connection.request("GET", address.path or "/", headers={"Host": host})
            response = connection.getresponse()
            page = response.read(1 << 20).decode("utf-8", "replace").lower()
            if (response.status != 200 or "<!doctype html>" not in page
                    or not _has_sign_in_form(page)):
                raise UpdateError("the expected manager did not serve the configured sign-in page")
            return
        except (OSError, http.client.HTTPException):
            pass
        finally:
            if connection is not None:
                connection.close()
        time.sleep(0.25)
    raise UpdateError("the expected manager did not serve its sign-in page")


def _preservation_state(sessions, credentials):
    return {"expected_sessions": [list(session) for session in sessions],
            "expected_credentials": [digest.hex() if digest is not None else None
                                     for digest in credentials]}


def _verify_repair_preservation(config_root, sessions, login_digest, key_digest):
    if set(store.read_sessions_at(store.store_path(config_root))) != set(sessions):
        raise UpdateError("browser sessions changed during Repair")
    login_path, key_path = _credential_paths(config_root)
    if _copy_identity(login_path) != login_digest:
        raise UpdateError("the login changed during Repair")
    current_key = _copy_identity(key_path)
    if key_digest is not None and current_key != key_digest:
        raise UpdateError("the router API key changed during Repair")
    if key_digest is None and store._router_key_reason(key_path):
        raise UpdateError("Repair did not create a usable router API key")


def _verify_preservation(config_root, state):
    sessions = state.get("expected_sessions")
    credentials = state.get("expected_credentials")
    if not isinstance(sessions, list) or not isinstance(credentials, list) or len(credentials) != 2:
        raise UpdateError("the update has no durable session and credential preservation record")
    present_sessions = store.read_sessions_at(store.store_path(config_root))
    if not set(tuple(session) for session in sessions).issubset(present_sessions):
        raise UpdateError("one or more browser sessions were lost during the update")
    current_credentials = [_copy_identity(path) for path in _credential_paths(config_root)]
    current = [digest.hex() if digest is not None else None for digest in current_credentials]
    if current != credentials:
        raise UpdateError("login or router credentials changed during the update")


def _decode_update_state(raw):
    if not raw:
        return None
    try:
        state = json.loads(raw["phase"])
    except (TypeError, ValueError):
        return {"stage": raw["phase"], "version": raw["release_version"]}
    return state if isinstance(state, dict) else {
        "stage": raw["phase"], "version": raw["release_version"]}


def _recover(config_root, data_root, *, retry_failed_repair=False):
    """Complete an activation begun by an interrupted updater before admitting writers."""
    path = store.store_path(config_root)
    manager_stopped = False
    opened = None
    try:
        if store._safe_sidecars(path):
            # Take writer exclusion before stopping a manager that may still own
            # an ordinary SQLite transaction; a live writer leaves it untouched.
            opened = store.open_store(config_root, exclusive=True, allow_newer=True)
            active = _systemctl(["is-active", MANAGER_UNIT])
            if active.stdout.strip() == "active":
                stopped = _systemctl(["stop", MANAGER_UNIT])
                if stopped.returncode:
                    raise UpdateError("cannot stop the manager before replaying its SQLite journal")
                inactive = _systemctl(["is-active", MANAGER_UNIT])
                if inactive.stdout.strip() not in ("inactive", "failed"):
                    raise UpdateError("systemd did not confirm the manager stopped for journal recovery")
                manager_stopped = True
        state = _decode_update_state(store.read_update_state(path))
        if not state or state.get("stage") not in (
                "preparing", "prepared", "activating", "maintenance", "verifying"):
            if opened is not None:
                opened.close()
                opened = None
            if manager_stopped:
                started = _systemctl(["start", MANAGER_UNIT])
                if started.returncode:
                    raise UpdateError("cannot restart the manager after SQLite journal recovery")
            return False
        if opened is None:
            opened = store.open_store(config_root, exclusive=True, allow_newer=True)
        if not manager_stopped:
            stopped = _systemctl(["stop", MANAGER_UNIT])
            if stopped.returncode:
                raise UpdateError("cannot stop the manager before recovering an interrupted update")
            inactive = _systemctl(["is-active", MANAGER_UNIT])
            if inactive.stdout.strip() not in ("inactive", "failed"):
                raise UpdateError("systemd did not confirm the manager stopped for update recovery")
    except BaseException:
        if opened is not None:
            opened.close()
        raise
    try:
        state = _decode_update_state(store.read_update_state(path))
        if not state or state.get("stage") not in (
                "preparing", "prepared", "activating", "maintenance", "verifying"):
            if opened is not None:
                opened.close()
                opened = None
            if manager_stopped:
                started = _systemctl(["start", MANAGER_UNIT])
                if started.returncode:
                    raise UpdateError("cannot restart the manager after SQLite journal recovery")
            return False
        failed_repair = (state.get("repair_result") == "failed"
                         or (state.get("repair_result") == "in_progress"
                             and state.get("repair_prior_result") == "failed"))
        if (retry_failed_repair and state.get("stage") == "maintenance"
                and failed_repair and state.get("repair_recovered_update") is False):
            if opened is not None:
                opened.close()
                opened = None
            if manager_stopped:
                started = _systemctl(["start", MANAGER_UNIT])
                if started.returncode:
                    reason = started.stdout.strip() or "the manager did not start"
                    _mark_maintenance(config_root, state["version"], reason)
                    raise UpdateError(f"cannot restart the manager after SQLite journal recovery: {reason}")
            store.write_update_state(path, state["version"], "complete")
            return False
        version = state["version"]
        if state["stage"] in ("preparing", "prepared"):
            staged = state.get("staged_store")
            if staged:
                staged_path = Path(staged)
                if (staged_path.parent != path.parent
                        or not staged_path.name.startswith(store.MIGRATION_TEMP_PREFIX)):
                    raise UpdateError("the interrupted update names an unsafe staged store")
                if staged_path.exists():
                    store._safe_file(staged_path, "Staged Matuto store")
                    staged_path.unlink()
            release_root = releases_directory(data_root)
            release_path = Path(state.get("release_path", release_root / version))
            if release_path.parent != release_root or release_path.name != version:
                raise UpdateError("the interrupted update names an unsafe release directory")
            if release_path.is_symlink():
                release_path.unlink()
            elif release_path.exists():
                shutil.rmtree(release_path)
            partial = release_root / f".staging-{version}"
            if partial.is_symlink():
                partial.unlink()
            elif partial.exists():
                shutil.rmtree(partial)
            store.discard_staged_migrations(config_root)
            store.write_update_state(path, version, "failed",
                                     details={"reason": "interrupted before activation"})
            state["_recovery"] = "old"
            return state
        active = None
        try:
            active = installed_version(data_root)
        except UpdateError:
            pass
        staged = state.get("staged_store")
        if staged:
            staged_path = Path(staged)
            if (staged_path.parent != path.parent
                    or not staged_path.name.startswith(store.MIGRATION_TEMP_PREFIX)):
                raise UpdateError("the interrupted update names an unsafe staged store")
            if staged_path.exists():
                store._safe_file(staged_path, "Staged Matuto store")
                details = {key: value for key, value in state.items()
                           if key not in ("version", "stage", "_recovery")}
                store.write_update_state(staged_path, version, "activating",
                                         list_number=state.get("list_number"), details=details)
                _sync_file(staged_path)
                os.replace(staged_path, path)
                _sync_directory(path.parent)
        if (active is not None and active != version
                and _version(active) > _version(version)):
            raise UpdateError("forward recovery will not replace a newer active release")
        expected_schema = state.get("schema_version")
        actual_schema = store.read_schema_version(path)
        if expected_schema is not None and actual_schema != expected_schema:
            if active is not None and active != version:
                release_root = releases_directory(data_root)
                release_path = Path(state.get("release_path", release_root / version))
                if release_path.parent != release_root or release_path.name != version:
                    raise UpdateError("the interrupted update names an unsafe release directory")
                if release_path.is_symlink():
                    release_path.unlink()
                elif release_path.exists():
                    shutil.rmtree(release_path)
                partial = release_root / f".staging-{version}"
                if partial.is_symlink():
                    partial.unlink()
                elif partial.exists():
                    shutil.rmtree(partial)
                store.write_update_state(path, version, "failed",
                                         details={"reason": "staged database was not activated"})
                state["_recovery"] = "old"
                return state
            raise UpdateError("the interrupted update store does not match its staged schema")
        if active != version:
            _atomic_current(data_root, version)
        store.write_update_state(path, version, "verifying")
        state["_recovery"] = "forward"
        return state
    finally:
        if opened is not None:
            opened.close()


def _updater_lock(data_root, *, shared=False):
    path = data_root / ".updater.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        status = os.fstat(descriptor)
        if (not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
                or status.st_uid != os.getuid() or status.st_mode & 0o077):
            raise UpdateError(f"the updater lock is unsafe: {path}")
        try:
            operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdaterBusyError("another Matuto update is already running") from None
        return descriptor
    except UpdateError:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    except OSError as error:
        if "descriptor" in locals():
            os.close(descriptor)
        raise UpdateError(f"cannot lock the updater process: {error}") from None


def apply(feed_url, trusted_key, *, config_root=None, data_root=None, free_bytes=None,
          health_check=None, serving_reader=None, fetcher=None, version=None,
          list_number=None):
    """Fetch, verify, stage and activate the newest eligible release forward."""
    config_root = Path(config_root) if config_root is not None else None
    data_root = Path(data_root) if data_root is not None else data_directory()
    _safe_dir(data_root, create=True)
    lock_fd = _updater_lock(data_root)
    try:
        return _apply(feed_url, trusted_key, config_root=config_root, data_root=data_root,
                      free_bytes=free_bytes, health_check=health_check,
                      serving_reader=serving_reader, fetcher=fetcher, version=version,
                      list_number=list_number)
    finally:
        os.close(lock_fd)


def apply_file(release_path, list_path, trusted_key, *, config_root=None, data_root=None,
               free_bytes=None, health_check=None, serving_reader=None):
    """Verify release, list and signature from disk, then use the ordinary install path."""
    root = Path(data_root) if data_root is not None else data_directory()
    remember = lambda reading: _record_list_number(root, reading.release_list.number)
    fetcher = lambda _feed, accepted, key: _fetch_files(
        release_path, list_path, accepted, key, on_verified=remember)
    return apply("", trusted_key, config_root=config_root, data_root=root,
                 free_bytes=free_bytes, health_check=health_check,
                 serving_reader=serving_reader, fetcher=fetcher)


def trusted_key(config_root=None):
    """Read the key recorded by bootstrap from the update-state record."""
    try:
        state = store.read_update_state(store.store_path(config_root))
    except store.StoreError as error:
        raise UpdateError(
            f"cannot read the trusted release key from the Matuto store; run bootstrap Matuto first: {error}") from None
    try:
        phase = json.loads(state["phase"]) if state and state.get("phase") else {}
    except (TypeError, ValueError):
        phase = {}
    key = phase.get("trusted_key") if isinstance(phase, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise UpdateError("no release key is recorded; bootstrap Matuto first")
    return key.encode("utf-8")


def _record_repair_started(config_root):
    path = store.store_path(config_root)
    raw = store.read_update_state(path)
    state = _decode_update_state(raw) or {}
    if state.get("repair_result") == "in_progress":
        return
    version = state.get("version") or (raw or {}).get("release_version")
    if not version:
        return
    prior_result = state.get("repair_result")
    recovered_update = (state.get("repair_recovered_update") is True
                        if prior_result == "failed" else False)
    store.write_update_state(
        path, version, state.get("stage", "complete"), list_number=state.get("list_number"),
        details={"repair_result": "in_progress", "repair_error": "",
                 "repair_prior_result": prior_result,
                 "repair_recovered_update": recovered_update})


def repair(*, config_root=None, data_root=None, fetcher=None, health_check=None):
    """Recover an interrupted update or restore this exact installed release."""
    config_root = Path(config_root) if config_root is not None else None
    data_root = Path(data_root) if data_root is not None else data_directory()
    try:
        status = data_root.lstat()
    except OSError:
        raise UpdateError(f"cannot inspect Matuto data directory: {data_root}") from None
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
        raise UpdateError(f"refusing unsafe Matuto data directory: {data_root}")
    lock_fd = _updater_lock(data_root)
    try:
        try:
            return _repair_locked(config_root, data_root, fetcher=fetcher,
                                  health_check=health_check)
        except (OSError, UpdateError, releases.ReleaseError, store.StoreError,
                snapshot.SnapshotError) as error:
            try:
                raw = store.read_update_state(store.store_path(config_root))
                state = _decode_update_state(raw) or {}
                if (state.get("repair_result") != "failed"
                        or state.get("repair_error") != str(error)[:500]):
                    version = (state.get("version") or (raw or {}).get("release_version")
                               or installed_version(data_root))
                    _record_repair_failure(
                        config_root, version, error,
                        recovered_update=state.get("repair_recovered_update", False))
            except Exception as record_error:
                print(f"matuto-repair: could not record its failed outcome: {record_error}",
                      file=sys.stderr)
            raise
    finally:
        os.close(lock_fd)


def _repair_pending_app_install(config_root, data_root, feed_url, key, fetcher, health_check):
    path = store.store_path(config_root)
    state = _decode_update_state(store.read_update_state(path))
    pending = state.get("app_install_pending") if state else None
    if not isinstance(pending, dict):
        return None
    stage = state.get("stage")
    if stage not in ("complete", "failed"):
        return None
    handoff = app_install_unit_status(pending)
    if handoff != "inactive":
        raise UpdateError(f"cannot confirm the Matuto {pending.get('version', 'unknown')} updater has stopped; retry Repair when its status is available")
    if stage == "failed" and not pending.get("relaunching"):
        store.write_update_state(
            path, installed_version(data_root), "complete",
            list_number=state.get("list_number"),
            details={"app_install_pending": None,
                     "app_install_result_unit": pending.get("unit_name"),
                     "install_result": {"version": pending.get("version", installed_version(data_root)),
                                        "stage": "failed",
                                        "reason": state.get("reason", "the updater failed before activation")}})
        return None
    version = pending.get("version")
    list_number = pending.get("list_number")
    if (not isinstance(version, str) or not releases.VERSION.fullmatch(version)
            or isinstance(list_number, bool) or not isinstance(list_number, int)
            or list_number < 0):
        raise UpdateError("the interrupted app-install reservation is invalid; run bootstrap Matuto")
    if (version == installed_version(data_root) == state.get("version")
            and list_number == state.get("list_number")):
        store.write_update_state(
            path, version, "complete", list_number=list_number,
            details={"app_install_pending": None, "app_install_result_unit": pending.get("unit_name"),
                     "install_result": {"version": version, "stage": "complete", "reason": ""}})
        return RepairResult(version, recovered_update=True)
    try:
        result = _apply(feed_url, key, config_root=config_root, data_root=data_root,
                        free_bytes=None, health_check=health_check, serving_reader=None,
                        fetcher=fetcher, version=version, list_number=list_number)
    except (OSError, UpdateError, releases.ReleaseError, store.StoreError,
            snapshot.SnapshotError) as error:
        latest = _decode_update_state(store.read_update_state(path))
        if latest and latest.get("stage") not in UPDATE_STAGES + ("maintenance",):
            store.write_update_state(
                path, installed_version(data_root), "complete",
                list_number=latest.get("list_number"),
                details={"app_install_pending": None,
                         "app_install_result_unit": pending.get("unit_name"),
                         "install_result": {"version": version, "stage": "failed",
                                            "reason": str(error)[:500]}})
        raise
    store.write_update_state(
        path, result.version, "complete", list_number=list_number,
        details={"app_install_pending": None, "app_install_result_unit": pending.get("unit_name"),
                 "install_result": {"version": result.version, "stage": "complete", "reason": ""}})
    return RepairResult(result.version, recovered_update=True,
                        updater_warning=result.updater_warning)


def _repair_locked(config_root, data_root, *, fetcher=None, health_check=None):
    recovered = _recover(config_root, data_root, retry_failed_repair=True)
    if recovered and recovered.get("_recovery") == "old":
        try:
            _finish_recovery(config_root, data_root, recovered, health_check)
        except UpdateError as error:
            _record_repair_failure(config_root, installed_version(data_root), error,
                                   recovered_update=None)
            raise
    elif recovered:
        return _finish_recovery(config_root, data_root, recovered, health_check, repair=True)
    _record_repair_started(config_root)
    warning = _finish_pending_updater_swap(config_root, data_root)
    if warning:
        error = UpdateError(f"cannot finish the interrupted updater replacement: {warning}")
        _record_repair_failure(config_root, installed_version(data_root), error)
        raise error
    version = installed_version(data_root)
    release_root = releases_directory(data_root)
    current = active_release(data_root).resolve(strict=True)
    from matuto.bootstrap import trusted_inputs
    try:
        feed_url, key = trusted_inputs(config_root)
    except Exception as error:
        raise UpdateError(f"Repair cannot verify the installed release: {error}") from None
    pending_repair = _repair_pending_app_install(
        config_root, data_root, feed_url, key, fetcher, health_check)
    if pending_repair is not None:
        return pending_repair
    store_path = store.store_path(config_root)
    store._safe_file(store_path, "Matuto store")
    try:
        connection = store._readonly_connect(store_path)
    except sqlite3.Error as error:
        raise UpdateError(f"cannot read the accepted release-list number: {error}") from None
    try:
        accepted = _highest_list_number(connection, data_root)
    finally:
        connection.close()
    update_state = _decode_update_state(store.read_update_state(store_path))
    repair_temps = _validated_repair_temps(
        update_state.get("repair_temporary_files", []) if update_state else [])
    try:
        reading, release, archive = (fetcher(feed_url, accepted, key) if fetcher else
                                     _fetch(feed_url, accepted, key, version=version,
                                            allow_withdrawn=True))
        if release is None or release.version != version:
            raise UpdateError(f"the installed release {version} is absent from the verified release list")
        _check_archive(release, archive)
        verified, _expanded = _archive_members(archive)
    except (OSError, releases.ReleaseError, UpdateError) as error:
        raise UpdateError(
            f"Repair needs the network to verify the installed release {version}: {error}") from None

    _preflight_repair(current, config_root, data_root, verified, repair_temps)
    enabled = _manager_enabled(config_root)
    sessions_before = store.read_sessions_at(store.store_path(config_root))
    login_path, key_path = _credential_paths(config_root)
    login_before = _copy_identity(login_path)
    key_before = _copy_identity(key_path)
    stopped = _systemctl(["stop", MANAGER_UNIT])
    if stopped.returncode:
        error = UpdateError(f"cannot restart the manager for Repair: {stopped.stdout.strip()}")
        _record_repair_failure(config_root, version, error)
        raise error
    try:
        _repair_private_directories(config_root, data_root, current, verified)
        repaired = _repair_release_files(current, archive, verified, config_root, version,
                                         repair_temps)
        _repair_private_directories(config_root, data_root, current, verified)
        _repair_generated_files(config_root, data_root)
        _ensure_router_key(config_root)
        _regenerate_router_presets(config_root)
        reloaded = _systemctl(["daemon-reload"])
        if reloaded.returncode:
            raise UpdateError(f"systemd could not reload the repaired manager unit: {reloaded.stdout.strip()}")
    except BaseException as error:
        with contextlib.suppress(Exception):
            store.write_update_state(store.store_path(config_root), version, "complete",
                                     details={"repair_result": "failed",
                                              "repair_error": str(error)[:500]})
        restarted = _systemctl(["start", MANAGER_UNIT])
        if restarted.returncode:
            _record_repair_failure(config_root, version, error)
            raise UpdateError(
                f"Repair failed and the manager did not restart: {restarted.stdout.strip()}") from None
        raise
    started = _systemctl(["start", MANAGER_UNIT])
    if started.returncode:
        error = UpdateError(f"Repair restored files but the manager did not start: {started.stdout.strip()}")
        _record_repair_failure(config_root, version, error)
        raise error
    enablement = _systemctl(["disable", MANAGER_UNIT]) if not enabled else None
    if enablement is not None and enablement.returncode:
        error = UpdateError(f"cannot preserve manager login start: {enablement.stdout.strip()}")
        _record_repair_failure(config_root, version, error)
        raise error
    try:
        _check_manager(health_check, current, version, config_root)
        _verify_repair_preservation(config_root, sessions_before, login_before, key_before)
    except BaseException as error:
        _systemctl(["stop", MANAGER_UNIT])
        _record_repair_failure(config_root, version, error)
        raise UpdateError(f"Repair manager check failed: {error}") from None
    state = _decode_update_state(store.read_update_state(store.store_path(config_root))) or {}
    details = {"repair_result": "complete", "repair_recovered_update": False,
               "repair_files": repaired, "repair_temporary_files": [],
               **_recovered_install_outcome(
                   state, "failed", state.get("reason", "installation did not complete"))}
    store.write_update_state(store.store_path(config_root), version, "complete", details=details)
    return RepairResult(version, repaired_files=tuple(repaired))


def _repair_walk_error(error):
    raise UpdateError(f"cannot inspect Matuto directory: {error.filename or error}") from None


def _release_directories(verified):
    directories = {"."}
    for name, _size, _mode in verified:
        parent = Path(name).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _repair_private_tree(root, allowed_directories):
    """Inspect every release directory before restoring any private modes."""
    root = Path(root)
    directories = []
    for directory, subdirs, _files in os.walk(root, topdown=True, followlinks=False,
                                               onerror=_repair_walk_error):
        path = Path(directory)
        try:
            status = path.lstat()
        except OSError:
            raise UpdateError(f"cannot inspect Matuto directory: {path}") from None
        relative = path.relative_to(root).as_posix()
        if relative not in allowed_directories:
            raise UpdateError(f"refusing to change an unowned installed directory: {path}")
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
            raise UpdateError(f"refusing unsafe Matuto directory: {path}")
        for name in list(subdirs):
            child = path / name
            info = child.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise UpdateError(f"refusing unsafe Matuto directory: {child}")
            if child.relative_to(root).as_posix() not in allowed_directories:
                raise UpdateError(f"refusing to change an unowned installed directory: {child}")
        directories.append((path, status))
    return directories


def _repair_private_directory_statuses(config_root, data_root):
    config_root = Path(config_root) if config_root is not None else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    private = (Path(data_root), releases_directory(data_root),
               store.store_directory(config_root),
               config_root / "ai-agents-sandbox/pi-local")
    directories = []
    for directory in private:
        try:
            status = directory.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise UpdateError(f"cannot inspect Matuto directory: {directory}") from None
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
            raise UpdateError(f"refusing unsafe Matuto directory: {directory}")
        directories.append((directory, status))
    return directories


def _validate_repair_release_paths(data_root, root):
    data_root = Path(data_root)
    releases = releases_directory(data_root)
    current = active_release(data_root)
    for directory in (data_root, releases):
        try:
            status = directory.lstat()
        except OSError:
            raise UpdateError(f"cannot inspect Matuto directory: {directory}") from None
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
            raise UpdateError(f"refusing unsafe Matuto directory: {directory}")
    try:
        pointer = current.lstat()
        target = current.resolve(strict=True)
        release_parent = releases.resolve(strict=True)
        target.relative_to(release_parent)
    except (OSError, ValueError):
        raise UpdateError(f"the active Matuto release pointer is unsafe: {current}") from None
    if (not stat.S_ISLNK(pointer.st_mode) or pointer.st_uid != os.getuid()
            or target != Path(root)):
        raise UpdateError(f"the active Matuto release pointer is unsafe: {current}")


def _validated_repair_temps(values):
    if not isinstance(values, list):
        raise UpdateError("the recorded Repair temporary-file list is invalid")
    for relative in values:
        if not isinstance(relative, str):
            raise UpdateError("the recorded Repair temporary-file list is invalid")
        path = Path(relative)
        name = path.name
        token = name[len(".matuto-repair-"):] if name.startswith(".matuto-repair-") else ""
        if (not relative or path.is_absolute() or path.as_posix() != relative
                or ".." in path.parts or len(token) != 32
                or any(character not in "0123456789abcdef" for character in token)):
            raise UpdateError(f"the recorded Repair temporary path is invalid: {relative}")
    return tuple(values)


def _preflight_repair(root, config_root, data_root, verified, repair_temps):
    root = Path(root)
    config_root = Path(config_root) if config_root is not None else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    data_root = Path(data_root)
    _validate_repair_release_paths(data_root, root)
    _repair_private_directory_statuses(config_root, data_root)
    expected = {name for name, _size, _mode in verified}
    allowed_directories = _release_directories(verified)
    repair_temps = set(repair_temps)
    for directory, subdirs, files in os.walk(root, topdown=True, followlinks=False,
                                              onerror=_repair_walk_error):
        parent = Path(directory)
        status = parent.lstat()
        relative_directory = parent.relative_to(root).as_posix()
        if relative_directory not in allowed_directories:
            raise UpdateError(f"refusing to change an unowned installed directory: {parent}")
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
            raise UpdateError(f"refusing unsafe Matuto directory: {parent}")
        for name in list(subdirs):
            child = parent / name
            info = child.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise UpdateError(f"refusing unsafe installed path: {child}")
            if child.relative_to(root).as_posix() not in allowed_directories:
                raise UpdateError(f"refusing to change an unowned installed directory: {child}")
        for name in files:
            path = parent / name
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1):
                raise UpdateError(f"refusing unsafe installed path: {path}")
            relative = path.relative_to(root).as_posix()
            if relative not in expected and relative not in repair_temps:
                raise UpdateError(f"refusing to remove an unowned installed file: {path}")
    user_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    outputs = [(config_root, config_root / "systemd/user/matuto-manager.service")]
    outputs.extend((user_data, user_data / "applications" / name) for name in (
        "matuto.desktop", "repair-matuto.desktop", "update-matuto.desktop"))
    outputs.extend(((config_root, store.store_path(config_root).parent / "router-presets.ini"),
                    (config_root, config_root / "ai-agents-sandbox/pi-local/api-key")))
    unit = config_root / "systemd/user/matuto-manager.service"
    key_path = config_root / "ai-agents-sandbox/pi-local/api-key"
    if os.path.lexists(key_path):
        try:
            store._safe_file(key_path, "Router API key")
            if key_path.lstat().st_nlink != 1:
                raise UpdateError(f"refusing unsafe repair path: {key_path}")
        except store.StoreError as error:
            raise UpdateError(str(error)) from None
    preset_path = store.store_path(config_root).parent / "router-presets.ini"
    if os.path.lexists(preset_path):
        store._router_preset_snapshot(preset_path)
    from matuto.bootstrap import _desktop_entry
    updater_script = Path(data_root) / "updater/current/agents/matuto/updater.py"
    entries = (("matuto.desktop", "Matuto", "open"),
               ("repair-matuto.desktop", "Repair Matuto", "repair"),
               ("update-matuto.desktop", "Update Matuto", "update"))
    expected_entries = {
        user_data / "applications" / filename: _desktop_entry(name, updater_script, action)
        for filename, name, action in entries}
    for base, path in outputs:
        base = Path(base)
        current = base
        status = current.lstat()
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
            raise UpdateError(f"refusing unsafe generated-file directory: {current}")
        for part in path.parent.relative_to(base).parts:
            current /= part
            if not os.path.lexists(current):
                continue
            status = current.lstat()
            if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
                raise UpdateError(f"refusing unsafe generated-file directory: {current}")
        if os.path.lexists(path):
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1):
                raise UpdateError(f"refusing unsafe repair path: {path}")
            if path in expected_entries:
                try:
                    current = path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeError):
                    raise UpdateError(f"cannot identify a menu entry before Repair: {path}") from None
                identity = [line for line in expected_entries[path].splitlines()
                            if line.startswith(("Type=", "Name=", "Exec="))]
                present = [line for line in current if line.startswith(("Type=", "Name=", "Exec="))]
                if present != identity:
                    raise UpdateError(f"refusing to overwrite a menu entry Repair did not write: {path}")
    if os.path.lexists(unit):
        try:
            unit_text = unit.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise UpdateError(f"cannot identify the manager unit before Repair: {unit}") from None
        from matuto.bootstrap import _manager_unit
        expected_start = next(line for line in _manager_unit(data_root).splitlines()
                              if line.startswith("ExecStart="))
        service_commands = []
        section = ""
        for line in unit_text.splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line
            elif section == "[Service]" and line.startswith("Exec"):
                service_commands.append(line)
        if service_commands != [expected_start]:
            raise UpdateError(f"refusing to overwrite a manager unit Repair did not write: {unit}")


def _repair_release_files(root, archive_data, verified, config_root, version, repair_temps):
    """Replace verified release files and clean only temporaries recorded by Repair."""
    root = Path(root)
    expected = {name: (size, mode) for name, size, mode in verified}
    repair_temps = set(repair_temps)
    present = set()
    for directory, subdirs, files in os.walk(root, topdown=True, followlinks=False,
                                              onerror=_repair_walk_error):
        parent = Path(directory)
        for name in list(subdirs):
            path = parent / name
            status = path.lstat()
            if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
                raise UpdateError(f"refusing unsafe installed path: {path}")
        for name in files:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            status = path.lstat()
            if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                    or status.st_nlink != 1):
                raise UpdateError(f"refusing unsafe installed path: {path}")
            if relative not in expected and relative not in repair_temps:
                raise UpdateError(f"refusing to remove an unowned installed file: {path}")
            if relative in expected:
                present.add(relative)
    for relative in repair_temps:
        (root / relative).unlink(missing_ok=True)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz")
    except (OSError, tarfile.TarError):
        raise UpdateError("the verified release archive cannot be reopened for repair") from None
    with archive:
        members = {item.name: item for item in archive.getmembers()}
        changed = []
        for name, (size, mode) in expected.items():
            destination = root / name
            matches = False
            if name in present:
                try:
                    status = destination.lstat()
                    matches = status.st_size == size and stat.S_IMODE(status.st_mode) == mode
                    if matches:
                        digest = hashlib.sha256()
                        with destination.open("rb") as existing:
                            for chunk in iter(lambda: existing.read(1 << 20), b""):
                                digest.update(chunk)
                        source = archive.extractfile(members[name])
                        expected_digest = hashlib.sha256()
                        while chunk := source.read(1 << 20):
                            expected_digest.update(chunk)
                        matches = digest.digest() == expected_digest.digest()
                except OSError:
                    matches = False
            if not matches:
                changed.append(name)
        temporary_paths = {}
        for name in changed:
            destination = root / name
            while True:
                token = secrets.token_hex(16)
                temporary = destination.with_name(f".matuto-repair-{token}")
                if not os.path.lexists(temporary):
                    temporary_paths[name] = temporary
                    break
        if changed or repair_temps:
            store.write_update_state(
                store.store_path(config_root), version, "complete",
                details={"repair_result": "in_progress",
                         "repair_temporary_files": [
                             path.relative_to(root).as_posix()
                             for path in temporary_paths.values()]})
        for name in changed:
            destination = root / name
            temporary = temporary_paths[name]
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                                 | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    os.fchmod(output.fileno(), expected[name][1])
                    source = archive.extractfile(members[name])
                    while chunk := source.read(1 << 20):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if os.path.lexists(destination):
                    status = destination.lstat()
                    if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                            or status.st_nlink != 1):
                        raise UpdateError(f"refusing unsafe installed path: {destination}")
                os.replace(temporary, destination)
                _sync_directory(destination.parent)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        if changed or repair_temps:
            store.write_update_state(
                store.store_path(config_root), version, "complete",
                details={"repair_temporary_files": []})
    return tuple(changed)


def _repair_generated_files(config_root, data_root):
    from matuto import bootstrap
    config_root = Path(config_root) if config_root is not None else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    user_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    updater_script = Path(data_root) / "updater/current/agents/matuto/updater.py"
    outputs = [(config_root / "systemd/user/matuto-manager.service",
                bootstrap._manager_unit(data_root))]
    entries = (("matuto.desktop", "Matuto", "open"),
               ("repair-matuto.desktop", "Repair Matuto", "repair"),
               ("update-matuto.desktop", "Update Matuto", "update"))
    outputs.extend((user_data / "applications" / filename,
                    bootstrap._desktop_entry(name, updater_script, action))
                   for filename, name, action in entries)
    for path, content in outputs:
        _write_repaired_file(path, content)


def _write_repaired_file(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
        raise UpdateError(f"refusing unsafe generated-file directory: {path.parent}")
    if os.path.lexists(path):
        status = path.lstat()
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                or status.st_nlink != 1):
            raise UpdateError(f"refusing to replace unsafe generated file: {path}")
        if path.read_text(encoding="utf-8") == content:
            return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-repair-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _ensure_router_key(config_root):
    from matuto.bootstrap import _ensure_router_key as ensure_key
    try:
        ensure_key(config_root)
    except Exception as error:
        raise UpdateError(f"cannot restore the router API key: {error}") from None


def _regenerate_router_presets(config_root):
    try:
        opened = store.open_store(config_root)
        try:
            store.regenerate_router_files(opened)
        finally:
            opened.close()
    except (OSError, store.StoreError) as error:
        raise UpdateError(f"cannot regenerate router presets: {error}") from None


def _manager_enabled(config_root):
    config_root = Path(config_root) if config_root is not None else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    unit = config_root / "systemd/user/matuto-manager.service"
    if os.path.lexists(unit):
        try:
            status = unit.lstat()
            if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid() or status.st_nlink != 1:
                raise UpdateError(f"refusing unsafe manager unit: {unit}")
        except OSError:
            raise UpdateError(f"cannot inspect manager login-start choice: {unit}") from None
    else:
        link = unit.parent / "default.target.wants/matuto-manager.service"
        return os.path.lexists(link)
    result = _systemctl(["is-enabled", MANAGER_UNIT])
    state = result.stdout.strip()
    if state == "disabled":
        return False
    if state == "masked":
        raise UpdateError("the manager service is masked; Repair will not unmask it")
    if state not in ("enabled", "enabled-runtime", "linked", "linked-runtime"):
        raise UpdateError(f"cannot determine the manager login-start choice: {state or 'unknown'}")
    if result.returncode:
        raise UpdateError(f"cannot determine the manager login-start choice: {state}")
    return True


def _repair_private_directories(config_root, data_root, release_root, verified):
    _validate_repair_release_paths(data_root, release_root)
    private = _repair_private_directory_statuses(config_root, data_root)
    release = _repair_private_tree(release_root, _release_directories(verified))
    for directory, status in private + release:
        if stat.S_IMODE(status.st_mode) != 0o700:
            os.chmod(directory, 0o700, follow_symlinks=False)


def _finish_pending_updater_swap(config_root, data_root):
    update_state = store.read_update_state(store.store_path(config_root))
    state = _decode_update_state(update_state)
    if state and state.get("updater_swap_pending"):
        version = state.get("updater_swap_version", state["version"])
        if version == installed_version(data_root):
            warning = _replace_installed_updater(
                releases_directory(data_root) / version, data_root, version,
                state.get("updater_swap_manifest_sha256", state.get("updater_manifest_sha256", "")))
            if not warning:
                store.write_update_state(store.store_path(config_root), version, "complete",
                                         details={"updater_swap_pending": False})
            return warning
    return ""


def _recovered_install_outcome(state, stage, reason="", *, installed=None):
    pending = state.get("app_install_pending")
    if not isinstance(pending, dict):
        return {}
    version = pending.get("version", state.get("version", "unknown"))
    if (stage == "complete"
            and (version != state.get("version") or version != installed
                 or pending.get("list_number") != state.get("list_number"))):
        stage = "failed"
        reason = "installed release did not match the reviewed offer"
    return {"app_install_pending": None,
            "app_install_result_unit": pending.get("unit_name"),
            "install_result": {"version": version, "stage": stage, "reason": reason[:500]}}


def _finish_recovery(config_root, data_root, recovered, health_check, *, repair=False):
    version = installed_version(data_root)
    started = _systemctl(["start", MANAGER_UNIT])
    if started.returncode:
        reason = started.stdout.strip() or "the manager did not start"
        error = UpdateError(f"forward recovery activated the release but {reason}")
        if repair:
            _record_repair_failure(config_root, version, error, recovered_update=True)
        else:
            _mark_maintenance(config_root, version, str(error))
        raise error
    try:
        _check_manager(health_check, releases_directory(data_root) / version, version, config_root)
        if recovered.get("_recovery") == "forward":
            _verify_preservation(config_root, recovered)
    except BaseException as error:
        _systemctl(["stop", MANAGER_UNIT])
        if repair:
            _record_repair_failure(config_root, version, error, recovered_update=True)
        else:
            _mark_maintenance(config_root, version, str(error))
        raise UpdateError(f"forward recovery manager check failed: {error}") from None
    if recovered.get("_recovery") == "forward":
        installed_updater = _updater_install(data_root)
        manifest_digest = recovered.get("updater_manifest_sha256", "")
        details = {"updater_swap_pending": installed_updater is not None,
                   "updater_swap_version": version,
                   "updater_swap_manifest_sha256": manifest_digest,
                   **_recovered_install_outcome(recovered, "complete", installed=version)}
        if repair:
            details.update(repair_result="complete", repair_recovered_update=True,
                           repair_files=[])
        store.write_update_state(store.store_path(config_root), version, "complete", details=details)
        warning = ""
        if installed_updater is not None:
            warning = _replace_installed_updater(
                releases_directory(data_root) / version, data_root, version, manifest_digest)
            if not warning:
                details["updater_swap_pending"] = False
                store.write_update_state(store.store_path(config_root), version, "complete",
                                         details=details)
        if repair:
            return RepairResult(version, recovered_update=True, updater_warning=warning)
        return UpdateResult(version, warning)
    if repair:
        details = {"repair_result": "complete", "repair_recovered_update": True,
                   "repair_files": [],
                   **_recovered_install_outcome(
                       recovered, "failed", recovered.get("reason", "interrupted before activation"))}
        store.write_update_state(store.store_path(config_root), version, "complete", details=details)
        return RepairResult(version, recovered_update=True)
    return None


def _apply(feed_url, trusted_key, *, config_root, data_root, free_bytes,
           health_check, serving_reader, fetcher, version=None, list_number=None):
    """Run one serialized update attempt."""
    _safe_dir(data_root, create=True)
    _safe_dir(releases_directory(data_root), create=True)
    recovered = _recover(config_root, data_root)
    if recovered:
        result = _finish_recovery(config_root, data_root, recovered, health_check)
        if result is not None:
            return result
    _finish_pending_updater_swap(config_root, data_root)
    opened = store.open_store(config_root, allow_update_read=version is not None,
                              allow_newer=True)
    try:
        accepted_number = _highest_list_number(opened.connection(), data_root)
        before_version = installed_version(data_root)
        remember = lambda verified: _record_list_number(
            data_root, verified.release_list.number)
        if fetcher:
            reading, release, archive_data = fetcher(feed_url, accepted_number, trusted_key)
            remember(reading)
        else:
            reading, release, archive_data = _fetch(
                feed_url, accepted_number, trusted_key, on_verified=remember, version=version)
        if list_number is not None and reading.release_list.number != list_number:
            raise UpdateError("the signed release list changed after review; review the offer again")
        if release is None:
            if version is not None:
                raise UpdateError(f"the reviewed release {version} is no longer offered")
            raise UpdateError("the signed release list has no installable release")
        if version is not None and release.version != version:
            raise UpdateError(f"the reviewed release {version} is no longer offered")
        if release.withdrawn:
            raise UpdateError(f"release {release.version} is withdrawn")
        _check_archive(release, archive_data)
        if _version(release.version) <= _version(before_version):
            raise UpdateError(f"release {release.version} is not newer than installed {before_version}")
        verified_files, expanded_size = _archive_members(archive_data)
        _prerequisites(release)
        _space_check(config_root, data_root, release.size, expanded_size, free_bytes)
        _admit_under_lock(opened, config_root, serving_reader, settle_work=version is None)
    finally:
        opened.close()

    _stop_before_update()

    try:
        opened = store.open_store(config_root, exclusive=True, allow_newer=True)
    except BaseException:
        _systemctl(["start", MANAGER_UNIT])
        raise
    activation_started = False
    attempt_recorded = False
    release_path = None
    staged_store = None
    try:
        _admit_under_lock(opened, config_root, serving_reader, check_stored_work=True)
        # Recheck from the lock-protected store after any already-open caller exits.
        current_number = _highest_list_number(opened.connection(), data_root)
        expected_number = max(number for number in
                              (accepted_number, reading.release_list.number) if number is not None)
        if current_number != expected_number:
            raise UpdateError("the accepted release list changed while Update Matuto was starting")
        _record_list_number(data_root, reading.release_list.number)
        _record_state(opened.connection(), release.version, "preparing",
                      list_number=reading.release_list.number)
        attempt_recorded = True
        saved = snapshot.take(opened, release.version)
        _record_state(opened.connection(), release.version, "preparing",
                      snapshot=str(saved.path), list_number=reading.release_list.number)
        release_path = _extract(data_root, release.version, archive_data, verified_files)
        staged_store = _stage_store(release_path, config_root)
        _record_state(opened.connection(), release.version, "prepared",
                      staged_store=str(staged_store), release_path=str(release_path),
                      list_number=reading.release_list.number)
        sessions = store.read_sessions(opened.connection())
        login_path, key_path = _credential_paths(config_root)
        credentials = (_copy_identity(login_path), _copy_identity(key_path))

        schema_version = store.read_schema_version(staged_store)
        manifest_digest = _release_manifest_digest(archive_data)
        details = {"staged_store": str(staged_store), "release_path": str(release_path),
                   "schema_version": schema_version,
                   "updater_manifest_sha256": manifest_digest,
                   **_preservation_state(sessions, credentials)}
        activation_state = _record_state(
            opened.connection(), release.version, "activating",
            list_number=reading.release_list.number, **details)
        activation_started = True
        # The staged copy must already carry this ledger if power fails at rename.
        staged_details = {key: value for key, value in activation_state.items()
                          if key not in ("version", "stage")}
        store.write_update_state(staged_store, release.version, "activating",
                                 list_number=reading.release_list.number, details=staged_details)
        _sync_file(staged_store)
        _sync_directory(staged_store.parent)
        os.replace(staged_store, opened.path)
        _sync_directory(opened.path.parent)
        store.write_update_state(opened.path, release.version, "activating",
                                 list_number=reading.release_list.number,
                                 details={"schema_version": schema_version,
                                          "database_activated": True})
        opened.close()
        _atomic_current(data_root, release.version)
        store.write_update_state(opened.path, release.version, "verifying",
                                 list_number=reading.release_list.number,
                                 details={"schema_version": schema_version,
                                          "database_activated": True,
                                          "release_activated": True})
    except BaseException as error:
        if opened._connection is not None:
            opened.close()
        if not activation_started:
            if attempt_recorded:
                with contextlib.suppress(Exception):
                    store.write_update_state(store.store_path(config_root), release.version, "failed",
                                             details={"reason": str(error)[:500]})
            if staged_store is not None:
                staged_store.unlink(missing_ok=True)
            if release_path is not None and release_path.is_dir() and not release_path.is_symlink():
                shutil.rmtree(release_path, ignore_errors=True)
            _systemctl(["start", MANAGER_UNIT])
        if isinstance(error, UpdateError):
            raise
        raise UpdateError(f"update {release.version} stopped before it completed: {error}") from None

    # Release the exclusive store lock before the new manager opens the migrated DB.
    if opened._connection is not None:
        opened.close()
    started = _systemctl(["start", MANAGER_UNIT])
    if started.returncode:
        reason = started.stdout.strip() or "the manager did not start"
        _mark_maintenance(config_root, release.version, reason)
        raise UpdateError(f"the new release is active but the manager did not start: {reason}")
    try:
        _check_manager(health_check, release_path, release.version, config_root)
        _verify_preservation(config_root, activation_state)
    except BaseException as error:
        _systemctl(["stop", MANAGER_UNIT])
        _mark_maintenance(config_root, release.version, str(error))
        raise UpdateError(f"release {release.version} remains in maintenance after health verification failed: {error}") from None

    installed_updater = _updater_install(data_root)
    store.write_update_state(
        store.store_path(config_root), release.version, "complete",
        list_number=reading.release_list.number,
        details={"updater_swap_pending": installed_updater is not None,
                 "updater_swap_version": release.version,
                 "updater_swap_manifest_sha256": manifest_digest,
                 **_recovered_install_outcome(activation_state, "complete",
                                              installed=release.version)})
    updater_warning = ""
    if installed_updater is not None:
        updater_warning = _replace_installed_updater(
            release_path, data_root, release.version, manifest_digest)
        if not updater_warning:
            store.write_update_state(store.store_path(config_root), release.version, "complete",
                                     details={"updater_swap_pending": False})
    return UpdateResult(release.version, updater_warning)


def _record_repair_failure(config_root, version, error, *, recovered_update=False):
    details = {"repair_result": "failed", "repair_error": str(error)[:500]}
    if recovered_update is not None:
        details["repair_recovered_update"] = recovered_update
    store.write_update_state(store.store_path(config_root), version, "complete",
                             details=details)
    _mark_maintenance(config_root, version, str(error))


def _mark_maintenance(config_root, version, reason=""):
    try:
        store.write_update_state(store.store_path(config_root), version, "maintenance",
                                 details={"reason": str(reason)[:500]})
    except (OSError, store.StoreError, sqlite3.Error) as error:
        raise UpdateError(f"cannot durably record maintenance for release {version}: {error}") from None


def _resolved_loopback_addresses():
    try:
        records = socket.getaddrinfo("matuto.localhost", 8890, type=socket.SOCK_STREAM)
    except OSError as error:
        raise UpdateError(f"cannot resolve matuto.localhost for the address-owner check: {error}") from None
    addresses = {str(ipaddress.ip_address(record[4][0])) for record in records
                 if ipaddress.ip_address(record[4][0]).is_loopback}
    if not addresses:
        raise UpdateError("matuto.localhost has no loopback address to verify")
    return addresses


def _proc_address(raw, family):
    try:
        encoded = bytes.fromhex(raw)
        if family == socket.AF_INET:
            return str(ipaddress.ip_address(encoded[::-1]))
        return str(ipaddress.ip_address(b"".join(encoded[index:index + 4][::-1]
                                                   for index in range(0, 16, 4))))
    except (ValueError, IndexError):
        return None


def _process_socket_owners(inodes, proc_root):
    """Collect visible descriptor holders; /proc/net's socket UID remains authoritative."""
    holders = {inode: set() for inode in inodes}
    try:
        processes = list(Path(proc_root).iterdir())
    except OSError:
        return None
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            uid = process.stat().st_uid
            for descriptor in (process / "fd").iterdir():
                try:
                    target = os.readlink(descriptor)
                except FileNotFoundError:
                    continue
                if target.startswith("socket:[") and target.endswith("]"):
                    inode = target[8:-1]
                    if inode in holders:
                        holders[inode].add(uid)
        except OSError:
            # The kernel socket UID remains authoritative when unrelated proc entries are private.
            continue
    return holders


def _listening_owners(addresses, proc_root=Path("/proc")):
    """Return account names for every matching listener, or None if ownership is unknown."""
    socket_owners = {}
    for table, family in (("net/tcp", socket.AF_INET), ("net/tcp6", socket.AF_INET6)):
        try:
            rows = (Path(proc_root) / table).read_text().splitlines()[1:]
        except OSError:
            return None
        for row in rows:
            fields = row.split()
            if len(fields) < 8:
                return None
            if fields[3] != "0A":
                continue
            try:
                local_address, raw_port = fields[1].rsplit(":", 1)
                port, uid = int(raw_port, 16), int(fields[7])
                inode = fields[9]
            except (IndexError, ValueError):
                return None
            address = _proc_address(local_address, family)
            if port != 8890 or (address not in addresses and address not in ("0.0.0.0", "::")):
                continue
            socket_owners.setdefault(inode, set()).add(uid)
    if not socket_owners:
        return set()
    process_owners = _process_socket_owners(socket_owners, proc_root)
    if process_owners is None:
        return None
    owners = set()
    for inode, uids in socket_owners.items():
        uids.update(process_owners[inode])
        for uid in uids:
            try:
                owners.add(pwd.getpwuid(uid).pw_name)
            except KeyError:
                owners.add(None)
    return owners


def _owner_refusal(owners):
    if owners is None:
        return "Cannot verify who holds the manager address; refusing to open it."
    if None in owners:
        return "Another account's process with an unidentifiable owner holds the manager address."
    foreign = sorted(owner for owner in owners if owner != pwd.getpwuid(os.getuid()).pw_name)
    if foreign:
        return f"Another account's process ({foreign[0]}) holds the manager address."
    return None


def _manager_health_address():
    parsed = urllib.parse.urlsplit(MANAGER_URL)
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        raise UpdateError("the manager health-check address is not a literal loopback IP") from None
    if not address.is_loopback:
        raise UpdateError("the manager health-check address is not loopback")
    return str(address)


def _manager_answers(addresses):
    from http.client import HTTPConnection, HTTPException
    health_address = _manager_health_address()
    if health_address not in addresses:
        raise UpdateError("the manager health-check address is not covered by the owner check")
    deadline = time.monotonic() + MANAGER_OPEN_TIMEOUT
    while time.monotonic() < deadline:
        owners = _listening_owners(addresses)
        refusal = _owner_refusal(owners)
        if refusal:
            raise UpdateError(refusal)
        if not owners:
            time.sleep(0.1)
            continue
        connection = None
        try:
            address = urllib.parse.urlsplit(MANAGER_URL)
            connection = HTTPConnection(address.hostname, address.port or 80, timeout=1)
            connection.request("GET", address.path or "/", headers={"Host": MANAGER_HOST})
            response = connection.getresponse()
            page = response.read(1 << 20).decode("utf-8", "replace").lower()
            if (response.status == 200 and "<!doctype html>" in page
                    and ("sign in" in page or "create login" in page)):
                return True
        except (OSError, HTTPException):
            pass
        finally:
            if connection is not None:
                connection.close()
        time.sleep(0.1)
    return False


def _error_page(cause, *, config_root=None, offer=None, blockers=(), list_number=None):
    from matuto import error_page
    try:
        log = subprocess.run(["journalctl", "--user", "-u", MANAGER_UNIT,
                              "--no-pager", "-n", "100", "-o", "cat"],
                             capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        log = ""
    login_path, key_path = _credential_paths(config_root)
    del login_path
    try:
        store._safe_file(key_path, "Router API key")
        key = key_path.read_text().strip()
    except (OSError, store.StoreError):
        key, log = "", ""
        cause = "Manager details are withheld because the router API key could not be read safely."
    try:
        path = error_page.write(cause, log, key, offer=offer, blockers=blockers,
                                list_number=list_number)
    except error_page.ErrorPageError as error:
        print(f"matuto-manager open: {error}", file=sys.stderr)
        return 1
    try:
        return subprocess.run(["xdg-open", str(path)], check=False).returncode
    except OSError as error:
        print(f"matuto-manager open: cannot open the error page: {error}", file=sys.stderr)
        return 1


def _recovery_error_page(cause):
    """Refresh the signed offer before showing the manager's recovery page."""
    from matuto import updates
    try:
        from matuto import error_page
        previous = error_page.read_update_consent()
        checked = updates.check(persist=False)
        list_number, stale = _page_list_floor(previous, checked)
        offer = None if stale else checked.get("offer")
        status = (updates.install_status(
            offer=offer, list_number=list_number, settle_work=False) if offer else {})
        details = ({**offer, "installedVersion": checked.get("installedVersion")}
                   if offer else None)
        if stale:
            explanation = "The signed release list is stale; no update is offered."
        elif not offer:
            withdrawn = checked.get("withdrawn")
            if isinstance(withdrawn, dict):
                explanation = f"Release {withdrawn.get('version', 'unknown')} is withdrawn; no update is offered."
            else:
                explanation = checked.get("reason") or "No newer release is available."
        else:
            explanation = ""
        if explanation:
            cause = f"{cause} {explanation}"
        return _error_page(cause, offer=details, blockers=status.get("blockers", []),
                           list_number=list_number)
    except (OSError, UpdateError, releases.ReleaseError, store.StoreError,
            snapshot.SnapshotError) as error:
        return _error_page(f"{cause} Update check failed: {error}")


def _open_manager_locked():
    """Recover the manager while excluding an update for the duration of the action."""
    try:
        raw = store.read_update_state(store.store_path())
        state = _decode_update_state(raw)
        if state and state.get("stage") in UPDATE_STAGES:
            version = state.get("version", "unknown")
            stage = state["stage"]
            progress = f"Matuto update to {version} is in progress ({stage})."
            return _error_page(progress)
        if state and state.get("stage") == "maintenance":
            reason = state.get("reason") or "the update ended in maintenance without a recorded error"
            return _error_page(f"Matuto update to {state.get('version', 'unknown')} ended in maintenance: {reason}")
    except (OSError, store.StoreError, sqlite3.Error) as error:
        print(f"matuto-manager open: cannot check update state: {error}", file=sys.stderr)
        return 1

    try:
        addresses = _resolved_loopback_addresses() | {_manager_health_address()}
        owners = _listening_owners(addresses)
        refusal = _owner_refusal(owners)
        if refusal:
            return _recovery_error_page(refusal)
        reset = _systemctl(["reset-failed", MANAGER_UNIT])
        if reset.returncode:
            return _recovery_error_page(
                f"Could not reset the manager's failed state: {reset.stdout.strip()}")
        started = _systemctl(["start", MANAGER_UNIT])
        if started.returncode:
            return _recovery_error_page(f"Could not start the manager: {started.stdout.strip()}")
        try:
            answered = _manager_answers(addresses)
            if not answered:
                restarted = _systemctl(["restart", MANAGER_UNIT])
                answered = not restarted.returncode and _manager_answers(addresses)
        except UpdateError as error:
            return _recovery_error_page(str(error))
        if not answered:
            detail = restarted.stdout.strip() if restarted.returncode else "the manager did not answer after one restart"
            return _recovery_error_page(f"The manager failed to answer at {MANAGER_OPEN_URL}: {detail}")
        owners = _listening_owners(addresses)
        refusal = _owner_refusal(owners)
        if refusal:
            return _recovery_error_page(refusal)
        return subprocess.run(["xdg-open", MANAGER_OPEN_URL], check=False).returncode
    except UpdateError as error:
        return _error_page(str(error))
    except (OSError, ValueError, store.StoreError) as error:
        print(f"matuto-manager open: {error}", file=sys.stderr)
        return 1


def _update_busy_cause():
    try:
        state = _decode_update_state(store.read_update_state(store.store_path()))
    except (OSError, store.StoreError, sqlite3.Error):
        state = None
    if state and state.get("stage") in UPDATE_STAGES:
        return f"Matuto update to {state.get('version', 'unknown')} is in progress ({state['stage']})."
    if state and state.get("stage") == "maintenance":
        reason = state.get("reason") or "the update ended in maintenance without a recorded error"
        return f"Matuto update to {state.get('version', 'unknown')} ended in maintenance: {reason}"
    return "A Matuto update is in progress; the manager was left untouched."


def open_manager():
    """Recover Matuto while excluding a concurrent updater process."""
    try:
        lock_fd = _updater_lock(data_directory(), shared=True)
    except UpdateError as error:
        cause = (_update_busy_cause() if str(error) == "another Matuto update is already running"
                 else f"Cannot safely coordinate manager recovery: {error}")
        return _error_page(cause)
    try:
        return _open_manager_locked()
    finally:
        os.close(lock_fd)


def _page_list_floor(named, checked):
    """Keep consent metadata monotonic without persisting a check in the store."""
    previous = named.get("listNumber") if isinstance(named, dict) else None
    current = checked.get("listNumber")
    if not isinstance(previous, int) or isinstance(previous, bool):
        return current, False
    if not isinstance(current, int) or isinstance(current, bool):
        return previous, False
    return (previous, True) if current < previous else (current, False)


def _update_from_menu():
    """Install only the signed offer and list number shown on the error page."""
    from matuto import updates
    offer = None
    try:
        from matuto import error_page
        named = error_page.read_update_consent()
        previous_offer = named.get("offer") if named else None
        previous_number = named.get("listNumber") if named else None
        previous_blockers = named.get("blockers", []) if named else []
        checked = updates.check(persist=False)
        list_number, stale = _page_list_floor(named, checked)
        offer = None if stale else checked.get("offer")
        if stale or not offer:
            if stale:
                reason = "The signed release list is stale; no update is offered."
            else:
                withdrawn = checked.get("withdrawn")
                if isinstance(withdrawn, dict):
                    reason = f"Release {withdrawn.get('version', 'unknown')} is withdrawn; no update is offered."
                else:
                    reason = checked.get("reason") or "No newer release is available."
            _error_page(reason, list_number=list_number)
            return _open_update_screen(reason)
        current = {**offer, "installedVersion": checked.get("installedVersion")}
        status = updates.install_status(offer=offer, list_number=list_number, settle_work=False)
        blockers = status.get("blockers", [])
        if (not isinstance(previous_offer, dict) or previous_number != checked.get("listNumber")
                or previous_offer != offer):
            _error_page("The release offer changed. Review the updated information before installing.",
                        offer=current, blockers=blockers, list_number=checked.get("listNumber"))
            return 0
        if previous_blockers != blockers:
            _error_page("What must stop has changed. Review the updated error page before installing Matuto.",
                        offer=current, blockers=blockers, list_number=checked.get("listNumber"))
            return 0
        if blockers:
            _error_page("Stop the listed work and check the prerequisites before updating Matuto.",
                        offer=current, blockers=blockers, list_number=checked.get("listNumber"))
            return 0
        with error_page.install_consent() as current:
            if (not isinstance(current, dict)
                    or current.get("offer") != previous_offer
                    or current.get("listNumber") != previous_number
                    or current.get("blockers", []) != previous_blockers):
                return 0
            updates.install(offer["version"], list_number, open_after=True,
                            persist_check=False)
        return 0
    except UpdaterLaunchUncertain:
        return 1
    except (OSError, UpdateError, releases.ReleaseError, store.StoreError,
            snapshot.SnapshotError) as error:
        try:
            latest = updates.install_status(offer=offer, settle_work=False)
            offer = latest.get("offer")
            details = ({**offer, "installedVersion": latest.get("installedVersion")}
                       if offer else None)
            _error_page(str(error), offer=details, blockers=latest.get("blockers", []),
                        list_number=latest.get("listNumber"))
        except Exception:
            offer = None
            _error_page(str(error))
        return 1 if offer else _open_update_screen(str(error))


def _open_update_screen(cause=""):
    """Open Maintenance only after refusing any address held by another account."""
    try:
        addresses = _resolved_loopback_addresses() | {_manager_health_address()}
        refusal = _owner_refusal(_listening_owners(addresses))
        if refusal:
            return _error_page(f"{cause} {refusal}".strip())
        return subprocess.run(["xdg-open", MANAGER_OPEN_URL + "#/settings/maintenance"],
                              check=False).returncode
    except (OSError, UpdateError) as error:
        return _error_page(f"{cause} {error}".strip())


def _record_install_result(version, stage, reason, *, list_number, unit_name):
    """Record an outcome only while this transient unit still owns the reservation."""
    try:
        lock_fd = _updater_lock(data_directory())
    except UpdateError as error:
        if str(error) == "another Matuto update is already running":
            return
        raise
    try:
        return store.record_app_install_result(
            store.store_path(), installed_version=installed_version(), version=version,
            list_number=list_number, unit_name=unit_name, stage=stage, reason=reason)
    finally:
        os.close(lock_fd)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Run an installed Matuto menu action or install a verified release.",
        epilog="The trusted release-feed base URL and public key are recorded by matuto-bootstrap.")
    parser.add_argument("action", nargs="?", choices=("open", "repair", "update", "remove"),
                        help="menu action to run")
    parser.add_argument("--install-from-file", nargs=2,
                        metavar=("RELEASE_FILE", "RELEASE_LIST"),
                        help="install a signed release and list already present on disk")
    parser.add_argument("--version", help="install exactly the reviewed release version")
    parser.add_argument("--list-number", type=int, help="require the reviewed signed-list number")
    parser.add_argument("--install-unit", help=argparse.SUPPRESS)
    parser.add_argument("--open-after-install", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config-root", help=argparse.SUPPRESS)
    parser.add_argument("--data-root", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (args.version or args.list_number is not None or args.install_unit is not None) and args.action != "update":
        parser.error("pinned install arguments are only valid with the update action")
    if (args.config_root is not None or args.data_root is not None) and args.action != "remove":
        parser.error("removal paths are only valid with the remove action")
    if args.install_unit and not APP_INSTALL_UNIT_PATTERN.fullmatch(args.install_unit):
        parser.error("--install-unit must name the app updater's transient service")
    if args.action == "update" and not any((args.version, args.list_number is not None,
                                               args.install_unit)):
        return _update_from_menu()
    if args.action == "update" and (not args.version or args.list_number is None or not args.install_unit):
        parser.error("pinned updates require a version, list number and install unit")
    if args.action == "open":
        return open_manager()
    if args.action == "repair":
        try:
            result = repair()
        except (OSError, UpdateError, releases.ReleaseError, store.StoreError,
                snapshot.SnapshotError) as error:
            try:
                from matuto.error_page import write
                write(str(error), "", "")
            except Exception as page_error:
                print(f"matuto-repair: error page could not be written: {page_error}",
                      file=sys.stderr)
            print(f"matuto-repair: {error}", file=sys.stderr)
            return 1
        if result.recovered_update:
            print(f"Step 1: reconciled the interrupted update to Matuto {result.version}")
            if result.updater_warning:
                print(f"Step 1: installed updater replacement pending: {result.updater_warning}",
                      file=sys.stderr)
            print("Step 4: the manager opened its database and served the sign-in page")
        else:
            print("Step 1: no interrupted update needed reconciliation")
            print(f"Step 2: verified Matuto {result.version}; restored "
                  f"{len(result.repaired_files)} installed files")
            for path in result.repaired_files:
                print(f"Restored {path}")
            print("Step 3: regenerated the service entries and router files, restored permissions, "
                  "and preserved or created the router key")
            print("Step 4: the manager opened its database and served the sign-in page")
        return 0
    if args.action == "remove":
        from matuto.removal import RemovalError, finish
        try:
            finish(config_root=args.config_root, data_root=args.data_root)
        except (OSError, RemovalError, UpdateError, store.StoreError) as error:
            print(f"matuto-update remove: {error}", file=sys.stderr)
            return 1
        print("Matuto and its updater were removed.")
        return 0
    from matuto.bootstrap import BootstrapError, trusted_inputs
    try:
        feed_url, key = trusted_inputs()
        result = (apply(feed_url, key, version=args.version, list_number=args.list_number)
                  if args.action == "update" else
                  apply_file(args.install_from_file[0], args.install_from_file[1], key)
                  if args.install_from_file else apply(feed_url, key))
    except (OSError, BootstrapError, UpdateError, releases.ReleaseError, store.StoreError,
            snapshot.SnapshotError) as error:
        if args.action == "update" and args.version and args.install_unit:
            _record_install_result(args.version, "failed", str(error),
                                   list_number=args.list_number, unit_name=args.install_unit)
            if args.open_after_install:
                _error_page(str(error))
        print(f"matuto-update: {error}", file=sys.stderr)
        return 1
    if args.action == "update":
        _record_install_result(result.version, "complete", "",
                               list_number=args.list_number, unit_name=args.install_unit)
        if args.open_after_install:
            return open_manager()
    print(f"Installed Matuto {result.version}")
    if result.updater_warning:
        print(f"matuto-update: installed updater was not replaced: {result.updater_warning}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
