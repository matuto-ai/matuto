"""Trust a Matuto release key and bootstrap a verified release installation."""

import contextlib
import hashlib
import json
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matuto import releases, snapshot, store, updater


class BootstrapError(Exception):
    """A refused bootstrap or an install step that needs to be retried."""


def _fingerprint(public_key):
    try:
        releases._allowed_signers(public_key)
    except releases.ReleaseError as error:
        raise BootstrapError(str(error)) from None
    with tempfile.TemporaryDirectory(prefix="matuto-bootstrap-key-") as directory:
        path = Path(directory) / "release.pub"
        path.write_bytes(public_key)
        path.chmod(0o600)
        try:
            result = subprocess.run(["ssh-keygen", "-l", "-f", str(path)],
                                    capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BootstrapError(f"cannot fingerprint the release key: {error}") from None
    fields = result.stdout.split()
    if result.returncode or len(fields) < 2 or not fields[1].startswith("SHA256:"):
        reason = (result.stderr or result.stdout).strip().splitlines()
        raise BootstrapError("cannot fingerprint the release key" +
                             (f": {reason[-1]}" if reason else ""))
    return fields[1]


def _trusted_inputs(config_root):
    path = store.store_path(config_root)
    if not path.exists():
        return None
    state = store.read_update_state(path)
    if state is None:
        return None
    try:
        details = json.loads(state["phase"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(details, dict):
        return None
    key, address = details.get("trusted_key"), details.get("release_address")
    if isinstance(key, str) and isinstance(address, str):
        return key.encode("utf-8"), address
    return None


def _ensure_router_key(config_root):
    config_root = Path(config_root) if config_root is not None else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    directory = config_root / store.CONFIG_ROOT_NAME / "pi-local"
    if not directory.exists():
        directory.mkdir(parents=True, mode=0o700)
    store._private_directory(directory)
    path = directory / "api-key"
    if os.path.lexists(path):
        store._safe_file(path, "Router API key")
        if path.lstat().st_nlink != 1:
            raise BootstrapError(f"the Router API key has multiple hard links: {path}")
        return
    temporary = directory / f".api-key-{secrets.token_hex(16)}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(descriptor, (secrets.token_urlsafe(32) + "\n").encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path, follow_symlinks=False)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
    store._safe_file(path, "Router API key")


def _archive_schema_version(archive_data):
    """Read the constant schema version from the already-verified release archive."""
    import ast
    import io
    import tarfile

    try:
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as archive:
            source = archive.extractfile("agents/matuto/store.py").read().decode("utf-8")
        module = ast.parse(source)
        for node in module.body:
            if (isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION"
                    for target in node.targets) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, int)):
                return node.value.value
    except (OSError, UnicodeDecodeError, SyntaxError, KeyError, AttributeError, tarfile.TarError):
        pass
    raise BootstrapError("the verified release has no readable store schema version")


def _tree_files(root):
    root = Path(root)
    try:
        status = root.lstat()
        if (not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid()
                or status.st_mode & 0o022):
            return None
        found = {}
        for directory, subdirectories, filenames in os.walk(root, followlinks=False):
            for name in subdirectories:
                child = Path(directory) / name
                child_status = child.lstat()
                if (not stat.S_ISDIR(child_status.st_mode) or child_status.st_uid != os.getuid()
                        or child_status.st_mode & 0o022):
                    return None
            for name in filenames:
                path = Path(directory) / name
                status = path.lstat()
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    return None
                found[str(path.relative_to(root))] = path
        return found
    except OSError:
        return None


def _release_matches(path, archive_data, verified_files):
    """Reuse only an installed tree whose files still match the signed archive."""
    import io
    import tarfile

    try:
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as archive:
            manifest = json.load(archive.extractfile("RELEASE.json"))
            expected = set(manifest["files"]) | {"RELEASE.json"}
            archive_digests = {item.name: hashlib.sha256(archive.extractfile(item).read()).hexdigest()
                               for item in archive.getmembers() if item.isfile()}
        installed = _tree_files(path)
        if installed is None or installed.keys() != expected:
            return False
        return all(hashlib.sha256(installed[name].read_bytes()).hexdigest() == archive_digests[name]
                   for name, _size, _mode in verified_files)
    except (OSError, KeyError, TypeError, ValueError, tarfile.TarError):
        return False


def _install_updater(data_root, release_path):
    root = Path(data_root) / "updater"
    versions = root / "versions"
    source = release_path / "agents"
    version = Path(release_path).name
    if not source.is_dir() or source.is_symlink():
        raise BootstrapError("the verified release has no updater package")
    try:
        updater._version(version)
        updater._safe_dir(root, create=True)
        updater._safe_dir(versions, create=True)
    except updater.UpdateError as error:
        raise BootstrapError(str(error)) from None

    current = root / "current"
    current_target = None
    current_version = None
    if current.is_symlink():
        try:
            candidate = current.resolve(strict=True)
            if candidate.parent == versions.resolve(strict=True):
                updater._safe_dir(candidate)
                current_target = candidate
                current_version = updater._version(candidate.name)
        except (OSError, RuntimeError, updater.UpdateError):
            pass
    if current_version is not None and current_version > updater._version(version):
        return

    destination = versions / version
    if destination.exists() or destination.is_symlink():
        try:
            updater._safe_dir(destination)
        except updater.UpdateError as error:
            raise BootstrapError(str(error)) from None
        if (current_target == destination.resolve(strict=True)
                and _same_tree(source, destination / "agents")):
            return

    staging = Path(tempfile.mkdtemp(prefix=".updater-install-", dir=root))
    backup = None
    temporary_pointer = None
    try:
        shutil.copytree(source, staging / "agents", symlinks=False)
        for directory, _subdirs, _files in os.walk(staging):
            os.chmod(directory, 0o700)
        updater._sync_tree(staging)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink():
                raise BootstrapError(f"the installed updater version is unsafe: {destination}")
            if current_target == destination.resolve(strict=True):
                backup = root / f".updater-backup-{secrets.token_hex(16)}"
                os.replace(destination, backup)
            else:
                shutil.rmtree(destination)
        os.replace(staging, destination)
        updater._sync_directory(versions)
        temporary_pointer = root / f".current-{secrets.token_hex(16)}"
        os.symlink(f"versions/{version}", temporary_pointer)
        os.replace(temporary_pointer, current)
        updater._sync_directory(root)
    except BaseException:
        if backup is not None and backup.exists() and not destination.exists():
            os.replace(backup, destination)
            updater._sync_directory(versions)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if temporary_pointer is not None:
            temporary_pointer.unlink(missing_ok=True)
    if backup is not None:
        shutil.rmtree(backup)


def trusted_inputs(config_root=None):
    """Return the persisted release address and key, refusing absent or corrupt trust."""
    inputs = _trusted_inputs(config_root)
    if inputs is None:
        raise BootstrapError("no release key and address have been trusted; run matuto-bootstrap first")
    key, address = inputs
    try:
        releases._allowed_signers(key)
    except releases.ReleaseError as error:
        raise BootstrapError(f"the stored release key is invalid: {error}") from None
    return address, key


def _same_tree(source, installed):
    expected, actual = _tree_files(source), _tree_files(installed)
    if expected is None or actual is None or expected.keys() != actual.keys():
        return False
    try:
        return all(not path.stat().st_mode & 0o022 and path.read_bytes() == actual[name].read_bytes()
                   for name, path in expected.items())
    except OSError:
        return False


def _systemd_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _desktop_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%") + '"'


def _manager_command(data_root):
    script = Path(data_root) / "releases/current/agents/matuto/cli.py"
    return f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -B {shlex.quote(str(script))} \"$@\"\n"


def _manager_unit(data_root):
    script = Path(data_root) / "releases/current/agents/matuto/cli.py"
    return "\n".join((
        "[Unit]", "Description=Matuto Manager", "StartLimitIntervalSec=60", "StartLimitBurst=3",
        "", "[Service]", "Type=simple",
        f"ExecStart={_systemd_quote(sys.executable)} -B {_systemd_quote(script)} serve",
        "Restart=on-failure", "RestartSec=2", "", "[Install]", "WantedBy=default.target", ""))


def _desktop_entry(name, updater_script, action):
    command = " ".join((_desktop_quote(sys.executable), _desktop_quote(updater_script), action))
    return "\n".join(("[Desktop Entry]", "Type=Application", f"Name={name}",
                        f"Exec={command}", "Terminal=false", "Categories=Utility;", ""))


def _file_needs_update(path, content, *, mode=None):
    path = Path(path)
    try:
        status = path.lstat()
    except FileNotFoundError:
        return True
    if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
            or status.st_nlink != 1):
        raise BootstrapError(f"refusing to replace an unsafe generated file: {path}")
    return (path.read_text(encoding="utf-8") != content
            or mode is not None and stat.S_IMODE(status.st_mode) != mode)


