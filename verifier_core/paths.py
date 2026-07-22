"""Per-user application directories, cross-platform.

Windows keeps the historical locations (%APPDATA% / %LOCALAPPDATA%) so
existing installs keep their settings and extracted databases; macOS and
Linux follow their platform conventions (Application Support, XDG dirs).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "ALR Quote Verifier"


def config_dir() -> Path:
    """Small per-user config (settings JSON, stored API key)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR_NAME


def data_dir() -> Path:
    """Large machine-local data (extracted reference databases)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME
