"""Remove the installed Matuto release while preserving operator data."""

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

from matuto import admission, bootstrap, serving_state, snapshot, store, updater
from matuto.matuto_runtime import InspectionError

MANAGER_FILES = {"matuto", "matuto_runtime.py"}
RECOVERY_NAME = ".matuto-removal.pyz"
RECOVERY_ENTRYPOINT = (
    "import sys\n"
    "from matuto.removal import finish\n"
    "try:\n"
    "    finish(config_root={config!r}, data_root={data!r})\n"
    "except Exception as error:\n"
    "    print(f'Matuto removal recovery: {{error}}', file=sys.stderr)\n"
    "    sys.exit(1)\n"
)


class RemovalError(Exception):
    """A refusal or an interrupted removal that can be retried."""


def preview(*, config_root=None, data_root=None):
    """Return a read-only removal and retention plan with current sizes."""
    config = Path(config_root) if config_root is not None else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    data = Path(data_root) if data_root is not None else updater.data_directory()
    state_dir = store.store_directory(config_root)
    unit_dir = config / "systemd/user"
    applications = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"
    remove = [
        ("Generated router preset", state_dir / "router-presets.ini"),
        ("Generated voice preset", state_dir / "voice-presets.ini"),
        ("Agentic router service", unit_dir / serving_state.ROUTER_UNIT),
        ("Voice router service", unit_dir / serving_state.VOICE_ROUTER_UNIT),
        ("Manager service (final step)", unit_dir / updater.MANAGER_UNIT),
        ("Matuto menu entry", applications / "matuto.desktop"),
        ("Repair Matuto menu entry", applications / "repair-matuto.desktop"),
        ("Update Matuto menu entry", applications / "update-matuto.desktop"),
        ("Installed matuto-manager command", Path.home() / ".local/bin/matuto-manager"),
        ("Release files and commands", updater.releases_directory(data)),
        ("Independent updater (last)", data / "updater"),
    ]
    if unit_dir.is_dir():
        remove.extend(("Generated voice service", unit_dir / name)
                      for name in sorted(os.listdir(unit_dir))
                      if name.startswith("matuto-app-voice-part-") and name.endswith(".service"))
    try:
        opened = store.read_store(config_root)
    except store.StoreError:
        opened = None
    try:
        keep = _kept_paths(config_root, opened.connection() if opened else None)
    finally:
        if opened is not None:
            opened.close()
    removed_items = [_item(label, path) for label, path in remove]
    removed_items.append({
        "item": "Transient voice-serving records",
        "path": f"{store.store_path(config_root)} (voice_serving, voice_serving_parts)",
        "size_bytes": 0,
    })
    return {"remove": removed_items,
            "keep": [_item(label, path) for label, path in keep],
            "consumer_configuration": "Never touched.",
            "later_bootstrap": "Reuses the kept data.",
            "blockers": blockers(config_root=config_root, data_root=data)}


