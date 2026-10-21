"""Read the installed speech executables for Runtime › Speech runtimes."""

import contextlib
import sqlite3
from urllib.parse import quote

from matuto import recipes, store

PATH = "/api/speech-runtimes"

SPEECH_RECIPES = {
    ("Breeze-TTS-2.cpp", "vulkan"): recipes.BREEZE,
    ("whisper.cpp", "cpu"): recipes.WHISPER,
}


def _entry(record):
    provenance = record.get("provenance") or {}
    project = provenance.get("project") or "Unknown project"
    recipe = SPEECH_RECIPES.get((project, provenance.get("backend")))
    submodules = provenance.get("submodules") or {}
    return {
        "project": project,
        "recipe": recipe or provenance.get("recipe") or "Unknown recipe",
        "backend": {"vulkan": "Vulkan", "cpu": "CPU"}.get(
            provenance.get("backend"), provenance.get("backend") or "Unknown backend"),
        "commit": provenance.get("commit") or record.get("version") or "Unknown commit",
        "ggmlCommit": submodules.get("third_party/ggml", ""),
        "installationPath": record["root"],
        "executablePath": f'{record["root"].rstrip("/")}/{record["path"].lstrip("/")}',
        "sha256": record["sha256"],
        "installHref": (f"#/runtime/install?recipe={quote(recipe, safe='')}"
                        if recipe else "#/runtime/install"),
    }


def handle(request):
    if request.method not in ("GET", "HEAD"):
        return 405, {"error": "Speech runtimes accepts GET requests only."}
    try:
        with contextlib.closing(store.open_store()) as opened:
            entries = [_entry(record) for record in
                       store.list_speech_executables(opened.connection())]
        return 200, {"executables": entries}
    except (OSError, sqlite3.Error, store.StoreError) as error:
        return 503, {"error": str(error)}
