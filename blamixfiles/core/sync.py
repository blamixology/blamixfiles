"""Folder compare & sync: scan both sides, build a plan the user can review, apply it.

Safety rules:
- Nothing is deleted unless the plan was made in *mirror* mode, and every delete is a
  visible line in the plan.
- Two-way sync only copies (new files, and the newer copy of changed files). Without a
  history of previous syncs a missing file could mean "new here" or "deleted there", so
  two-way never deletes; files changed on both sides with the same timestamp are shown
  as conflicts and left alone.
- Timestamps are compared with a tolerance (FTP servers often report minutes only).
"""
from __future__ import annotations

import fnmatch
import os
import posixpath
from dataclasses import asdict, dataclass, field
from typing import Callable

from .engine import Job, TransferEngine
from .vfs import Backend, Cancelled, Entry

UPLOAD, DOWNLOAD = "upload", "download"
MKDIR_REMOTE, MKDIR_LOCAL = "mkdir-remote", "mkdir-local"
DELETE_REMOTE, DELETE_LOCAL = "delete-remote", "delete-local"
CONFLICT = "conflict"
ACTION_LABELS = {UPLOAD: "Upload", DOWNLOAD: "Download", MKDIR_REMOTE: "Create folder on server",
                 MKDIR_LOCAL: "Create local folder", DELETE_REMOTE: "Delete on server",
                 DELETE_LOCAL: "Delete locally", CONFLICT: "Conflict (skipped)"}
DELETES = (DELETE_REMOTE, DELETE_LOCAL)

DEFAULT_EXCLUDES = [".git", ".svn", ".hg", ".DS_Store", "Thumbs.db", "desktop.ini",
                    "*.blamixfiles-tmp", "__pycache__", "node_modules"]


@dataclass
class SyncOptions:
    direction: str = UPLOAD          # upload (local -> server) | download | both
    mirror: bool = False             # also delete what doesn't exist on the source side
    compare: str = "mtime"           # mtime: size + modified time | size: size only | checksum: content
    tolerance: float = 2.0           # seconds of timestamp difference still counted as "same"
    excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))

    @classmethod
    def from_dict(cls, d: dict) -> "SyncOptions":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Action:
    kind: str
    rel: str                         # path relative to both roots, "/"-separated
    is_dir: bool = False
    local: Entry | None = None
    remote: Entry | None = None
    reason: str = ""
    enabled: bool = True

    @property
    def size(self) -> int:
        src = self.local if self.kind in (UPLOAD, MKDIR_REMOTE) else self.remote
        return (src.size if src and not self.is_dir else 0) or 0


@dataclass
class Plan:
    local_root: str
    remote_root: str
    options: SyncOptions
    actions: list[Action] = field(default_factory=list)
    unchanged: int = 0
    to_check: list = field(default_factory=list)     # same-size pairs to hash (checksum mode)
    unverifiable: int = 0                            # the server couldn't hash these
    scanned_local: int = 0
    scanned_remote: int = 0

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.actions:
            if a.enabled:
                out[a.kind] = out.get(a.kind, 0) + 1
        return out

    def transfer_bytes(self) -> int:
        return sum(a.size for a in self.actions if a.enabled and a.kind in (UPLOAD, DOWNLOAD))

    def summary(self) -> str:
        c = self.counts()
        parts = [f"{n} {ACTION_LABELS[k].lower()}" for k, n in c.items()]
        text = (", ".join(parts) or "Nothing to do") + f"; {self.unchanged} unchanged"
        if self.unverifiable:
            text += f" ({self.unverifiable} compared by size only: the server can't compute checksums)"
        return text


# ------------------------------------------------------------------ scanning
def excluded(rel: str, name: str, patterns: list[str]) -> bool:
    for p in patterns:
        p = p.strip().rstrip("/")
        if not p:
            continue
        if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*"):
            return True
    return False


ProgressFn = Callable[[str], None]     # "scanning <folder>" messages; may raise Cancelled


