"""Runtime paths.

`FLET_ASSETS_DIR` is set by Flet in production builds. During `flet run` and
pytest there is no such variable, so fall back to `src/assets`.
"""

from __future__ import annotations

import os
from pathlib import Path


def assets_dir() -> Path:
    override = os.environ.get("FLET_ASSETS_DIR")
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "assets"
        if (candidate / "kokoro").is_dir():
            return candidate
    return Path.cwd() / "assets"


def kokoro_dir() -> Path:
    return assets_dir() / "kokoro"


def model_path() -> Path:
    return kokoro_dir() / "kokoro-v1.0.int8.onnx"


def voices_path() -> Path:
    return kokoro_dir() / "voices-v1.0.bin"


def g2p_dir() -> Path:
    return assets_dir() / "g2p"


def data_dir() -> Path:
    """Durable app data: database, imported books."""
    override = os.environ.get("FLET_APP_STORAGE_DATA")
    path = Path(override) if override else Path.home() / ".local/share/soundbook"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    """Regenerable cache: rendered page audio."""
    override = os.environ.get("FLET_APP_STORAGE_CACHE")
    path = Path(override) / "soundbook" if override else Path.home() / ".cache/soundbook"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "soundbook.sqlite3"