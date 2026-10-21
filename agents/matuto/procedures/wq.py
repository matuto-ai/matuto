"""Worker qualification for one stored language profile."""

import importlib.util
import json
from functools import lru_cache
import os
from pathlib import Path
import subprocess
import sys

from matuto import store, updater
from matuto.procedure_support import setup


class LimitError(store.StoreError):
    def __init__(self, field, message):
        self.field = field
        super().__init__(message)


def release_root():
    module = Path(__file__).resolve()
    return next((parent for parent in module.parents
                 if (parent / "wq.py").is_file() and (parent / "wq.py").resolve() != module),
                module.parents[3])


def revision():
    try:
        return updater.installed_version()
    except Exception:
        root = release_root()
        try:
            return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"],
                                           text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return "unavailable"


def dirty_checkout():
    root = release_root()
    if not (root / ".git").exists():
        return False
    try:
        return bool(subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain"],
            text=True, stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError):
        return True


@lru_cache(maxsize=1)
def _driver():
    root = release_root()
    driver_spec = importlib.util.spec_from_file_location("matuto_wq_driver", root / "wq.py")
    if driver_spec is None or driver_spec.loader is None:
        raise store.StoreError("the WQ driver is unavailable")
    driver = importlib.util.module_from_spec(driver_spec)
    driver_spec.loader.exec_module(driver)
    return driver


def form_defaults(opened, record_id):
    record = store.read_record(opened, "language", record_id)
    card = opened.connection().execute(
        "SELECT vram_ceiling_bytes FROM cards WHERE card_id = ?", (record["card_id"],)).fetchone()
    if card is None:
        raise store.StoreError(f"the card for language profile {record_id!r} is unavailable")
    limits = _driver().validate_limits()
    limits["vram_mb"] = card["vram_ceiling_bytes"] / 1_000_000
    return limits


def prepare(opened, target, limits=None):
    from matuto import matuto_runtime

    root = release_root()
    driver = _driver()
    devices = matuto_runtime.pci_devices()
    if devices.get("state") != "completed":
        raise store.StoreError("GPU detection failed; WQ cannot verify the stored profile")
    spec = store.launch_spec(opened, "language", target.id,
                             detected_gpus=devices.get("devices", ()))
    overrides = {} if limits is None else limits
    try:
        spec["wq_limits"] = driver.validate_limits(overrides)
    except ValueError as error:
        if isinstance(error, driver.InvalidLimitError):
            raise LimitError(error.field, str(error)) from None
        raise store.StoreError(str(error)) from None
    if "vram_mb" not in overrides:
        spec["wq_limits"]["vram_mb"] = spec["record"]["vram_ceiling_bytes"] / 1_000_000
    return spec


def run(opened, record, spec=None):
    if record["kind"] != "language":
        print("matuto: worker qualification requires a stored language profile", file=sys.stderr)
        return 2
    try:
        spec = prepare(opened, record) if spec is None else spec
        if spec["record"]["id"] != record["profile_id"] or spec["record"]["kind"] != "language":
            raise store.StoreError("the pinned WQ launch spec does not match the stored profile")
    except (store.StoreError, store.RecordRefusal) as error:
        print(f"matuto: {error}", file=sys.stderr)
        return 1

    pinned, environment, home = setup(opened, record, spec)
    evidence = Path(os.environ["MATUTO_RUN_EVIDENCE_DIR"])
    report = evidence / "wq"
    root = release_root()
    environment.update(MATUTO_WQ_LAUNCH_SPEC=json.dumps(pinned, separators=(",", ":")),
                       MATUTO_WQ_PROCEDURE_REVISION=revision())
    command = [sys.executable, "-B", str(root / "wq.py"), "run", "--profile", record["profile_id"],
               "--port", str(pinned["lab_port"]), "--role", "", "--run-dir", str(report)]
    try:
        subprocess.run(command, env=environment, check=False)
        try:
            result = json.loads((report / "report.json").read_text())
            checks = [{"name": row["id"], "status": row["status"], "detail": row["reason"]}
                      for row in result["results"]]
            (evidence / "checks.json").write_text(json.dumps(checks) + "\n")
            counts = result["counts"]
            (evidence / "summary.txt").write_text(
                "WQ completed; evidence only, no qualification granted. " +
                ", ".join(f"{status}: {counts[status]}" for status in ("PASS", "FAIL", "ERROR", "SKIP")) + "\n")
        except (OSError, KeyError, TypeError, ValueError):
            print("matuto: WQ did not produce its evidence report", file=sys.stderr)
            return 1
        return 1 if result.get("interrupted") else 0
    finally:
        home.cleanup()
