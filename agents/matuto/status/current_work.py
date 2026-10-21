"""The one active job or run, read without settling or changing it."""

import subprocess
from urllib.parse import quote

from matuto import admission

NAME = "current_work"
TYPE = "current-work"
ACTIVE = frozenset(("active", "activating", "deactivating", "reloading"))


def report(connection):
    """Return the newest recorded work only when its systemd unit is active."""
    row = connection.execute(
        "SELECT work_type, work_id, kind, label, unit_name FROM ("
        " SELECT 'job' AS work_type, job_id AS work_id, kind, 'Runtime build' AS label,"
        " unit_name, started_at FROM jobs WHERE outcome IS NULL"
        " UNION ALL"
        " SELECT 'run' AS work_type, run_id AS work_id, procedure AS kind,"
        " record_label AS label, unit_name, started_at FROM runs WHERE outcome IS NULL"
        ") ORDER BY started_at DESC LIMIT 1").fetchone()
    if row is None:
        return {"name": NAME, "type": TYPE, "label": "Nothing running", "href": "#/runs"}
    try:
        result = subprocess.run(["systemctl", "--user", "show", row["unit_name"],
                                 "--property=LoadState", "--property=ActiveState",
                                 "--property=SubState"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, check=False, timeout=0.75)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("the current work status is unknown") from None
    if result.returncode != 0:
        if f"Unit {row['unit_name']} could not be found" in result.stdout:
            return {"name": NAME, "type": TYPE, "label": "Nothing running", "href": "#/runs"}
        raise RuntimeError("the current work status is unknown")
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    load, active, sub = (properties.get(key, "") for key in ("LoadState", "ActiveState", "SubState"))
    if load == "not-found":
        return {"name": NAME, "type": TYPE, "label": "Nothing running", "href": "#/runs"}
    if not load or not active or not sub:
        raise RuntimeError("the current work status is unknown")
    if active in ("failed", "inactive") or (active == "active" and sub == "exited"):
        return {"name": NAME, "type": TYPE, "label": "Nothing running", "href": "#/runs"}
    if active not in ACTIVE:
        raise RuntimeError("the current work status is unknown")
    if row["work_type"] == "job":
        label = "Downloading model files" if row["kind"] == admission.DOWNLOAD else "Building runtime"
    else:
        label = f"{row['kind'].replace('-', ' ').title()}: {row['label']}"
    return {"name": NAME, "type": TYPE, "label": label,
            "card_holding": row["work_type"] == "run" and row["kind"] not in admission.NO_CARD_RUNS,
            "href": "#/runs?work=" + quote(row["work_id"], safe="")}
