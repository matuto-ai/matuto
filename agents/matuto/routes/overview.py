"""The record-derived setup state shown on Overview."""

from matuto import registry, storage, store
from matuto.status import storage as storage_status

PATH = "/api/overview"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("Overview data is read-only.")
    opened = store.open_store()
    try:
        connection = opened.connection()
        saved_storage = storage.model_storage(connection)
        acquisition = connection.execute(
            "SELECT r.name FROM acquisition_records AS r"
            " WHERE EXISTS (SELECT 1 FROM acquisition_files AS f"
            "             WHERE f.acquisition_id = r.acquisition_id)"
            " AND NOT EXISTS (SELECT 1 FROM acquisition_files AS f"
            "                 WHERE f.acquisition_id = r.acquisition_id"
            "                   AND length(f.computed_sha256) <> 64)"
            " ORDER BY r.name, r.acquisition_id").fetchall()
        runtime = connection.execute(
            "SELECT s.build_id, b.label, s.card_id, c.name AS card"
            " FROM runtime_selection AS s"
            " LEFT JOIN builds AS b ON b.build_id = s.build_id"
            " LEFT JOIN cards AS c ON c.card_id = s.card_id WHERE s.selection_id = 1").fetchone()
        profile = connection.execute(
            "SELECT name FROM model_profiles WHERE kind = 'language'"
            " ORDER BY name, profile_id LIMIT 1").fetchone()
        tasks, configured = [], []

        def record(label, value, href, destination):
            item = {"label": label, "value": value, "href": href,
                    "destination": destination}
            (configured if value is not None else tasks).append(item)

        record("Set model storage", saved_storage, "#/settings/storage-artifacts",
               "Settings › Storage & artifacts")
        record("Get model files", ", ".join(row["name"] for row in acquisition) if acquisition else None,
               "#/model-files/acquisition", "Model files wizard")
        if runtime:
            runtime_value = f"{runtime['label'] or runtime['build_id']} on {runtime['card'] or runtime['card_id']}"
            runtime_href, runtime_destination = "#/runtime", "Runtime"
        elif registry.listing(connection):
            runtime_value = None
            runtime_href, runtime_destination = "#/runtime/builds", "Runtime › llama.cpp builds"
        else:
            runtime_value = None
            runtime_href, runtime_destination = "#/runtime/install", "Runtime › Install a version"
        record("Prepare the runtime", runtime_value, runtime_href, runtime_destination)
        record("Create or import a language profile", profile["name"] if profile else None,
               "#/profiles/language", "Profiles › Language profiles")

        repair = storage_status.report(connection)
        if repair:
            affected = _affected_records(connection, repair["path"])
            names = ", ".join(affected) if affected else "none"
            repair["detail"] = (f"{repair['path']} {repair['reason']}. "
                                f"Affected records: {names}.")
            repair["affected"] = affected
        return 200, {"tasks": tasks if tasks else [],
                     "configured": configured if tasks else [],
                     "storageRepair": repair}, "application/json"
    finally:
        opened.close()


def _affected_records(connection, path):
    location = connection.execute(
        "SELECT location_id FROM artifact_locations WHERE path = ?", (path,)).fetchone()
    if location is None:
        return []
    names = {row["name"] for row in connection.execute(
        "SELECT name FROM acquisition_records WHERE location_id = ?", (location["location_id"],))}
    names.update(row["name"] for row in connection.execute(
        "SELECT DISTINCT p.name FROM model_profiles AS p"
        " JOIN profile_artifacts AS a ON a.profile_id = p.profile_id"
        " JOIN acquisition_records AS r ON r.acquisition_id = a.acquisition_id"
        " WHERE r.location_id = ?", (location["location_id"],)))
    names.update(row["name"] for row in connection.execute(
        "SELECT DISTINCT b.name FROM bundles AS b"
        " JOIN bundle_members AS m ON m.bundle_id = b.bundle_id"
        " JOIN profile_artifacts AS a ON a.profile_id = m.profile_id"
        " JOIN acquisition_records AS r ON r.acquisition_id = a.acquisition_id"
        " WHERE r.location_id = ?", (location["location_id"],)))
    return sorted(names)
