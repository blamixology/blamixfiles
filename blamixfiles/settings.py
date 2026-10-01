"""Non-secret preferences (plain JSON next to the vault)."""
from __future__ import annotations

import json

from .paths import settings_path

DEFAULTS = {
    "window_geometry": "",
    "window_state": "",
    "policy": "ask",
    "workers": 4,
    "preserve_mtime": True,
    "last_local_dir": "",
    "limit_up_kb": 0,          # speed limits in KB/s, 0 = unlimited
    "limit_down_kb": 0,
    "verify_checksums": False, # compare SHA-256/MD5 after each transfer
    "show_tree": True,         # folder tree above each file list
    "restore_tabs": True,      # reopen site tabs (and their folders) on start
    "open_tabs": [],
}


class Settings:
    def __init__(self):
        self.data = dict(DEFAULTS)
        try:
            self.data.update(json.loads(settings_path().read_text("utf-8")))
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self) -> None:
        try:
            settings_path().write_text(json.dumps(self.data, indent=2), "utf-8")
        except OSError:
            pass
