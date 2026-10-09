"""Transfer queue: parallel workers, per-site limits, resume, retry, overwrite policy.

Qt-free: the GUI subscribes with `on_change` (called from worker threads; the GUI
marshals it onto the UI thread), the CLI just waits.

Each worker keeps its own connection per site (FTP and SFTP can't run two
transfers on one channel). `connector(site)` makes those connections; the GUI
passes one that opens extra SFTP channels on the already-authenticated SSH
connection, so 2FA sites don't prompt again for every worker.
"""
from __future__ import annotations

import itertools
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .errors import friendly, is_connection_error
from .ratelimit import TokenBucket
from .vfs import Backend, BackendError, Cancelled, Entry

POLICIES = {
    "ask": "Ask",                      # (GUI only; the CLI treats it as overwrite)
    "overwrite": "Overwrite",
    "newer": "Overwrite if source is newer",
    "resume": "Resume / overwrite if different size",
    "skip": "Skip existing",
}

QUEUED, RUNNING, DONE, FAILED, SKIPPED, CANCELLED = (
    "queued", "running", "done", "failed", "skipped", "cancelled")
FINISHED = (DONE, FAILED, SKIPPED, CANCELLED)

_ids = itertools.count(1)

_WIN_BAD = set('<>:"|?*')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_local_name(name: str, windows: bool | None = None) -> str:
    """A file name from the server, made safe to create locally. Names that could
    climb out of the target folder ('..', 'a/b', 'a\\b' on Windows) are refused;
    characters Windows can't store are replaced."""
    windows = (os.name == "nt") if windows is None else windows
    seps = ("/", "\\") if windows else ("/",)
    if not name or name in (".", "..") or "\x00" in name or any(sep in name for sep in seps):
        raise BackendError(f"Refusing unsafe file name from the server: {name!r}")
    if windows:
        name = "".join("_" if c in _WIN_BAD or ord(c) < 32 else c for c in name).rstrip(" .") or "_"
        if name.split(".")[0].upper() in _WIN_RESERVED:
            name = "_" + name
    return name


@dataclass
class Job:
    kind: str                     # upload | download
    site: object                  # models.Site
    src: str
    dst: str
    is_dir: bool = False
    size: int = 0
    mtime: float = 0.0
    id: int = field(default_factory=lambda: next(_ids))
    done: int = 0
    status: str = QUEUED
    error: str = ""
    attempts: int = 0
    started: float = 0.0
    finished: float = 0.0
    resumed_from: int = 0
    policy: str = ""              # per-job override ("" = engine policy)
    cancel_flag: bool = False
    key: str = field(default_factory=lambda: uuid.uuid4().hex)   # stable id for the saved queue
    make_parents: bool = False    # create missing parent folders first (sync jobs)
    verified: str = ""            # "sha256"/"md5" when checked, "unsupported", or "" (not checked)
    dst_site: object = None       # relay (server -> server) jobs: where the copy goes

    @property
    def name(self) -> str:
        return os.path.basename(self.src.rstrip("/\\")) or self.src

    @property
    def speed(self) -> float:
        if not self.started:
            return 0.0
        end = self.finished or time.time()
        dt = max(end - self.started, 1e-3)
        return max(0, self.done - self.resumed_from) / dt


Connector = Callable[[object], Backend]
OverwriteAsk = Callable[[Job, Entry], str]   # returns a policy name, "<policy>-all" or "cancel"


