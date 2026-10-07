"""Search a folder tree on any backend, and add up folder sizes.

Qt-free: the app runs it on a session's worker thread, the CLI (`blamixfiles find`, `blamixfiles du`)
calls it directly. Walks breadth-first so near matches show up first, never follows folder links
(loops), and stops as soon as `should_stop()` says so.
"""
from __future__ import annotations

import fnmatch
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .vfs import Backend, Entry

UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2, "g": 1024 ** 3, "gb": 1024 ** 3,
         "t": 1024 ** 4, "tb": 1024 ** 4}
AGES = {"m": 60, "min": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_size(text: str) -> int:
    """'1.5M', '200k', '3 GB', '512' -> bytes."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", text or "")
    if not m or m.group(2).lower() not in UNITS:
        raise ValueError(f"Not a size: {text!r} (examples: 500k, 10M, 2G)")
    return int(float(m.group(1)) * UNITS[m.group(2).lower()])


def parse_age(text: str) -> float:
    """'30m', '12h', '7d', '2w' -> seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*", text or "")
    if not m or m.group(2).lower() not in AGES:
        raise ValueError(f"Not an age: {text!r} (examples: 30m, 12h, 7d, 2w)")
    return float(m.group(1)) * AGES[m.group(2).lower()]


@dataclass
class Query:
    name: str = ""                 # glob ("*.log") or, without wildcards, part of the name
    case_sensitive: bool = False
    min_size: int = 0              # bytes; 0 = no lower bound
    max_size: int = 0              # bytes; 0 = no upper bound
    newer_than: float = 0.0        # unix time; 0 = any
    older_than: float = 0.0
    kind: str = "any"              # any | file | folder
    max_depth: int = 0             # 0 = unlimited
    excludes: list[str] = field(default_factory=lambda: [".git", "node_modules"])

    def matches(self, e: Entry) -> bool:
        if self.kind == "file" and e.is_dir or self.kind == "folder" and not e.is_dir:
            return False
        if self.name:
            name, pat = (e.name, self.name) if self.case_sensitive else (e.name.lower(), self.name.lower())
            if any(c in pat for c in "*?["):
                if not fnmatch.fnmatchcase(name, pat):
                    return False
            elif pat not in name:
                return False
        if not e.is_dir:
            if self.min_size and e.size < self.min_size:
                return False
            if self.max_size and e.size > self.max_size:
                return False
        if self.newer_than and (not e.mtime or e.mtime < self.newer_than):
            return False
        if self.older_than and (not e.mtime or e.mtime > self.older_than):
            return False
        return True


def find(b: Backend, root: str, q: Query, should_stop: Callable[[], bool] = lambda: False,
         on_folder: Callable[[str], None] | None = None, limit: int = 10_000) -> Iterator[Entry]:
    """Matching entries under `root`, nearest first. Folders that can't be listed are skipped."""
    todo = deque([(root, 0)])
    found = 0
    while todo and not should_stop():
        path, depth = todo.popleft()
        if on_folder:
            on_folder(path)
        try:
            entries = b.list(path)
        except Exception:
            continue                                       # permission denied etc.: keep going
        for e in sorted(entries, key=lambda x: x.name.lower()):
            if should_stop():
                return
            if q.matches(e):
                yield e
                found += 1
                if found >= limit:
                    return
            if e.is_dir and not e.is_link and e.name not in q.excludes:
                if not q.max_depth or depth + 1 < q.max_depth:
                    todo.append((e.path, depth + 1))


@dataclass
class Usage:
    bytes: int = 0
    files: int = 0
    folders: int = 0
    unreadable: int = 0             # folders that couldn't be listed (counted as 0)
    seconds: float = 0.0


def folder_size(b: Backend, root: str, should_stop: Callable[[], bool] = lambda: False,
                on_progress: Callable[[Usage], None] | None = None) -> Usage:
    """Total size of everything under `root` (folder links are not followed)."""
    u = Usage()
    start = time.monotonic()
    todo = deque([root])
    while todo and not should_stop():
        path = todo.popleft()
        try:
            entries = b.list(path)
        except Exception:
            u.unreadable += 1
            continue
        for e in entries:
            if e.is_dir:
                if not e.is_link:
                    u.folders += 1
                    todo.append(e.path)
            else:
                u.files += 1
                u.bytes += e.size
        if on_progress:
            on_progress(u)
    u.seconds = time.monotonic() - start
    return u