def scan(b: Backend, root: str, excludes: list[str], progress: ProgressFn | None = None,
         should_stop: Callable[[], bool] = lambda: False) -> dict[str, Entry]:
    """rel path -> Entry for everything under root (folders included). Symlinked
    folders are listed as entries but not followed (no loops)."""
    out: dict[str, Entry] = {}
    stack = [("", root)]
    while stack:
        if should_stop():
            raise Cancelled()
        rel_dir, path = stack.pop()
        if progress:
            progress(rel_dir or ".")
        for e in b.list(path):
            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
            if excluded(rel, e.name, excludes):
                continue
            out[rel] = e
            if e.is_dir and not e.is_link:
                stack.append((rel, e.path))
    return out


# ------------------------------------------------------------------ comparing
def compare(local: dict[str, Entry], remote: dict[str, Entry], local_root: str, remote_root: str,
            opt: SyncOptions) -> Plan:
    """Rules for a file on both sides (tol = opt.tolerance):
    - size differs -> copy from the source side (two-way: from the newer side)
    - same size, source newer by more than tol -> copy (mtime mode only)
    - same size, target newer -> unchanged: that's what an earlier upload looks like on
      servers that can't keep the original time, so we don't re-send every file each run
    """
    plan = Plan(local_root, remote_root, opt, scanned_local=len(local), scanned_remote=len(remote))
    acts: list[Action] = []
    up = opt.direction in (UPLOAD, "both")
    down = opt.direction in (DOWNLOAD, "both")
    tol = opt.tolerance
    for rel in sorted(set(local) | set(remote), key=lambda r: (r.count("/"), r.lower())):
        lo, re_ = local.get(rel), remote.get(rel)
        if lo and re_:
            if lo.is_dir != re_.is_dir:
                acts.append(Action(CONFLICT, rel, lo.is_dir, lo, re_,
                                   "a folder on one side, a file on the other"))
                continue
            if lo.is_dir:
                continue
            if opt.compare == "checksum" and lo.size == re_.size:
                plan.unchanged += 1               # until the hashes say otherwise
                plan.to_check.append((rel, lo, re_))
                continue
            by_time = opt.compare == "mtime"
            local_newer = by_time and lo.mtime > re_.mtime + tol
            remote_newer = by_time and re_.mtime > lo.mtime + tol
            if lo.size == re_.size and not (local_newer and up) and not (remote_newer and down):
                plan.unchanged += 1
                continue
            if opt.direction == UPLOAD:
                why = f"size {re_.size} → {lo.size} bytes" if lo.size != re_.size else "newer here"
                acts.append(Action(UPLOAD, rel, False, lo, re_, why))
            elif opt.direction == DOWNLOAD:
                why = f"size {lo.size} → {re_.size} bytes" if lo.size != re_.size else "newer on the server"
                acts.append(Action(DOWNLOAD, rel, False, lo, re_, why))
            elif local_newer:
                acts.append(Action(UPLOAD, rel, False, lo, re_, "newer here"))
            elif remote_newer:
                acts.append(Action(DOWNLOAD, rel, False, lo, re_, "newer on the server"))
            else:
                acts.append(Action(CONFLICT, rel, False, lo, re_,
                                   "different size, same time: changed on both sides?"))
        elif lo:
            if up:
                acts.append(Action(MKDIR_REMOTE if lo.is_dir else UPLOAD, rel, lo.is_dir, lo, None,
                                   "only here"))
            elif opt.mirror:
                acts.append(Action(DELETE_LOCAL, rel, lo.is_dir, lo, None, "not on the server"))
        else:
            if down:
                acts.append(Action(MKDIR_LOCAL if re_.is_dir else DOWNLOAD, rel, re_.is_dir, None, re_,
                                   "only on the server"))
            elif opt.mirror:
                acts.append(Action(DELETE_REMOTE, rel, re_.is_dir, None, re_, "not here"))
    plan.actions = _collapse_deletes(acts)
    return plan


