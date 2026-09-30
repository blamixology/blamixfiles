"""Filesystem locations used by BlamixFiles.

Portable by default: data lives in a `data` folder next to the executable (or next
to run.py in a source checkout). Falls back to the per-user folder when that isn't
writable or when installed with pip/pipx. BLAMIXFILES_HOME overrides everything.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "BlamixFiles"
INSTALLED_MARKER = "installed.marker"

_DATA_DIR: Path | None = None


def app_dir() -> Path | None:
    """The folder holding the executable (or the .app on macOS), or the project root
    when running from a source checkout. None when installed as a package."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if (exe.parent / INSTALLED_MARKER).exists():
            return None
        for parent in exe.parents:           # .../BlamixFiles.app/Contents/MacOS/BlamixFiles
            if parent.suffix == ".app":
                if parent.parent.name == "Applications":
                    return None
                return parent.parent
        return exe.parent
    root = Path(__file__).resolve().parent.parent
    return root if (root / "run.py").exists() else None


def user_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME.lower()


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def data_dir() -> Path:
    global _DATA_DIR
    override = os.environ.get("BLAMIXFILES_HOME")
    if override:
        base = Path(override)
        base.mkdir(parents=True, exist_ok=True)
        return base
    if _DATA_DIR is not None:
        return _DATA_DIR
    portable = app_dir()
    base = portable / "data" if portable else user_dir()
    if not (portable and _writable(base)):
        base = user_dir()
        base.mkdir(parents=True, exist_ok=True)
    _DATA_DIR = base
    return base


def vault_path() -> Path:
    return data_dir() / "vault.bfv"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def known_hosts_path() -> Path:
    return data_dir() / "known_hosts"


def assets_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "blamixfiles" / "assets"
    return Path(__file__).resolve().parent / "assets"
