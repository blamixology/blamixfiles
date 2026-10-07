"""Diagnostics for problem reports: versions, system, install kind, settings (no secrets), the latest
transfers, and crash reports.

Nothing is ever sent anywhere: the app copies the text to the clipboard or saves it to a file, and the
user decides where it goes. Crash reports are written to <data>/crashes/ by an exception hook; on the
next start the app offers to show the newest one.
"""
from __future__ import annotations

import os
import platform
import sys
import time
import traceback
from pathlib import Path

from . import __version__

# settings keys whose values are never put in a report (paths can contain user names, but are useful;
# these are the ones that are plain noise or could be personal)
_SKIP_SETTINGS = {"window_geometry", "window_state", "open_tabs"}
_MAX_CRASHES = 20


def crash_dir() -> Path:
    from .paths import data_dir
    d = data_dir() / "crashes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _qt_version() -> str:
    try:
        import PySide6
        from PySide6 import QtCore
        return f"PySide6 {PySide6.__version__}, Qt {QtCore.qVersion()}"
    except Exception:  # noqa: BLE001
        return "no Qt (command line / terminal UI)"


def report(settings: dict | None = None, extra: str = "") -> str:
    from . import updater
    from .core import engine as E
    from .paths import data_dir, transfer_log_path
    lines = [
        f"BlamixFiles {__version__} ({updater.install_kind()})",
        f"System: {platform.platform()} ({platform.machine()})",
        f"Python: {sys.version.split()[0]} {platform.python_implementation()}",
        f"Qt: {_qt_version()}",
        f"Data folder: {data_dir()}",
        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
    ]
    if settings:
        lines.append("")
        lines.append("Settings:")
        for k in sorted(settings):
            if k not in _SKIP_SETTINGS:
                lines.append(f"  {k} = {settings[k]!r}")
    log = E.read_log(transfer_log_path(), 30)
    if log:
        lines.append("")
        lines.append("Latest transfers (time, status, direction, site, from, to, bytes, seconds, error):")
        lines += ["  " + ln.replace("\t", " | ") for ln in log]
    crashes = list_crashes()
    if crashes:
        lines.append("")
        lines.append(f"Crash reports: {len(crashes)} (newest: {crashes[0].name})")
    if extra:
        lines += ["", extra]
    return "\n".join(lines) + "\n"


def write_crash(exc_type, exc, tb) -> Path | None:
    try:
        d = crash_dir()
        name = time.strftime("crash-%Y%m%d-%H%M%S") + f"-{os.getpid()}.txt"
        text = report(extra="Crash:\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
        path = d / name
        path.write_text(text, encoding="utf-8")
        for old in list_crashes()[_MAX_CRASHES:]:
            old.unlink(missing_ok=True)
        return path
    except Exception:  # noqa: BLE001  (a crash handler must never crash)
        return None


def install_crash_hook() -> None:
    """Record unhandled exceptions (main thread and worker threads) as crash reports, then carry on
    as Python normally would."""
    import threading
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            write_crash(exc_type, exc, tb)
        previous(exc_type, exc, tb)
    sys.excepthook = hook
    prev_thread = threading.excepthook

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            write_crash(args.exc_type, args.exc_value, args.exc_traceback)
        prev_thread(args)
    threading.excepthook = thread_hook


def list_crashes() -> list[Path]:
    try:
        return sorted(crash_dir().glob("crash-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def new_crashes(since: float) -> list[Path]:
    return [p for p in list_crashes() if p.stat().st_mtime > since]