def _unrecorded_work_units(recorded):
    pattern = re.compile(r"matuto-(?:verify|smoke|bench|serve|bundle|wq|context_search|build|download)-[a-z0-9]+\.service")
    try:
        result = subprocess.run(["systemctl", "--user", "list-units", "--all", "--no-legend", "--plain", "matuto-*.service"],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    unknown = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            if line.strip() and not re.fullmatch(r"\d+ units? files? listed\.", line.strip()):
                return None
            continue
        unit, active = fields[0], fields[2]
        if pattern.fullmatch(unit):
            if active not in serving_state.ANSWERED:
                return None
            if active in ("active", "activating", "deactivating", "reloading") and unit not in recorded:
                unknown.append(unit)
    return unknown


def _item(label, path):
    if path is None:
        return {"item": label, "path": "operator-configured locations", "size_bytes": 0}
    path = Path(path)
    return {"item": label, "path": str(path), "size_bytes": _size(path)}


def _size(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    if not stat.S_ISDIR(info.st_mode):
        return 0
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            try:
                info = (Path(root) / name).lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def blockers(*, config_root=None, data_root=None, opened_store=None, updater_locked=False):
    """Read serving, work and update state without settling or changing them."""
    try:
        connection = opened_store.connection() if opened_store is not None else None
        state = serving_state.read(config_root=config_root, store_connection=connection)
    except Exception:
        state = serving_state.State(admission.SERVING_UNKNOWN)
    if state.state not in (admission.STOPPED,):
        if state.state == admission.SERVING_UNKNOWN:
            reasons = ["serving state is unknown; verify serving is stopped on Overview before removal"]
        else:
            reasons = ["Stop serving on Overview before removing Matuto"]
    else:
        reasons = []
    updater_busy = None if updater_locked else _updater_active(data_root)
    if updater_busy is None and not updater_locked:
        reasons.append("update state is unknown; the updater lock cannot be checked")
    elif updater_busy:
        reasons.append("an update is installing; it must end before removing Matuto")
    try:
        opened = opened_store or store.read_store(config_root)
    except Exception:
        opened = None
    if opened is None:
        try:
            update = store.read_update_state(store.store_path(config_root))
        except store.StoreError:
            update = None
            update_unknown = True
        else:
            update_unknown = False
        unavailable = reasons + ["Matuto store or work state is unknown; check Runs & jobs and stop or cancel active work before removal"]
        if update_unknown:
            unavailable.append("update state is unknown; retry when it can be read")
        elif update and not updater_busy:
            unavailable.extend(_update_blockers(update.get("phase")))
        return unavailable
    try:
        connection = opened.connection()
        work = connection.execute(
            "SELECT run_id AS id, procedure AS kind, unit_name AS unit FROM runs WHERE outcome IS NULL "
            "UNION ALL SELECT job_id, kind, unit_name AS unit FROM jobs WHERE outcome IS NULL "
            "ORDER BY id").fetchall()
        for item in work:
            action = (f"Cancel download {item['id']}" if item["kind"] == admission.DOWNLOAD
                      else f"Stop {item['kind']} {item['id']}")
            reasons.append(f"{action} in Runs & jobs before removing Matuto")
        active_units = {item["unit"] for item in work if item["unit"]}
        unknown_units = _unrecorded_work_units(active_units)
        if unknown_units is None:
            reasons.append("job state is unknown; check Runs & jobs and stop or cancel active work before removal (systemd did not list the Matuto work units)")
        elif unknown_units:
            reasons.append("job state is unknown; check Runs & jobs and stop or cancel active work before removal; systemd reports: "
                           + ", ".join(unknown_units))
        row = connection.execute(
            "SELECT phase FROM update_state WHERE state_id = 1").fetchone()
    except Exception:
        return reasons + ["Matuto work or update state is unknown"]
    finally:
        if opened_store is None:
            opened.close()
    if row and not updater_busy:
        reasons.extend(_update_blockers(row["phase"]))
    return reasons


def _updater_active(data_root=None):
    import fcntl
    path = Path(data_root) if data_root is not None else updater.data_directory()
    path = path / ".updater.lock"
    try:
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            return None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _update_blockers(phase_text):
    try:
        phase = json.loads(phase_text)
        stage = phase.get("stage") if isinstance(phase, dict) else None
    except (TypeError, ValueError):
        stage = None
    if stage is None:
        return ["update state is unknown; retry when it can be read"]
    if stage not in ("complete", "failed"):
        return ["an update is installing; it must end before removing Matuto"]
    return []


def remove(*, confirm=False, config_root=None, data_root=None, on_kept=None):
    """Run the confirmed removal; every step is safe to retry."""
    if not confirm:
        return {"preview": preview(config_root=config_root, data_root=data_root), "removed": False}
    config = Path(os.path.abspath(config_root if config_root is not None else
                                   os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")))
    data = Path(os.path.abspath(data_root if data_root is not None else updater.data_directory()))
    if _progress(data) == "final-step":
        finish(config_root=config, data_root=data)
        return {"removed": True}
    reasons = blockers(config_root=config, data_root=data)
    if reasons:
        raise RemovalError("Removal refused: " + "; ".join(reasons))
    try:
        updater._safe_dir(data)
        _validate_parent_chain(data / ".updater.lock")
        _validate_parent_chain(store.store_path(config))
        runtime_path = Path.home() / ".config/ai-agents-sandbox/matuto-runtime/run.lock"
        _validate_parent_chain(runtime_path)
        if os.path.lexists(runtime_path):
            _validate_tree(runtime_path)
        update_lock = updater._updater_lock(data)
    except updater.UpdateError as error:
        raise RemovalError(f"Removal refused: {error}") from None
    runtime_lock = None
    opened = None
    try:
        updater._safe_dir(data, create=True)
        from matuto.matuto_runtime import runtime_lease
        runtime_lock = runtime_lease(exclusive=True)
        opened = store.open_store(config, exclusive=True, allow_newer=True)
        # Recheck after locks: no serving, work, or updater may begin between the
        # preview and the destructive first step.
        reasons = blockers(config_root=config, data_root=data, opened_store=opened,
                           updater_locked=True)
        if reasons:
            raise RemovalError("Removal refused: " + "; ".join(reasons))
        _validate_plan(config, data, opened.connection())
        _mark_progress(data, "first-step")
        _clear_runtime_records(opened)
        _remove_first_step(config, data)
        kept = [_item(label, path) for label, path in _kept_paths(config, opened.connection())]
        if on_kept is not None:
            on_kept(kept)
        _mark_progress(data, "final-step")
        opened.close()
        opened = None
        os.close(runtime_lock)
        runtime_lock = None
        os.close(update_lock)
        update_lock = None
        _run_finalizer(config, data)
        return {"removed": True, "kept": kept}
    except InspectionError as error:
        raise RemovalError(f"Removal refused: {error}") from None
    except RemovalError as error:
        if os.path.lexists(data / ".removal-state") and _progress(data) == "final-step":
            raise RemovalError(f"Final removal failed. Retry with `python3 {data / 'releases/current/agents/matuto/cli.py'} remove`: {error}") from None
        raise
    except (OSError, store.StoreError, updater.UpdateError, RuntimeError) as error:
        if os.path.lexists(data / ".removal-state") and _progress(data) == "final-step":
            raise RemovalError(f"Final removal failed. Retry with `python3 {data / 'releases/current/agents/matuto/cli.py'} remove`: {error}") from None
        raise RemovalError(f"Removal stopped before completion; run remove again: {error}") from None
    finally:
        if opened is not None:
            opened.close()
        if runtime_lock is not None:
            os.close(runtime_lock)
        if update_lock is not None:
            os.close(update_lock)


def _run_finalizer(config, data):
    updater_script = data / "updater/current/agents/matuto/updater.py"
    recovery = data / RECOVERY_NAME
    command = ["systemd-run", "--user", "--wait", "--pipe", "--collect",
               f"--unit=matuto-removal-finalizer-{os.getpid()}", sys.executable,
               str(updater_script), "remove", "--config-root", str(config),
               "--data-root", str(data)]
    retry = ([sys.executable, str(recovery)] if os.path.lexists(recovery) else
             [sys.executable, str(updater_script), "remove", "--config-root", str(config),
              "--data-root", str(data)])
    retry_command = " ".join(shlex.quote(part) for part in retry)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RemovalError(f"Final removal failed; complete it with `{retry_command}`: {error}") from None
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"systemd-run exited {result.returncode}"
        raise RemovalError(f"Final removal failed; complete it with `{retry_command}`: {detail}")


def finish(*, config_root=None, data_root=None):
    """Finish a final-step interruption from the independent updater process."""
    config = Path(os.path.abspath(config_root if config_root is not None else
                                  os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")))
    data = Path(os.path.abspath(data_root if data_root is not None else updater.data_directory()))
    try:
        updater._safe_dir(data)
        _validate_parent_chain(data / ".updater.lock")
        _validate_parent_chain(store.store_path(config))
        _validate_parent_chain(data / RECOVERY_NAME)
        runtime_path = Path.home() / ".config/ai-agents-sandbox/matuto-runtime/run.lock"
        _validate_parent_chain(runtime_path)
        if os.path.lexists(runtime_path):
            _validate_tree(runtime_path)
        update_lock = updater._updater_lock(data)
    except (updater.UpdateError, RemovalError) as error:
        raise RemovalError(f"Removal finalizer refused: {error}") from None
    try:
        recovery_exists = _valid_recovery_helper(data / RECOVERY_NAME)
    except (OSError, RemovalError) as error:
        os.close(update_lock)
        if isinstance(error, RemovalError):
            raise
        raise RemovalError(f"Removal finalizer refused: {error}") from None
    if _progress(data) != "final-step" and not recovery_exists:
        os.close(update_lock)
        raise RemovalError("Removal finalizer requires a recorded final-step interruption")
    runtime_lock = None
    opened = None
    try:
        from matuto.matuto_runtime import runtime_lease
        runtime_lock = runtime_lease(exclusive=True)
        opened = store.open_store(config, exclusive=True, allow_newer=True)
        reasons = blockers(config_root=config, data_root=data, opened_store=opened,
                           updater_locked=True)
        if reasons:
            raise RemovalError("Removal finalizer refused: " + "; ".join(reasons))
        _validate_plan(config, data, opened.connection())
        _mark_progress(data, "final-step")
        _ensure_recovery_helper(data, config)
        manager_unit = _unit_path(config, updater.MANAGER_UNIT)
        if os.path.lexists(manager_unit):
            _systemctl(["disable", updater.MANAGER_UNIT])
            _systemctl(["stop", updater.MANAGER_UNIT])
            _unlink_owned(manager_unit)
        _systemctl(["daemon-reload"])
        _finish_release_tree(updater.releases_directory(data))
        _remove_tree(data / "updater", allowed_links={"current"})
        _unlink_owned(data / ".removal-state")
        _unlink_owned(data / RECOVERY_NAME)
        return True
    except RemovalError as error:
        recovery = data / RECOVERY_NAME
        if os.path.lexists(recovery):
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(recovery))}"
            raise RemovalError(f"Removal finalizer stopped; rerun {command}: {error}") from None
        raise
    except (InspectionError, OSError, store.StoreError, updater.UpdateError, RuntimeError) as error:
        recovery = data / RECOVERY_NAME
        command = (f"{shlex.quote(sys.executable)} {shlex.quote(str(recovery))}"
                   if os.path.lexists(recovery) else
                   f"{shlex.quote(sys.executable)} {shlex.quote(str(data / 'releases/current/agents/matuto/cli.py'))} remove")
        raise RemovalError(f"Removal finalizer stopped; rerun {command}: {error}") from None
    finally:
        if opened is not None:
            opened.close()
        if runtime_lock is not None:
            os.close(runtime_lock)
        os.close(update_lock)


def _progress(data):
    try:
        path = data / ".removal-state"
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            return "unknown"
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return "unknown"


def _valid_recovery_helper(path):
    parent_fd = _open_parent_fd(path)
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                         dir_fd=parent_fd)
        except FileNotFoundError:
            return False
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o700):
            os.close(fd)
            raise RemovalError(f"refusing unsafe recovery helper: {path}")
        with os.fdopen(fd, "rb") as source:
            with zipfile.ZipFile(source) as archive:
                return "__main__.py" in archive.namelist() and archive.testzip() is None
    except (OSError, zipfile.BadZipFile):
        return False
    finally:
        os.close(parent_fd)


def _ensure_recovery_helper(data, config):
    path = data / RECOVERY_NAME
    if _valid_recovery_helper(path):
        return
    if os.path.lexists(path):
        _unlink_owned(path)
    source = Path(__file__).resolve().parent.parent
    package = source / "matuto"
    runtime = source / "matuto_runtime.py"
    if not package.is_dir() or not runtime.is_file():
        raise RemovalError("cannot create independent removal recovery helper")
    parent_fd = _open_parent_fd(path)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o700, dir_fd=parent_fd)
        with os.fdopen(fd, "w+b") as output:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("__main__.py", RECOVERY_ENTRYPOINT.format(
                    config=str(config), data=str(data)))
                for source_file in sorted(package.rglob("*")):
                    if source_file.is_file() and not source_file.is_symlink():
                        archive.write(source_file, source_file.relative_to(source).as_posix())
                archive.write(runtime, "matuto_runtime.py")
            output.flush()
            os.fsync(output.fileno())
        updater._sync_directory(path.parent)
    except FileExistsError:
        if not _valid_recovery_helper(path):
            raise RemovalError("cannot create independent removal recovery helper")
    except Exception as error:
        if os.path.lexists(path):
            _unlink_owned(path)
        raise RemovalError(f"cannot create independent removal recovery helper: {error}") from None
    finally:
        os.close(parent_fd)
    if not _valid_recovery_helper(path):
        _unlink_owned(path)
        raise RemovalError("independent removal recovery helper failed verification")


def _clear_runtime_records(opened):
    connection = opened.connection()
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM voice_serving_parts")
        connection.execute("DELETE FROM voice_serving")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _validate_parent_chain(path):
    path = Path(path)
    for ancestor in reversed(path.parent.parents):
        if not os.path.lexists(ancestor):
            continue
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RemovalError(f"refusing unsafe parent path: {ancestor}")
    if os.path.lexists(path.parent):
        info = path.parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RemovalError(f"refusing unsafe parent path: {path.parent}")


def _validate_plan(config, data, connection):
    _reject_kept_overlaps(config, data, connection)
    paths = [store.store_path(config), data / "releases", data / "updater",
             _unit_path(config, updater.MANAGER_UNIT), data / ".removal-state",
             data / RECOVERY_NAME]
    paths.extend(_first_step_files(config, data))
    for path in paths:
        _validate_parent_chain(path)
    for path in (store.store_path(config), data / "releases", data / "updater",
                 _unit_path(config, updater.MANAGER_UNIT), data / ".removal-state",
                 data / RECOVERY_NAME):
        if os.path.lexists(path):
            _validate_tree(path, allowed_links={"current"} if path.name in ("releases", "updater") else set())
    command = Path.home() / ".local/bin/matuto-manager"
    if os.path.lexists(command) and command.read_bytes() != bootstrap._manager_command(data).encode():
        raise RemovalError(f"refusing to remove a matuto-manager command not installed by this release: {command}")
    for path in _first_step_files(config, data):
        if os.path.lexists(path):
            if path == data / "releases":
                _validate_tree(path, allowed_links={"current"})
            else:
                _validate_tree(path)


def _validate_tree(path, *, allowed_links=frozenset()):
    path = Path(path)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        target = (path.parent / os.readlink(path)).resolve(strict=False)
        if (path.name in allowed_links and info.st_uid == os.getuid()
                and target.parent == path.parent.resolve()):
            return
        raise RemovalError(f"refusing unsafe linked removal path: {path}")
    if info.st_uid != os.getuid():
        raise RemovalError(f"refusing removal path owned by another account: {path}")
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise RemovalError(f"refusing removal path with multiple links: {path}")
        return
    if not stat.S_ISDIR(info.st_mode):
        raise RemovalError(f"refusing non-file removal path: {path}")
    for root, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            child = Path(root) / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = (child.parent / os.readlink(child)).resolve(strict=False)
                expected_parent = path / "versions" if path.name == "updater" else path
                if (child.name in allowed_links and child.parent == path
                        and info.st_uid == os.getuid() and target.parent == expected_parent.resolve()):
                    continue
                raise RemovalError(f"refusing linked removal path: {child}")
            if info.st_uid != os.getuid():
                raise RemovalError(f"refusing removal path owned by another account: {child}")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise RemovalError(f"refusing removal path with multiple links: {child}")
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise RemovalError(f"refusing non-file removal path: {child}")


def _reject_kept_overlaps(config, data, connection):
    removed = [*_first_step_files(config, data), _unit_path(config, updater.MANAGER_UNIT),
               Path(data) / "updater", Path(data) / RECOVERY_NAME]
    for label, kept in _kept_paths(config, connection):
        if kept is None:
            continue
        for target in removed:
            if _paths_overlap(target, kept):
                raise RemovalError(
                    f"refusing to remove {target}; it overlaps kept {label} at {kept}")


def _paths_overlap(left, right):
    def variants(path):
        path = Path(os.path.abspath(path))
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError):
            raise RemovalError(f"cannot safely resolve removal path: {path}") from None
        return path, resolved

    for first in variants(left):
        for second in variants(right):
            if first == second or first in second.parents or second in first.parents:
                return True
    return False