def resolve_checksums(plan: Plan, local: Backend, remote: Backend,
                      progress: ProgressFn | None = None,
                      should_stop: Callable[[], bool] = lambda: False) -> Plan:
    """Checksum mode: hash the same-size pairs on both sides and add actions for the
    ones whose content differs."""
    from .checksum import local_hash
    opt = plan.options
    for i, (rel, lo, re_) in enumerate(plan.to_check):
        if should_stop():
            raise Cancelled()
        if progress:
            progress(f"checksums {i + 1}/{len(plan.to_check)}: {rel}")
        r = remote.checksum(re_.path)
        if r is None:
            plan.unverifiable += 1
            continue
        algo, rh = r
        if local_hash(lo.path, algo, should_stop) == rh.lower():
            continue
        plan.unchanged -= 1
        if opt.direction == UPLOAD:
            kind = UPLOAD
        elif opt.direction == DOWNLOAD:
            kind = DOWNLOAD
        elif lo.mtime > re_.mtime + opt.tolerance:
            kind = UPLOAD
        elif re_.mtime > lo.mtime + opt.tolerance:
            kind = DOWNLOAD
        else:
            kind = CONFLICT
        plan.actions.append(Action(kind, rel, False, lo, re_, f"content differs ({algo})"))
    plan.to_check = []
    return plan


def _collapse_deletes(acts: list[Action]) -> list[Action]:
    """A deleted folder takes its contents with it: list the folder once, not every file."""
    gone = [a.rel for a in acts if a.kind in DELETES and a.is_dir]
    if not gone:
        return acts
    out = []
    for a in acts:
        if a.kind in DELETES and any(a.rel.startswith(g + "/") for g in gone):
            continue
        out.append(a)
    return out


# ------------------------------------------------------------------ applying
def local_path(root: str, rel: str) -> str:
    return os.path.join(root, *rel.split("/"))


def remote_path(root: str, rel: str) -> str:
    return posixpath.join(root.rstrip("/") or "/", rel)


def apply(plan: Plan, site, engine: TransferEngine, remote: Backend, local: Backend,
          confirm_deletes: bool = True) -> list[Job]:
    """Create folders and delete (mirror) right away, queue the transfers.
    Returns the queued jobs. `remote`/`local` are connected backends used for the
    folder and delete operations (transfers use the engine's own connections)."""
    enabled = [a for a in plan.actions if a.enabled]
    for a in enabled:                       # folders first, parents before children
        if a.kind == MKDIR_REMOTE:
            remote.makedirs(remote_path(plan.remote_root, a.rel))
        elif a.kind == MKDIR_LOCAL:
            os.makedirs(local_path(plan.local_root, a.rel), exist_ok=True)
    jobs: list[Job] = []
    for a in enabled:
        if a.kind == UPLOAD:
            jobs.append(Job(UPLOAD, site, local_path(plan.local_root, a.rel),
                            remote_path(plan.remote_root, a.rel), size=a.local.size,
                            mtime=a.local.mtime, policy="overwrite", make_parents=True))
        elif a.kind == DOWNLOAD:
            jobs.append(Job(DOWNLOAD, site, remote_path(plan.remote_root, a.rel),
                            local_path(plan.local_root, a.rel), size=a.remote.size,
                            mtime=a.remote.mtime, policy="overwrite", make_parents=True))
    if jobs:
        engine.add(jobs)
    for a in reversed(enabled):             # deepest first
        if a.kind == DELETE_REMOTE:
            p = remote_path(plan.remote_root, a.rel)
            remote.remove_tree(p) if a.is_dir and not a.remote.is_link else remote.remove(p)
        elif a.kind == DELETE_LOCAL:
            p = local_path(plan.local_root, a.rel)
            local.remove_tree(p) if a.is_dir and not a.local.is_link else local.remove(p)
    return jobs


# ------------------------------------------------------------------ saved profiles
@dataclass
class SyncProfile:
    name: str
    site_id: str
    local_dir: str
    remote_dir: str
    options: SyncOptions = field(default_factory=SyncOptions)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SyncProfile":
        return cls(name=d["name"], site_id=d["site_id"], local_dir=d["local_dir"],
                   remote_dir=d["remote_dir"], options=SyncOptions.from_dict(d.get("options", {})))
