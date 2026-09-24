"""Per-user directories (cache, config) without extra dependencies."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .. import APP_NAME


def cache_dir() -> Path:
    override = os.environ.get("CUTSENSEI_CACHE_DIR")
    if override:
        base = Path(override)
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) \
            / APP_NAME / "Cache"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cutsensei"
    base.mkdir(parents=True, exist_ok=True)
    return base


def thumbnails_dir(fingerprint: str) -> Path:
    path = cache_dir() / "thumbnails" / fingerprint
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_path(*parts: str) -> Path:
    """Path of a file shipped inside the package (works when frozen)."""
    return Path(__file__).resolve().parent.parent.joinpath(*parts)