def _first_step_files(config, data):
    config_root = Path(config) if config is not None else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    unit_dir = config_root / "systemd/user"
    applications = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"
    paths = [store.store_directory(config) / "router-presets.ini",
             store.store_directory(config) / "voice-presets.ini"]
    # Keep the manager unit until the final step; only delete Matuto-owned units.
    if unit_dir.exists():
        paths.extend(unit_dir / name for name in os.listdir(unit_dir)
                     if name in (serving_state.ROUTER_UNIT, serving_state.VOICE_ROUTER_UNIT)
                     or name.startswith("matuto-app-voice-part-") and name.endswith(".service"))
    paths.extend(applications / name for name in
                 ("matuto.desktop", "repair-matuto.desktop", "update-matuto.desktop"))
    paths.extend((Path.home() / ".local/bin/matuto-manager", data / "releases"))
    return paths


def _remove_first_step(config, data):
    unit_dir = (Path(config) if config is not None else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))) / "systemd/user"
    for path in _first_step_files(config, data):
        if path == data / "releases":
            _trim_release_tree(path)
        elif path == Path.home() / ".local/bin/matuto-manager":
            if os.path.lexists(path) and path.read_bytes() != bootstrap._manager_command(data).encode():
                raise RemovalError(f"refusing to remove a matuto-manager command not installed by this release: {path}")
            _unlink_owned(path)
        else:
            _remove_tree(path)
    if unit_dir.exists():
        _systemctl(["daemon-reload"])


