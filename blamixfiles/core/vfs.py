"""The one interface every protocol implements.

The GUI, the transfer engine, the editor and the CLI only talk to `Backend`, so
they never branch on protocol. `caps` says what a backend can do (the UI hides
what isn't supported: chmod on FTP-without-SITE, rename on SCP, …).
"""
from __future__ import annotations

import io
import posixpath
import stat as statmod
from dataclasses import dataclass, field
from typing import BinaryIO, Callable

ProgressFn = Callable[[int], None]   # called with the number of bytes just moved


class Cancelled(Exception):
    """Raised from a progress callback to abort a transfer."""


class BackendError(Exception):
    """A user-facing error from a backend (message is shown as-is)."""


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool = False
    size: int = 0
    mtime: float = 0.0            # unix time, UTC
    mode: int | None = None       # st_mode when known
    owner: str = ""
    is_link: bool = False
    link_target: str = ""

    @property
    def perms(self) -> str:
        if self.mode is None:
            return ""
        return statmod.filemode(self.mode)


@dataclass
class Capabilities:
    resume: bool = True
    rename: bool = True
    chmod: bool = False
    set_mtime: bool = False
    symlinks: bool = False
    atomic_replace: bool = True    # rename over an existing file works
    extra: dict = field(default_factory=dict)


class Backend:
    """Base class. Paths are strings in the backend's own syntax (POSIX for remotes)."""

    caps = Capabilities()
    name = "backend"
    is_local = False

    # ---- lifecycle
    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    @property
    def connected(self) -> bool:
        return True

    def home(self) -> str:
        return "/"

    # ---- path helpers (POSIX by default; LocalBackend overrides)
    def join(self, *parts: str) -> str:
        return posixpath.join(*parts)

    def parent(self, path: str) -> str:
        p = posixpath.dirname(path.rstrip("/")) if path not in ("", "/") else "/"
        return p or "/"

    def basename(self, path: str) -> str:
        return posixpath.basename(path.rstrip("/"))

    def normalize(self, path: str) -> str:
        return posixpath.normpath(path) if path else "/"

    # ---- listing
    def list(self, path: str) -> list[Entry]:
        raise NotImplementedError

    def stat(self, path: str) -> Entry | None:
        """None if the path doesn't exist."""
        parent = self.parent(path)
        name = self.basename(path)
        for e in self.list(parent):
            if e.name == name:
                return e
        return None

    # ---- changes
    def mkdir(self, path: str) -> None:
        raise NotImplementedError

    def makedirs(self, path: str) -> None:
        if self.stat(path) is not None:
            return
        parent = self.parent(path)
        if parent != path:
            self.makedirs(parent)
        self.mkdir(path)

    def remove(self, path: str) -> None:
        raise NotImplementedError

    def rmdir(self, path: str) -> None:
        raise NotImplementedError

    def remove_tree(self, path: str) -> None:
        for e in self.list(path):
            if e.is_dir and not e.is_link:
                self.remove_tree(e.path)
            else:
                self.remove(e.path)
        self.rmdir(path)

    def rename(self, src: str, dst: str) -> None:
        raise NotImplementedError

    def chmod(self, path: str, mode: int) -> None:
        raise BackendError("Changing permissions isn't supported here")

    def set_mtime(self, path: str, mtime: float) -> None:
        pass

    # ---- data
    def download(self, path: str, fp: BinaryIO, offset: int = 0,
                 progress: ProgressFn | None = None) -> None:
        raise NotImplementedError

    def upload(self, fp: BinaryIO, path: str, offset: int = 0,
               progress: ProgressFn | None = None) -> None:
        raise NotImplementedError

    def read_bytes(self, path: str) -> bytes:
        buf = io.BytesIO()
        self.download(path, buf)
        return buf.getvalue()

    def write_bytes(self, path: str, data: bytes, atomic: bool = True) -> None:
        """Write a whole file. With `atomic`, write a temp file next to it and rename
        it over the original so a dropped connection never leaves half a file; the
        original permissions are kept where the protocol allows it."""
        if not (atomic and self.caps.rename):
            self.upload(io.BytesIO(data), path)
            return
        old = self.stat(path)
        tmp = self.join(self.parent(path), f".{self.basename(path)}.blamixfiles-tmp")
        self.upload(io.BytesIO(data), tmp)
        try:
            if old is not None and self.caps.chmod and old.mode is not None:
                try:
                    self.chmod(tmp, statmod.S_IMODE(old.mode))
                except Exception:
                    pass
            if old is not None and not self.caps.atomic_replace:
                self.remove(path)
            self.rename(tmp, path)
        except Exception:
            try:
                self.remove(tmp)
            except Exception:
                pass
            raise


def sort_entries(entries: list[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))
