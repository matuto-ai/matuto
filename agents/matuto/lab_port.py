"""The loopback lab port setting and its admission lease."""

import datetime

from matuto import admission, runner, store


def saved_port(connection):
    row = connection.execute("SELECT port FROM lab_port WHERE setting_id = 1").fetchone()
    return None if row is None else row["port"]


def save_port(opened, port):
    """Save only when no run holds the lease, serialized with runner admission."""
    connection = opened.connection()
    running = runner.Runner(opened_store=opened)
    observed = {work.work_id: work for work in running.works()
                if work.kind in admission.RUN_KINDS}
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute(
            "SELECT run_id, procedure, record_label, unit_name FROM runs"
            " WHERE outcome IS NULL ORDER BY started_at, run_id").fetchall()
        if rows:
            row = rows[0]
            work = observed.get(row["run_id"])
            label = f"{row['procedure']} run for {row['record_label']} ({row['run_id']})"
            if work is not None and work.state == runner.UNKNOWN:
                raise store.StoreError(
                    f"The state of the {label} is unknown; the lab port cannot change. "
                    "Check or stop it in Runs & jobs.")
            raise store.StoreError(
                f"The {label} holds the lab port. Finish it or stop it in Runs & jobs.")
        connection.execute(
            "INSERT INTO lab_port (setting_id, port, saved_at) VALUES (1, ?, ?)"
            " ON CONFLICT(setting_id) DO UPDATE SET port = excluded.port, saved_at = excluded.saved_at",
            (port, datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")))
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return port