def _trim_release_tree(root):
    if not root.exists():
        return
    if root.is_symlink():
        raise RemovalError(f"refusing linked release directory: {root}")
    for version in root.iterdir():
        if version.name == "current":
            continue
        if version.is_symlink():
            raise RemovalError(f"refusing linked release path: {version}")
        agents = version / "agents"
        if agents.exists():
            for child in agents.iterdir():
                if child.name == "matuto" or child.name == "matuto_runtime.py":
                    continue
                _remove_tree(child)
        for child in version.iterdir():
            if child.name != "agents":
                _remove_tree(child)
        if agents.exists():
            for child in agents.iterdir():
                if child.name not in MANAGER_FILES:
                    _remove_tree(child)


def _finish_release_tree(root):
    if root.is_symlink():
        raise RemovalError(f"refusing linked release directory: {root}")
    _remove_tree(root, allowed_links={"current"})


def _kept_paths(config, connection=None):
    config_root = Path(config) if config is not None else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    manager_dir = store.store_directory(config)
    items = [
        ("Store (including sessions and job history)", store.store_path(config)),
        ("Pre-update snapshot", snapshot.snapshot_path(store.store_path(config))),
        ("Login", manager_dir / "login.json"),
        ("Router API key", config_root / "ai-agents-sandbox/pi-local/api-key"),
        ("Build source", data_home / "ai-agents-sandbox/build-source"),
        ("Build staging", data_home / "ai-agents-sandbox/build-staging"),
        ("Runtime builds", data_home / "ai-agents-sandbox/builds"),
    ]
    if connection is not None:
        try:
            locations = [row[0] for row in connection.execute(
                "SELECT path FROM artifact_locations ORDER BY path")]
            items.extend(("Model weights and partial downloads", Path(path)) for path in locations)
            items.extend(("Partial download staging", Path(path) / ".matuto-downloads")
                         for path in locations)
            items.extend(("Registered llama.cpp build", Path(row[0])) for row in connection.execute(
                "SELECT location FROM builds ORDER BY location"))
            items.extend(("Installed speech executable", Path(row[0])) for row in connection.execute(
                "SELECT path FROM speech_executables ORDER BY path"))
        except Exception:
            items.append(("Configured weights and runtime paths", None))
    else:
        items.append(("Configured weights and runtime paths", None))
    return items