def _write_if_changed(path, content, *, mode=0o644):
    path = Path(path)
    if not _file_needs_update(path, content, mode=mode):
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def _systemctl(args):
    try:
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                                text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BootstrapError(f"systemd did not answer while installing the manager: {error}") from None
    if result.returncode:
        raise BootstrapError(f"systemctl --user {' '.join(args)} failed: {result.stdout.strip()}"
                             .rstrip())


def _install_login_and_menu(data_root, config_root):
    unit = Path(config_root) / "systemd/user/matuto-manager.service"
    applications = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"
    updater_script = Path(data_root) / "updater/current/agents/matuto/updater.py"
    state_path = store.store_path(config_root)
    previous = store.read_update_state(state_path)
    try:
        state = json.loads(previous["phase"]) if previous else {}
    except (KeyError, TypeError, ValueError):
        state = {}
    state = state if isinstance(state, dict) else {}
    pending = state.get("manager_enable_pending") is True
    reload_pending = state.get("manager_daemon_reload_pending") is True
    unit_text = _manager_unit(data_root)
    unit_changed = _file_needs_update(unit, unit_text)
    if "manager_enable_pending" not in state:
        pending = not unit.exists()
    reload_pending = reload_pending or unit_changed
    if ("manager_enable_pending" not in state or unit_changed
            or state.get("manager_daemon_reload_pending") is not reload_pending):
        current = updater.installed_version(data_root)
        store.write_update_state(
            state_path, current, "complete",
            details={"manager_enable_pending": pending,
                     "manager_daemon_reload_pending": reload_pending})
    _write_if_changed(unit, unit_text)
    _write_if_changed(Path.home() / ".local/bin/matuto-manager",
                      _manager_command(data_root), mode=0o755)
    if reload_pending:
        _systemctl(["daemon-reload"])
        current = updater.installed_version(data_root)
        store.write_update_state(state_path, current, "complete",
                                 details={"manager_daemon_reload_pending": False})
    entries = (("matuto.desktop", "Matuto", "open"),
               ("repair-matuto.desktop", "Repair Matuto", "repair"),
               ("update-matuto.desktop", "Update Matuto", "update"))
    for filename, name, action in entries:
        _write_if_changed(applications / filename,
                          _desktop_entry(name, updater_script, action))
    if pending:
        _systemctl(["enable", "matuto-manager.service"])
        current = updater.installed_version(data_root)
        store.write_update_state(state_path, current, "complete",
                                 details={"manager_enable_pending": False})


