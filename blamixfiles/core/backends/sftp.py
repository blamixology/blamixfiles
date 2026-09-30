"""SFTP over SSH (paramiko), with pipelined I/O for decent throughput."""
from __future__ import annotations

import errno
import stat as statmod
from typing import BinaryIO

import paramiko

from .. import ssh
from ..vfs import Backend, BackendError, Capabilities, Entry, ProgressFn

BLOCK = 64 * 1024


class SFTPBackend(Backend):
    name = "sftp"
    caps = Capabilities(resume=True, rename=True, chmod=True, set_mtime=True,
                        symlinks=True, atomic_replace=True)

    def __init__(self, site, interactive=None):
        self.site = site
        self.interactive = interactive
        self.client: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None
        self.banner = ""
        self._posix_rename = True

        self._owns_client = True

    @classmethod
    def on_client(cls, site, client: paramiko.SSHClient) -> "SFTPBackend":
        """A second SFTP channel on an already-authenticated SSH connection (no new
        login, no 2FA prompt). Closing it leaves the SSH connection open."""
        b = cls(site)
        b.client = client
        b._owns_client = False
        b.sftp, b.banner = ssh.open_sftp(client)
        return b

    def connect(self) -> None:
        self.client = ssh.open_client(self.site, self.interactive)
        self._owns_client = True
        self.sftp, self.banner = ssh.open_sftp(self.client)

    def close(self) -> None:
        for obj in (self.sftp, self.client if self._owns_client else None):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self.sftp = self.client = None

    @property
    def connected(self) -> bool:
        t = self.client.get_transport() if self.client else None
        return bool(t and t.is_active() and self.sftp)

    def home(self) -> str:
        try:
            return self.sftp.normalize(".")
        except IOError:
            return "/"

    def _entry(self, parent: str, a: paramiko.SFTPAttributes) -> Entry:
        path = self.join(parent, a.filename)
        mode = a.st_mode or 0
        is_link = statmod.S_ISLNK(mode)
        is_dir = statmod.S_ISDIR(mode)
        target = ""
        if is_link:   # follow the link so folders-behind-links open like folders
            try:
                target = self.sftp.readlink(path) or ""
                is_dir = statmod.S_ISDIR(self.sftp.stat(path).st_mode or 0)
            except IOError:
                pass
        owner = ""
        longname = getattr(a, "longname", "") or ""
        if longname:
            parts = longname.split(None, 4)
            if len(parts) >= 4:
                owner = f"{parts[2]}:{parts[3]}"
        return Entry(name=a.filename, path=path, is_dir=is_dir, size=a.st_size or 0,
                     mtime=float(a.st_mtime or 0), mode=mode, owner=owner,
                     is_link=is_link, link_target=target)

    def list(self, path: str) -> list[Entry]:
        return [self._entry(path, a) for a in self.sftp.listdir_attr(path)
                if a.filename not in (".", "..")]

    def stat(self, path: str) -> Entry | None:
        try:
            a = self.sftp.stat(path)
        except IOError as e:
            if getattr(e, "errno", None) == errno.ENOENT or isinstance(e, FileNotFoundError):
                return None
            raise
        a.filename = self.basename(path)
        return self._entry(self.parent(path), a)

    def mkdir(self, path: str) -> None:
        self.sftp.mkdir(path)

    def remove(self, path: str) -> None:
        self.sftp.remove(path)

    def rmdir(self, path: str) -> None:
        self.sftp.rmdir(path)

    def rename(self, src: str, dst: str) -> None:
        # posix-rename@openssh.com replaces the target atomically; plain SFTP rename
        # refuses an existing target, so fall back to remove + rename.
        if self._posix_rename:
            try:
                self.sftp.posix_rename(src, dst)
                return
            except IOError as e:
                if "not supported" in str(e).lower() or "unsupported" in str(e).lower():
                    self._posix_rename = False
                else:
                    raise
        if self.stat(dst) is not None:
            self.sftp.remove(dst)
        self.sftp.rename(src, dst)

    def chmod(self, path: str, mode: int) -> None:
        self.sftp.chmod(path, mode)

    def set_mtime(self, path: str, mtime: float) -> None:
        self.sftp.utime(path, (mtime, mtime))

    def download(self, path: str, fp: BinaryIO, offset: int = 0,
                 progress: ProgressFn | None = None) -> None:
        with self.sftp.open(path, "rb", bufsize=BLOCK) as f:
            size = f.stat().st_size or 0
            if offset:
                f.seek(offset)
            if self.read_ahead:
                f.prefetch(max(0, size - offset))   # parallel read requests
            while True:
                chunk = f.read(BLOCK)
                if not chunk:
                    break
                fp.write(chunk)
                if progress:
                    progress(len(chunk))

    def upload(self, fp: BinaryIO, path: str, offset: int = 0,
               progress: ProgressFn | None = None) -> None:
        mode = "r+b" if offset else "wb"
        try:
            f = self.sftp.open(path, mode, bufsize=BLOCK)
        except IOError as e:
            raise BackendError(f"Can't write {path}: {e.strerror or e}") from None
        with f:
            if offset:
                f.seek(offset)
                f.truncate(offset)
            f.set_pipelined(True)                # don't wait for an ack per block
            while True:
                chunk = fp.read(BLOCK)
                if not chunk:
                    break
                f.write(chunk)
                if progress:
                    progress(len(chunk))
