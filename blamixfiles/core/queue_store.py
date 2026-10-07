"""Keeps unfinished transfers on disk (SQLite) so they survive a crash or restart.

Only jobs of *saved* sites are stored, and only by site id: no host names,
user names or passwords end up in this file (those stay in the encrypted vault).
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .engine import CANCELLED, DONE, FAILED, QUEUED, RUNNING, SKIPPED, Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  key TEXT PRIMARY KEY, seq INTEGER, kind TEXT, site_id TEXT, src TEXT, dst TEXT,
  is_dir INTEGER, size INTEGER, mtime REAL, status TEXT, error TEXT, policy TEXT)
"""


class QueueStore:
    def __init__(self, path: Path, keep_site: callable = lambda site_id: True):
        self.path = Path(path)
        self.keep_site = keep_site
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(SCHEMA)
        self._seq = (self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM jobs").fetchone()[0] or 0)

    def sync(self, job: Job) -> None:
        """Called on every status change."""
        if job.kind == "relay":                  # server -> server copies aren't kept across restarts
            return
        site_id = getattr(job.site, "id", "")
        if not site_id or not self.keep_site(site_id):
            return
        with self._lock:
            if job.status in (DONE, SKIPPED, CANCELLED):
                self._db.execute("DELETE FROM jobs WHERE key = ?", (job.key,))
                return
            row = self._db.execute("SELECT seq FROM jobs WHERE key = ?", (job.key,)).fetchone()
            if row:
                seq = row[0]
            else:
                self._seq += 1
                seq = self._seq
            self._db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (job.key, seq, job.kind, site_id, job.src, job.dst, int(job.is_dir), job.size,
                 job.mtime, job.status, job.error, job.policy))

    def forget(self, keys: list[str]) -> None:
        with self._lock:
            self._db.executemany("DELETE FROM jobs WHERE key = ?", [(k,) for k in keys])

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM jobs")

    def load(self, sites: dict) -> list[Job]:
        """Unfinished jobs, in order. `sites` maps site id -> Site; jobs of sites that
        were deleted meanwhile are dropped. Interrupted transfers resume where they stopped."""
        out: list[Job] = []
        gone: list[str] = []
        with self._lock:
            rows = self._db.execute(
                "SELECT key, kind, site_id, src, dst, is_dir, size, mtime, status, error, policy "
                "FROM jobs ORDER BY seq").fetchall()
        for key, kind, site_id, src, dst, is_dir, size, mtime, status, error, policy in rows:
            site = sites.get(site_id)
            if site is None:
                gone.append(key)
                continue
            was_running = status == RUNNING
            out.append(Job(kind, site, src, dst, is_dir=bool(is_dir), size=size, mtime=mtime,
                           key=key, status=FAILED if status == FAILED else QUEUED, error=error or "",
                           policy="resume" if was_running and not is_dir else (policy or "")))
        if gone:
            self.forget(gone)
        return out

    def close(self) -> None:
        with self._lock:
            self._db.close()