def _mark_progress(data, stage):
    path = Path(data) / ".removal-state"
    temporary = ".removal-state.tmp"
    parent_fd = _open_parent_fd(path)
    try:
        for name in (path.name, temporary):
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1):
                raise RemovalError(f"refusing unsafe removal progress file: {path.parent / name}")
            if name == temporary:
                os.unlink(name, dir_fd=parent_fd)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(descriptor, "wb") as progress:
                progress.write((stage + "\n").encode())
                progress.flush()
                os.fsync(progress.fileno())
            os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(parent_fd)


def _unit_path(config, name):
    root = Path(config) if config is not None else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "systemd/user" / name


def _open_parent_fd(path):
    path = Path(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as error:
        os.close(descriptor)
        raise RemovalError(f"refusing unsafe parent path: {path.parent}: {error}") from None


def _unlink_owned(path):
    path = Path(path)
    try:
        parent_fd = _open_parent_fd(path)
    except FileNotFoundError:
        return
    try:
        try:
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise RemovalError(f"refusing unsafe removal path: {path}")
        if info.st_nlink != 1:
            raise RemovalError(f"refusing removal path with multiple links: {path}")
        os.unlink(path.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _remove_tree(path, *, allowed_links=frozenset()):
    path = Path(path)
    try:
        _validate_tree(path, allowed_links=allowed_links)
        parent_fd = _open_parent_fd(path)
    except FileNotFoundError:
        return
    try:
        try:
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if info.st_uid != os.getuid():
            raise RemovalError(f"refusing removal path owned by another account: {path}")
        if stat.S_ISDIR(info.st_mode):
            if not shutil.rmtree.avoids_symlink_attacks:
                raise RemovalError("safe recursive removal is unavailable on this platform")
            shutil.rmtree(path.name, dir_fd=parent_fd)
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise RemovalError(f"refusing removal path with multiple links: {path}")
            os.unlink(path.name, dir_fd=parent_fd)
        else:
            raise RemovalError(f"refusing unsafe removal path: {path}")
    except OSError as error:
        raise RemovalError(f"cannot safely remove {path}: {error}") from None
    finally:
        os.close(parent_fd)


def _systemctl(arguments):
    try:
        result = subprocess.run(["systemctl", "--user", *arguments], capture_output=True,
                                text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RemovalError(f"systemd did not answer for {' '.join(arguments)}: {error}") from None
    if result.returncode:
        raise RemovalError(f"systemctl --user {' '.join(arguments)} failed: {result.stdout.strip()}")
