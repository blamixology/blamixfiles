"""Watch a local folder and report files that changed, once they've settled.

Polling (a stat walk every `interval` seconds) instead of OS change notifications:
it works the same on Windows, macOS, Linux, network drives and inside the portable
build, with no extra dependency. The ignore list keeps it cheap on project folders
(.git, node_modules, …).

A file is reported when it's new or its size/modification time changed, and only
after it has stayed the same for `settle` seconds, so half-written files (an editor
saving, a build writing, a big copy in progress) aren't uploaded early. Deleted files
are never deleted on the server: watching only uploads.

Qt-free: the GUI and the CLI both use it.
"""
from __future__ import annotations

import fnmatch
import os
import threading
import time
from typing import Callable

DEFAULT_IGNORE = (
    ".git", ".svn", ".hg", ".idea", ".vscode", "node_modules", "__pycache__", ".venv", "venv",
    ".DS_Store", "Thumbs.db", "desktop.ini",
    "*.swp", "*.swo", "*.swx", "*~", ".#*", "#*#", "*.tmp", "*.temp", "*.part", "*.crdownload",
    "~$*", ".~lock.*",
)

Snapshot = dict[str, tuple[int, int]]           # relative path ("/"-separated) -> (size, mtime_ns)


def parse_ignore(text: str) -> tuple[str, ...]:
    """'a, b; c' or one per line -> patterns."""
    parts = [p.strip() for chunk in text.replace(";", ",").splitlines() for p in chunk.split(",")]
    return tuple(p for p in parts if p)


class FolderWatcher:
    def __init__(self, root: str, on_changes: Callable[[list[str]], None],
                 ignore: tuple[str, ...] = DEFAULT_IGNORE, interval: float = 1.0, settle: float = 1.5,
                 on_error: Callable[[str], None] | None = None):
        """on_changes(relative_paths) is called from the watcher thread."""
        self.root = os.path.abspath(root)
        self.on_changes = on_changes
        self.on_error = on_error or (lambda msg: None)
        self.ignore = tuple(ignore)
        self.interval = interval
        self.settle = settle
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.snapshot: Snapshot = {}
        self._pending: dict[str, tuple[tuple[int, int], float]] = {}   # path -> (state, first seen)
        self.reported = 0

    # ------------------------------------------------------------ public
    def start(self) -> None:
        if not os.path.isdir(self.root):
            raise FileNotFoundError(2, "No such folder", self.root)
        self.snapshot = self.scan()                  # what's there now is the baseline: not uploaded
        self._thread = threading.Thread(target=self._loop, name="folder-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ scanning
    def ignored(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in self.ignore)

    def scan(self) -> Snapshot:
        out: Snapshot = {}
        stack = [""]
        while stack:
            rel = stack.pop()
            try:
                it = os.scandir(os.path.join(self.root, rel) if rel else self.root)
            except OSError:
                continue
            with it:
                for d in it:
                    if self.ignored(d.name):
                        continue
                    r = f"{rel}/{d.name}" if rel else d.name
                    try:
                        if d.is_dir(follow_symlinks=False):
                            stack.append(r)
                        elif d.is_file():
                            st = d.stat()
                            out[r] = (st.st_size, st.st_mtime_ns)
                    except OSError:
                        continue
        return out

    def poll(self, now: float | None = None) -> list[str]:
        """One scan; returns the files that changed and have settled. (The thread calls
        this; tests call it directly.)"""
        now = time.monotonic() if now is None else now
        cur = self.scan()
        for path, state in cur.items():
            if self.snapshot.get(path) != state:
                prev = self._pending.get(path)
                if prev is None or prev[0] != state:
                    self._pending[path] = (state, now)     # new or still changing: wait again
        ready = []
        for path, (state, seen) in list(self._pending.items()):
            if path not in cur:                            # gone again (a temp file): forget it
                del self._pending[path]
            elif cur[path] == state and now - seen >= self.settle:
                ready.append(path)
                del self._pending[path]
                self.snapshot[path] = state
        for path in [p for p in self.snapshot if p not in cur]:
            del self.snapshot[path]                        # deleted locally: nothing to do remotely
        return sorted(ready)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                ready = self.poll()
                if ready:
                    self.reported += len(ready)
                    self.on_changes(ready)
            except Exception as e:  # noqa: BLE001 (keep watching)
                self.on_error(str(e))


def queue_uploads(engine, site, root: str, remote_root: str, rel_paths: list[str], join=None) -> list:
    """Queue an upload for each changed file (always overwrite, create missing folders).
    A file that already has a queued, not-yet-started upload isn't queued twice."""
    from . import engine as E
    join = join or (lambda a, b: (a.rstrip("/") + "/" + b) if a != "/" else "/" + b)
    with engine._cv:
        waiting = {j.src for j in engine.jobs if j.status == E.QUEUED and j.kind == "upload"}
    jobs = []
    for rel in rel_paths:
        src = os.path.join(root, *rel.split("/"))
        if src in waiting:
            continue
        try:
            st = os.stat(src)
        except OSError:
            continue
        dst = remote_root
        for part in rel.split("/"):
            dst = join(dst, part)
        jobs.append(E.Job("upload", site, src, dst, size=st.st_size, mtime=st.st_mtime,
                          policy="overwrite", make_parents=True))
    if jobs:
        engine.add(jobs)
    return jobs
