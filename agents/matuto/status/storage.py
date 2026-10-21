"""The repair item a saved model storage that has become unavailable shows.

One status provider per file; the file name is the provider name. See README.md.
"""

from matuto import storage

TITLE = "Storage unavailable"
# Keep one repair link so every caller opens the same Storage & artifacts page.
LINK = "#/settings/storage-artifacts"


def report(connection):
    """The repair item while saved model storage is unavailable, else None.

    Model storage that was never saved is a setup task on Overview, not a
    repair, so nothing is reported for it.
    """
    path = storage.model_storage(connection)
    if path is None:
        return None
    available, reason = storage.availability(path)
    if available:
        return None
    return {"title": TITLE, "path": path, "reason": reason, "link": LINK}