def install(feed_url, public_key, *, confirm_fingerprint, config_root=None, data_root=None,
            fetcher=None):
    """Serialize bootstrap with updates, then verify, install and trust one release."""
    data_root = Path(data_root) if data_root is not None else updater.data_directory()
    try:
        updater._safe_dir(data_root, create=True)
        lock = updater._updater_lock(data_root)
    except updater.UpdateError as error:
        raise BootstrapError(str(error)) from None
    try:
        return _install_locked(feed_url, public_key, confirm_fingerprint=confirm_fingerprint,
                               config_root=config_root, data_root=data_root, fetcher=fetcher)
    finally:
        os.close(lock)


def _interrupted_creation(path):
    if not os.path.lexists(path):
        return False
    store._safe_file(path, "Matuto store")
    sidecars = store._safe_sidecars(path)
    interrupted = path.stat().st_size == 0 and not sidecars
    if sidecars:
        _has_meta, _version, interrupted = store._committed_version(path, sidecars)
    return interrupted


def _install_locked(feed_url, public_key, *, confirm_fingerprint, config_root=None, data_root=None,
                    fetcher=None):
    """Verify, install and trust a release; confirmation must match its fingerprint."""
    public_key = public_key.encode("utf-8") if isinstance(public_key, str) else public_key
    if not isinstance(public_key, bytes):
        raise BootstrapError("the release public key must be text or bytes")
    fingerprint = _fingerprint(public_key)
    if confirm_fingerprint != fingerprint:
        raise BootstrapError(f"trust requires confirming the displayed fingerprint {fingerprint}")
    config_root = Path(config_root) if config_root is not None else None
    data_root = Path(data_root) if data_root is not None else updater.data_directory()
    path = store.store_path(config_root)
    try:
        interrupted_creation = _interrupted_creation(path)
        accepted_number = updater._read_list_number(data_root)
        raw_state = (store.read_update_state(path)
                     if os.path.lexists(path) and not interrupted_creation else None)
        persisted = {}
        if raw_state is not None:
            try:
                persisted = json.loads(raw_state["phase"])
                if not isinstance(persisted, dict):
                    persisted = {}
                number = persisted.get("list_number")
                if isinstance(number, int) and not isinstance(number, bool):
                    accepted_number = max(accepted_number, number) if accepted_number is not None else number
            except (KeyError, TypeError, ValueError):
                persisted = {}
        current = None
        with contextlib.suppress(updater.UpdateError):
            current = updater.installed_version(data_root)
        recovery_version = (raw_state["release_version"]
                            if persisted.get("bootstrap_activation")
                            and persisted.get("stage") == "activating" else None)
        fetch_version = recovery_version or current
        reading, release, archive = (fetcher(feed_url, accepted_number, public_key,
                                             version=fetch_version,
                                             allow_withdrawn=bool(fetch_version)) if fetcher else
                                     updater._fetch(feed_url, accepted_number, public_key,
                                                    version=fetch_version,
                                                    allow_withdrawn=bool(fetch_version)))
    except (releases.ReleaseError, updater.UpdateError, store.StoreError) as error:
        raise BootstrapError(str(error)) from None
    if release is None:
        raise BootstrapError("the signed release list has no installable release")
    try:
        updater._check_archive(release, archive)
        verified_files, _expanded_size = updater._archive_members(archive)
    except (releases.ReleaseError, updater.UpdateError) as error:
        raise BootstrapError(str(error)) from None

    target_schema = _archive_schema_version(archive)
    trusted = public_key.decode("utf-8").strip()
    path = store.store_path(config_root)
    try:
        store._private_directory(path.parent)
        had_store = os.path.lexists(path)
        existing_schema = None
        if had_store:
            store._safe_file(path, "Matuto store")
            store._safe_sidecars(path)
            if not interrupted_creation:
                existing_schema = store.read_schema_version(path)
    except store.StoreError as error:
        raise BootstrapError(str(error)) from None
    if had_store and not interrupted_creation and existing_schema > target_schema:
        raise BootstrapError(
            f"the kept store schema version {existing_schema} is newer than release "
            f"{release.version} schema version {target_schema}")

    activation_lock = None
    try:
        updater._safe_dir(data_root, create=True)
        updater._safe_dir(updater.releases_directory(data_root), create=True)
        if current is not None and current != release.version and not recovery_version:
            raise BootstrapError(
                f"Matuto {current} is already installed; install Matuto {release.version} with the updater")
        destination = updater.releases_directory(data_root) / release.version
        if destination.exists() or destination.is_symlink():
            try:
                updater._safe_dir(destination)
            except updater.UpdateError as error:
                raise BootstrapError(str(error)) from None
            if not _release_matches(destination, archive, verified_files):
                if current == release.version:
                    raise BootstrapError(f"the active release is damaged: {destination}")
                shutil.rmtree(destination)
        if not destination.exists():
            release_path = updater._extract(data_root, release.version, archive, verified_files)
        else:
            release_path = destination

        if interrupted_creation:
            _create_store_from_release(release_path, config_root)
            existing_schema = store.read_schema_version(path)

        if had_store:
            if target_schema > existing_schema:
                activation_lock = store.acquire_update_lock(config_root)
                opened = None
                try:
                    store._safe_file(path, "Matuto store")
                    if store._safe_sidecars(path):
                        raise BootstrapError("the kept store has an unfinished write; retry bootstrap later")
                    existing_schema = store.read_schema_version(path)
                    if existing_schema >= target_schema:
                        raise BootstrapError("the kept store changed while bootstrap was preparing; retry bootstrap")
                    opened = store.Store(path, store._connect(path))
                    snapshot.take(opened, release.version)
                    staged = updater._stage_store(release_path, config_root)
                    store.write_update_state(
                        staged, release.version, "activating",
                        list_number=reading.release_list.number,
                        details={"trusted_key": trusted, "release_address": feed_url,
                                 "bootstrap_activation": True})
                finally:
                    if opened is not None:
                        opened.close()
                os.replace(staged, path)
        else:
            _create_store_from_release(release_path, config_root)

        _ensure_router_key(config_root)
        _install_updater(data_root, release_path)
        if current is None:
            previous = store.read_update_state(path)
            try:
                prior_state = json.loads(previous["phase"]) if previous else {}
            except (KeyError, TypeError, ValueError):
                prior_state = {}
            stage = prior_state.get("stage", "complete") if isinstance(prior_state, dict) else "complete"
            store.write_update_state(
                path, release.version, stage,
                list_number=reading.release_list.number,
                details={"trusted_key": trusted, "release_address": feed_url,
                         "manager_enable_pending": True})
        if current != release.version:
            updater._atomic_current(data_root, release.version)
        previous = store.read_update_state(path)
        try:
            prior_details = json.loads(previous["phase"]) if previous else {}
        except (KeyError, TypeError, ValueError):
            prior_details = {}
        if not isinstance(prior_details, dict):
            prior_details = {}
        _install_login_and_menu(data_root, config_root or Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")))
        previous = store.read_update_state(path)
        try:
            prior_details = json.loads(previous["phase"]) if previous else {}
        except (KeyError, TypeError, ValueError):
            prior_details = {}
        if not (previous and previous["release_version"] == release.version
                and prior_details.get("trusted_key") == trusted
                and prior_details.get("release_address") == feed_url
                and prior_details.get("list_number") == reading.release_list.number
                and prior_details.get("stage") == "complete"):
            details = {"trusted_key": trusted, "release_address": feed_url,
                       "manager_enable_pending": False}
            if prior_details.get("bootstrap_activation"):
                details["bootstrap_activation"] = False
            store.write_update_state(path, release.version, "complete",
                                     list_number=reading.release_list.number, details=details)
    except (OSError, store.StoreError, snapshot.SnapshotError, updater.UpdateError) as error:
        raise BootstrapError(str(error)) from None
    finally:
        if activation_lock is not None:
            os.close(activation_lock)
    return release.version, fingerprint


def _create_store_from_release(release_path, config_root):
    script = ("import sys; sys.path.insert(0, sys.argv[1]); from matuto import store; "
              "opened = store.open_store(sys.argv[2] or None, exclusive=True); opened.close()")
    try:
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script,
                                 str(Path(release_path) / "agents"), str(config_root or "")],
                                cwd=release_path, capture_output=True, text=True,
                                timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BootstrapError(f"the verified release could not create its store: {error}") from None
    if result.returncode:
        raise BootstrapError("the verified release could not create its store: " + result.stderr.strip())


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Trust and install a signed Matuto release.")
    parser.add_argument("--feed", required=True, help="release-list base address to trust")
    parser.add_argument("--trusted-key", required=True, help="release public-key file")
    args = parser.parse_args(argv)
    try:
        key = Path(args.trusted_key).read_bytes()
        fingerprint = _fingerprint(key)
        print(f"Release key fingerprint: {fingerprint}")
        if input("Compare this fingerprint with the published install instructions, then type it to continue: ").strip() != fingerprint:
            raise BootstrapError("the release key fingerprint was not confirmed")
        version, _fingerprint_value = install(args.feed, key, confirm_fingerprint=fingerprint)
    except (OSError, BootstrapError) as error:
        print(f"matuto-bootstrap: {error}", file=sys.stderr)
        return 1
    print(f"Installed Matuto {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
