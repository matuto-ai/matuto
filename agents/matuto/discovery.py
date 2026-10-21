"""Find the package's per-item files by name. See README.md for the conventions."""

import importlib.util
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent


def item_names(location, root=None):
    """Names of the item files directly inside one of the package's locations."""
    return sorted(path.stem for path in ((root or PACKAGE_ROOT) / location).glob("*.py")
                  if path.stem != "__init__")


def load_item(location, name, root=None):
    """Load one item file by name from a location; it is not part of a package itself."""
    spec = importlib.util.spec_from_file_location(
        f"matuto_{location}_{name}", (root or PACKAGE_ROOT) / location / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module