class TransferLog:
    """One line per finished file, tab-separated, kept in a plain text file:
    time, status, direction, site, source, destination, bytes, seconds, error.
    Rotated to <name>.1 above 2 MB, so it never grows without bound."""

    MAX_BYTES = 2_000_000

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def add(self, job: Job) -> None:
        secs = (job.finished or time.time()) - job.started if job.started else 0.0
        fields = [time.strftime("%Y-%m-%d %H:%M:%S"), job.status, job.kind,
                  getattr(job.site, "label", "") or "", job.src, job.dst, str(job.done), f"{secs:.1f}",
                  job.error or ""]
        line = "\t".join(f.replace("\t", " ").replace("\n", " ") for f in fields) + "\n"
        try:
            with self._lock:
                p = os.fspath(self.path)
                if os.path.exists(p) and os.path.getsize(p) > self.MAX_BYTES:
                    os.replace(p, p + ".1")
                with open(p, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            pass                                       # a log that can't be written never stops a transfer


def read_log(path, last: int = 300) -> list[str]:
    """The last `last` lines of the transfer log (oldest first), or []."""
    try:
        with open(os.fspath(path), encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-last:]
    except OSError:
        return []


class TransferEngine:
    MAX_ATTEMPTS = 3

    def __init__(self, connector: Connector, workers: int = 3,
                 on_change: Callable[[Job], None] | None = None,
                 policy: str = "overwrite", ask: OverwriteAsk | None = None,
                 preserve_mtime: bool = True, store=None, limit_up: int = 0, limit_down: int = 0,
                 verify: bool = False, log_path=None):
        """store: optional QueueStore (unfinished jobs survive restarts).
        limit_up / limit_down: bytes per second for all transfers together (0 = no limit); a site can
        add its own limits (Site.limit_up_kb / limit_down_kb).
        log_path: a file that gets one line per finished transfer (see TransferLog)."""
        self.connector = connector
        self.store = store
        self.verify = verify          # compare checksums after each file (when the server can)
        self.limits = {"upload": TokenBucket(limit_up), "download": TokenBucket(limit_down)}
        self._site_buckets: dict[tuple[str, str], TokenBucket] = {}
        self._ask_lock = threading.Lock()
        self._batch_policy = ""               # an "… for all remaining files" answer (this batch only)
        self.log = TransferLog(log_path) if log_path else None
        self.on_change = on_change or (lambda j: None)
        self.policy = policy
        self.ask = ask
        self.preserve_mtime = preserve_mtime
        self.jobs: list[Job] = []
        self._cv = threading.Condition()
        self._stop = False
        self._paused = False
        self._running_per_site: dict[str, int] = {}
        self._last_emit: dict[int, float] = {}
        self._threads = [threading.Thread(target=self._worker, name=f"transfer-{i}", daemon=True)
                         for i in range(max(1, workers))]
        for t in self._threads:
            t.start()

    # ------------------------------------------------------------ public API
    def upload(self, site, local_path: str, remote_dir: str, remote_join=None) -> Job:
        join = remote_join or (lambda a, b: (a.rstrip("/") + "/" + b) if a != "/" else "/" + b)
        st = os.stat(local_path)
        is_dir = os.path.isdir(local_path)
        name = os.path.basename(local_path.rstrip("/\\"))
        job = Job("upload", site, local_path, join(remote_dir, name), is_dir=is_dir,
                  size=0 if is_dir else st.st_size, mtime=st.st_mtime)
        self._add([job])
        return job

    def download(self, site, entry: Entry, local_dir: str) -> Job:
        job = Job("download", site, entry.path, os.path.join(local_dir, safe_local_name(entry.name)),
                  is_dir=entry.is_dir, size=entry.size, mtime=entry.mtime)
        self._add([job])
        return job

    def relay(self, site, entry: Entry, dst_site, dst_dir: str, dst_join=None) -> Job:
        """Copy a file or folder from one server to another (or to another folder on the same one).
        The data passes through this computer in a temporary file that is deleted right after."""
        join = dst_join or (lambda a, b: (a.rstrip("/") + "/" + b) if a != "/" else "/" + b)
        job = Job("relay", site, entry.path, join(dst_dir, entry.name), is_dir=entry.is_dir,
                  size=entry.size, mtime=entry.mtime, dst_site=dst_site)
        self._add([job])
        return job

    def move(self, job_id: int, where: str) -> None:
        """Reorder a waiting job: where = "top" | "up" | "down" | "bottom"."""
        with self._cv:
            job = next((j for j in self.jobs if j.id == job_id), None)
            if job is None or job.status != QUEUED:
                return
            i = self.jobs.index(job)
            waiting = [k for k, j in enumerate(self.jobs) if j.status == QUEUED]
            pos = waiting.index(i)
            if where == "top":
                target = waiting[0]
            elif where == "bottom":
                target = waiting[-1]
            elif where == "up":
                target = waiting[max(0, pos - 1)]
            else:
                target = waiting[min(len(waiting) - 1, pos + 1)]
            if target == i:
                return
            self.jobs.pop(i)
            self.jobs.insert(target, job)
            self._cv.notify_all()

    def add(self, jobs: list[Job]) -> list[Job]:
        """Queue ready-made jobs (the sync planner builds them with exact source/target paths)."""
        self._add(jobs)
        return jobs

    def cancel(self, job_id: int | None = None) -> None:
        """Cancel one job, or everything that hasn't finished."""
        with self._cv:
            for j in self.jobs:
                if (job_id is None or j.id == job_id) and j.status not in FINISHED:
                    j.cancel_flag = True
                    if j.status == QUEUED:
                        j.status = CANCELLED
                        self._emit(j, force=True)
            self._cv.notify_all()

    def retry(self, job_id: int | None = None) -> None:
        with self._cv:
            for j in self.jobs:
                if (job_id is None or j.id == job_id) and j.status in (FAILED, CANCELLED):
                    j.status, j.error, j.cancel_flag, j.attempts = QUEUED, "", False, 0
                    self._emit(j, force=True)
            self._cv.notify_all()

    def clear_finished(self) -> list[int]:
        with self._cv:
            gone = [j.id for j in self.jobs if j.status in (DONE, SKIPPED, CANCELLED)]
            self.jobs = [j for j in self.jobs if j.status not in (DONE, SKIPPED, CANCELLED)]
            return gone

    def restore(self, jobs: list[Job], paused: bool = True) -> None:
        """Put back jobs from the saved queue (paused by default, so the user decides)."""
        if not jobs:
            return
        with self._cv:
            self._paused = paused or self._paused
        self._add(jobs)

    def discard_unfinished(self) -> None:
        """Drop everything that hasn't finished (and forget it on disk)."""
        with self._cv:
            for j in self.jobs:
                if j.status not in FINISHED or j.status == FAILED:
                    j.cancel_flag = True
                    if j.status != RUNNING:
                        j.status = CANCELLED
                        self._emit(j, force=True)
            self._cv.notify_all()

    def set_limit(self, direction: str, bytes_per_s: int) -> None:
        self.limits[direction].rate = bytes_per_s

    def set_paused(self, paused: bool) -> None:
        with self._cv:
            self._paused = paused
            self._cv.notify_all()

    @property
    def paused(self) -> bool:
        return self._paused

    def pending(self) -> int:
        with self._cv:
            return sum(1 for j in self.jobs if j.status in (QUEUED, RUNNING))

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the queue is empty (CLI). Returns False on timeout."""
        end = None if timeout is None else time.time() + timeout
        with self._cv:
            while any(j.status in (QUEUED, RUNNING) for j in self.jobs):
                left = None if end is None else end - time.time()
                if left is not None and left <= 0:
                    return False
                self._cv.wait(0.2 if left is None else min(0.2, left))
        return True

    def shutdown(self) -> None:
        with self._cv:
            self._stop = True
            for j in self.jobs:
                if j.status not in FINISHED:
                    j.cancel_flag = True
            self._cv.notify_all()

    # ------------------------------------------------------------ internals
    def _add(self, jobs: list[Job], after: Job | None = None) -> None:
        with self._cv:
            if after is not None and after in self.jobs:
                i = self.jobs.index(after) + 1
                self.jobs[i:i] = jobs
            else:
                self.jobs.extend(jobs)
            for j in jobs:
                self._emit(j, force=True)
            self._cv.notify_all()

    def _emit(self, job: Job, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit.get(job.id, 0) < 0.1:
            return
        self._last_emit[job.id] = now
        if force and self.store is not None:
            try:
                self.store.sync(job)
            except Exception:
                pass
        if force and job.status in FINISHED and not job.is_dir and self.log is not None:
            self.log.add(job)
        try:
            self.on_change(job)
        except Exception:
            pass

    def _site_bucket(self, job: Job) -> TokenBucket | None:
        """The site's own limit for this direction (None = the site has none)."""
        kb = int(getattr(job.site, "limit_up_kb" if job.kind == "upload" else "limit_down_kb", 0) or 0)
        if kb <= 0:
            return None
        key = (job.site.id, job.kind)
        with self._cv:
            bucket = self._site_buckets.get(key)
            if bucket is None:
                bucket = self._site_buckets[key] = TokenBucket(kb * 1024)
            elif bucket.rate != kb * 1024:           # edited in the site dialog while running
                bucket.rate = kb * 1024
        return bucket

    def _next_job(self) -> Job | None:
        for j in self.jobs:
            if j.status != QUEUED:
                continue
            sid = j.site.id
            if self._running_per_site.get(sid, 0) < max(1, getattr(j.site, "parallel", 3)):
                return j
        return None

    def _worker(self) -> None:
        conns: dict[str, Backend] = {}
        try:
            while True:
                with self._cv:
                    while not self._stop and (self._paused or (job := self._next_job()) is None):
                        self._cv.wait(0.5)
                    if self._stop:
                        return
                    job.status = RUNNING
                    job.started = job.started or time.time()
                    self._running_per_site[job.site.id] = self._running_per_site.get(job.site.id, 0) + 1
                    self._emit(job, force=True)
                try:
                    self._run(job, conns)
                finally:
                    with self._cv:
                        self._running_per_site[job.site.id] -= 1
                        if job.status in FINISHED:
                            job.finished = time.time()
                        if self._batch_policy and not any(j.status in (QUEUED, RUNNING) for j in self.jobs):
                            self._batch_policy = ""      # the batch is done: the next one asks again
                        self._emit(job, force=True)
                        self._cv.notify_all()
        finally:
            for b in conns.values():
                try:
                    b.close()
                except Exception:
                    pass

    def _conn(self, site, conns: dict[str, Backend], role: str = "") -> Backend:
        key = site.id + role                  # a relay inside one server needs a second connection
        b = conns.get(key)
        if b is None or not b.connected:
            if b is not None:
                try:
                    b.close()
                except Exception:
                    pass
            b = self.connector(site)
            conns[key] = b
        return b

    def _run(self, job: Job, conns: dict[str, Backend]) -> None:
        while True:
            job.attempts += 1
            b = None
            try:
                b = self._conn(job.site, conns)
                if job.kind == "relay":
                    b2 = self._conn(job.dst_site, conns, "#dst")
                    if job.is_dir:
                        self._expand_relay(job, b, b2)
                    else:
                        self._relay_file(job, b, b2)
                elif job.is_dir:
                    self._expand(job, b)
                elif job.kind == "upload":
                    self._upload_file(job, b)
                else:
                    self._download_file(job, b)
                job.error = ""
                if self.verify and job.status == DONE and not job.is_dir and job.kind != "relay":
                    self._verify(job, b)
                return
            except Cancelled:
                job.status = CANCELLED
                return
            except Exception as e:  # noqa: BLE001 (reported to the user)
                if job.cancel_flag:
                    job.status = CANCELLED
                    return
                # retry only when the connection dropped (never on login or permission errors)
                lost = is_connection_error(e) or (b is not None and not b.connected)
                if lost:
                    conns.pop(job.site.id, None)
                    if job.dst_site is not None:
                        conns.pop(job.dst_site.id + "#dst", None)
                    if job.attempts < self.MAX_ATTEMPTS:
                        job.error = f"{friendly(e)}; retrying…"
                        self._emit(job, force=True)
                        time.sleep(min(8, 2 ** job.attempts))
                        continue
                job.status = FAILED
                job.error = friendly(e)
                return

    # ---- folders: create the target, then queue the children right after
    def _expand(self, job: Job, b: Backend) -> None:
        children: list[Job] = []
        if job.kind == "upload":
            b.makedirs(job.dst)
            with os.scandir(job.src) as it:
                for d in sorted(it, key=lambda x: (not x.is_dir(), x.name.lower())):
                    try:
                        st = d.stat()
                    except OSError:
                        continue
                    children.append(Job("upload", job.site, d.path, b.join(job.dst, d.name),
                                        is_dir=d.is_dir(), size=0 if d.is_dir() else st.st_size,
                                        mtime=st.st_mtime, policy=job.policy))
        else:
            os.makedirs(job.dst, exist_ok=True)
            for e in sorted(b.list(job.src), key=lambda x: (not x.is_dir, x.name.lower())):
                if e.is_link and e.is_dir:
                    continue   # don't follow remote folder links (loops)
                try:
                    local = os.path.join(job.dst, safe_local_name(e.name))
                except BackendError as bad:
                    skipped = Job("download", job.site, e.path, job.dst, status=FAILED, error=str(bad))
                    children.append(skipped)
                    continue
                children.append(Job("download", job.site, e.path, local,
                                    is_dir=e.is_dir, size=e.size, mtime=e.mtime, policy=job.policy))
        job.status = DONE
        if children:
            self._add(children, after=job)

    # ---- server -> server
    def _expand_relay(self, job: Job, src: Backend, dst: Backend) -> None:
        dst.makedirs(job.dst)
        children = []
        for e in sorted(src.list(job.src), key=lambda x: (not x.is_dir, x.name.lower())):
            if e.is_link and e.is_dir:
                continue
            children.append(Job("relay", job.site, e.path, dst.join(job.dst, e.name), is_dir=e.is_dir,
                                size=e.size, mtime=e.mtime, policy=job.policy, dst_site=job.dst_site))
        job.status = DONE
        if children:
            self._add(children, after=job)

    def _relay_file(self, job: Job, src: Backend, dst: Backend) -> None:
        """Download into a temporary file, then upload it: every backend can read and write a real
        file (some need its size or to seek in it), which a pipe between the two can't promise."""
        import tempfile
        if self._decide(job, job.size, job.mtime, dst.stat(job.dst)) is None:
            job.status = SKIPPED
            return
        job.done = job.resumed_from = 0
        half = {"phase": 0}
        down_bucket, up_bucket = self.limits["download"], self.limits["upload"]

        def progress(n: int) -> None:            # the bar covers both halves: down, then up
            if job.cancel_flag:
                raise Cancelled()
            half["phase"] += n
            job.done = min(job.size, half["phase"] // 2) if job.size else 0
            (down_bucket if half["phase"] <= job.size else up_bucket).consume(n, lambda: job.cancel_flag)
            self._emit(job)
        src.read_ahead = down_bucket.rate == 0
        with tempfile.TemporaryFile(prefix="blamixfiles-relay-") as tmp:
            src.download(job.src, tmp, 0, progress)
            tmp.seek(0)
            dst.upload(tmp, job.dst, 0, progress)
        if self.preserve_mtime and dst.caps.set_mtime and job.mtime:
            try:
                dst.set_mtime(job.dst, job.mtime)
            except Exception:
                pass
        job.done = job.size
        job.status = DONE

    # ---- overwrite policy: returns start offset, or None to skip
    def _decide(self, job: Job, src_size: int, src_mtime: float, dst: Entry | None) -> int | None:
        if dst is None:
            return 0
        policy = job.policy or self._batch_policy or self.policy
        if policy == "ask":
            # One question at a time: several workers can hit an existing file at once, and an
            # "all remaining files" answer must cover the ones already waiting to ask.
            with self._ask_lock:
                policy = job.policy or self._batch_policy or self.policy
                if policy == "ask":
                    policy = self.ask(job, dst) if self.ask else "overwrite"
                    if policy.endswith("-all"):          # "overwrite-all" etc. from the dialog
                        policy = policy[:-4]
                        if policy != "cancel":
                            self._batch_policy = policy  # until the queue is empty, then it asks again
            if policy == "cancel":
                raise Cancelled()
        if policy == "skip":
            return None
        if policy == "newer":
            return 0 if src_mtime > dst.mtime + 1 else None
        if policy == "resume":
            if dst.size == src_size:
                return None
            if 0 < dst.size < src_size:
                return dst.size
            return 0
        return 0   # overwrite

    def _progress(self, job: Job):
        bucket = self.limits[job.kind]
        site_bucket = self._site_bucket(job)

        def cb(n: int) -> None:
            if job.cancel_flag:
                raise Cancelled()
            job.done += n
            bucket.consume(n, lambda: job.cancel_flag)
            if site_bucket is not None:
                site_bucket.consume(n, lambda: job.cancel_flag)
            self._emit(job)

        def skip(n: int) -> None:
            """Bytes the server already had (a resumed S3/Nextcloud upload): they count
            as done, but not toward the speed or the speed limit."""
            if job.cancel_flag:
                raise Cancelled()
            job.done += n
            job.resumed_from += n
            self._emit(job)
        cb.skip = skip
        return cb

    def _verify(self, job: Job, b: Backend) -> None:
        from .checksum import local_hash
        remote_path, local = (job.dst, job.src) if job.kind == "upload" else (job.src, job.dst)
        job.error = "verifying…"
        self._emit(job, force=True)
        result = b.checksum(remote_path)
        if result is None:
            job.verified, job.error = "unsupported", ""
            return
        algo, remote_h = result
        local_h = local_hash(local, algo, lambda: job.cancel_flag)
        if local_h.lower() == remote_h.lower():
            job.verified, job.error = algo, ""
        else:
            job.verified = "mismatch"
            job.status = FAILED
            job.error = (f"Checksum mismatch ({algo}): the copy differs from the original. "
                         "Retry the transfer.")

    def _upload_file(self, job: Job, b: Backend) -> None:
        st = os.stat(job.src)
        if job.make_parents:
            parent = b.parent(job.dst)
            if parent not in ("", "/") and b.stat(parent) is None:
                b.makedirs(parent)
        job.size, job.mtime = st.st_size, st.st_mtime
        offset = self._decide(job, st.st_size, st.st_mtime, b.stat(job.dst))
        if offset is None:
            job.status = SKIPPED
            return
        if offset and not b.caps.resume:
            offset = 0
        job.done = job.resumed_from = offset
        with open(job.src, "rb") as f:
            f.seek(offset)
            b.upload(f, job.dst, offset, self._progress(job))
        if self.preserve_mtime and b.caps.set_mtime:
            try:
                b.set_mtime(job.dst, st.st_mtime)
            except Exception:
                pass
        job.status = DONE

    def _download_file(self, job: Job, b: Backend) -> None:
        local = None
        if os.path.exists(job.dst):
            st = os.stat(job.dst)
            local = Entry(name=os.path.basename(job.dst), path=job.dst, size=st.st_size, mtime=st.st_mtime)
        offset = self._decide(job, job.size, job.mtime, local)
        if offset is None:
            job.status = SKIPPED
            return
        job.done = job.resumed_from = offset
        if job.make_parents:
            os.makedirs(os.path.dirname(job.dst), exist_ok=True)
        # with a download limit, don't let SFTP read the whole file ahead at full speed
        b.read_ahead = self.limits["download"].rate == 0 and not int(getattr(job.site, "limit_down_kb", 0) or 0)
        with open(job.dst, "r+b" if offset else "wb") as f:
            if offset:
                f.seek(offset)
                f.truncate()
            b.download(job.src, f, offset, self._progress(job))
        if self.preserve_mtime and job.mtime:
            try:
                os.utime(job.dst, (job.mtime, job.mtime))
            except OSError:
                pass
        job.status = DONE
