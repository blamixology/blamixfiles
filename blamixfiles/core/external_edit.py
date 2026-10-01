"""Edit a remote file in another program (VS Code, Notepad++, Photoshop, …).

The file is downloaded to a private folder, opened in the other program, and
watched: each time it's saved there (and has stopped changing for a moment), the
app offers to upload it again. Before uploading, the server copy is checked: if it
changed since it was downloaded, the user decides (overwrite or keep theirs).

Qt-free bookkeeping; the GUI does the asking and the transfers.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(eq=False)
class ExternalEdit:
    site: object                      # models.Site
    session: object                   # the UI session that downloaded it (uploads go through it)
    remote_path: str
    local_path: str
    remote_mtime: float = 0.0         # the server copy we started from
    remote_size: int = 0
    ask: bool = True                  # ask before each upload
    uploads: int = 0
    _sig: tuple[int, int] = (0, 0)    # local (mtime_ns, size) last uploaded / downloaded
    _pending: tuple[tuple[int, int], float] | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return os.path.basename(self.local_path)


def local_sig(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


class ExternalEdits:
    def __init__(self, root: Path, settle: float = 1.0):
        self.root = Path(root)
        self.settle = settle
        self.edits: list[ExternalEdit] = []

    def local_path(self, site_id: str, remote_path: str) -> str:
        """data/external-edit/<site>/<hash of the remote path>/<file name>: the real name
        (so the other program picks the right syntax/app), no clashes between folders."""
        h = hashlib.sha1(remote_path.encode("utf-8")).hexdigest()[:12]
        d = self.root / (site_id or "quick") / h
        d.mkdir(parents=True, exist_ok=True)
        name = remote_path.rstrip("/").rsplit("/", 1)[-1] or "file"
        from .engine import safe_local_name
        return str(d / safe_local_name(name))

    def find(self, site_id: str, remote_path: str) -> ExternalEdit | None:
        for e in self.edits:
            if getattr(e.site, "id", "") == site_id and e.remote_path == remote_path:
                return e
        return None

    def add(self, edit: ExternalEdit) -> ExternalEdit:
        old = self.find(getattr(edit.site, "id", ""), edit.remote_path)
        if old is not None:
            self.edits.remove(old)
        edit._sig = local_sig(edit.local_path) or (0, 0)
        self.edits.append(edit)
        return edit

    def remove(self, edit: ExternalEdit, delete_file: bool = True) -> None:
        if edit in self.edits:
            self.edits.remove(edit)
        if delete_file:
            shutil.rmtree(os.path.dirname(edit.local_path), ignore_errors=True)

    def forget_session(self, session) -> list[ExternalEdit]:
        gone = [e for e in self.edits if e.session is session]
        for e in gone:
            self.edits.remove(e)
        return gone

    def poll(self, now: float | None = None) -> list[ExternalEdit]:
        """Files saved in the other program since the last upload, once they've settled."""
        now = time.monotonic() if now is None else now
        ready = []
        for e in self.edits:
            sig = local_sig(e.local_path)
            if sig is None or sig == e._sig:
                e._pending = None
                continue
            if e._pending is None or e._pending[0] != sig:
                e._pending = (sig, now)
            elif now - e._pending[1] >= self.settle:
                ready.append(e)
        return ready

    def accepted(self, edit: ExternalEdit) -> None:
        """This version is handled (uploaded, or the user said not now): don't offer it again."""
        edit._sig = local_sig(edit.local_path) or edit._sig
        edit._pending = None

    def uploaded(self, edit: ExternalEdit, remote_mtime: float, remote_size: int) -> None:
        self.accepted(edit)
        edit.remote_mtime, edit.remote_size = remote_mtime, remote_size
        edit.uploads += 1

    def remote_changed(self, edit: ExternalEdit, mtime: float, size: int) -> bool:
        """Did someone else change the server copy since we downloaded/uploaded it?"""
        if size != edit.remote_size:
            return True
        return bool(edit.remote_mtime and mtime and abs(mtime - edit.remote_mtime) > 1)

    def clean_old(self, days: float = 7.0) -> None:
        """Leftovers from earlier runs (nothing watches them any more)."""
        if not self.root.is_dir():
            return
        limit = time.time() - days * 86400
        for site_dir in self.root.iterdir():
            for d in site_dir.iterdir() if site_dir.is_dir() else []:
                try:
                    if d.stat().st_mtime < limit:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
