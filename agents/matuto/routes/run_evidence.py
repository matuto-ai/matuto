"""Download one retained run and all its evidence as a named archive."""

from urllib.parse import quote

from matuto import store

PATH = "/api/run-evidence"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        return 405, {"error": "Run evidence is read-only."}
    run_id = (request.query.get("id") or [""])[0]
    if not run_id:
        return 400, {"error": "a run ID is required"}
    opened = None
    try:
        opened = store.open_store()
        archive = store.run_archive(opened, run_id)
        if archive is None:
            return 404, {"error": f"Run {run_id!r} has no retained evidence."}
        filename = quote(f"matuto-{run_id}-evidence.zip", safe="")
        return 200, archive, "application/zip", {
            "Content-Disposition": f'attachment; filename="{filename}"'}
    except (OSError, store.StoreError) as error:
        return 503, {"error": str(error)}
    finally:
        if opened is not None:
            opened.close()
