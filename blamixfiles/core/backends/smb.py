"""SMB 2/3: Windows shares, NAS boxes, Samba (smbprotocol, pure Python, no OS mount needed).

Paths are /<share>/<folder>/<file>. SMB has no standard way to list the shares of a server from this
library, so the share is part of the site's start folder ("/Public"); "/" shows that share.
User names can carry a domain: DOMAIN\\user or user@domain.
"""
from __future__ import annotations

import posixpath
import stat as statmod
import uuid

from ..vfs import Backend, BackendError, Capabilities, Entry, ProgressFn

CHUNK = 1024 * 1024


def _client():
    try:
        import smbclient
    except ImportError:                                  # pragma: no cover - a packaging problem
        raise BackendError("SMB support isn't installed (pip install smbprotocol)") from None
    return smbclient


class SMBBackend(Backend):
    name = "smb"
    caps = Capabilities(resume=True, rename=True, chmod=False, set_mtime=True, symlinks=False,
                        atomic_replace=True)

    def __init__(self, site):
        self.site = site
        self._cache: dict = {}                            # this backend's own connection (workers don't share)
        self._ok = False
        self.smb = _client()

    # ---- lifecycle
    @property
    def _kw(self) -> dict:
        s = self.site
        return dict(username=s.username or None, password=s.password or None,
                    port=s.effective_port, connection_cache=self._cache)

    def connect(self) -> None:
        s = self.site
        if not s.host:
            raise BackendError("Enter the server's name or address")
        try:
            self.smb.register_session(s.host, username=s.username or None, password=s.password or None,
                                      port=s.effective_port, connection_cache=self._cache,
                                      connection_timeout=20)
        except Exception as e:  # noqa: BLE001
            raise BackendError(_explain(e)) from None
        self._ok = True

    def close(self) -> None:
        try:
            self.smb.reset_connection_cache(connection_cache=self._cache)
        except Exception:  # noqa: BLE001
            pass
        self._ok = False

    @property
    def connected(self) -> bool:
        return self._ok

    def home(self) -> str:
        share = self._share_of(self.site.remote_dir)
        return self.normalize(self.site.remote_dir) if share else "/"

    # ---- paths
    @staticmethod
    def _share_of(path: str) -> str:
        parts = [p for p in (path or "").split("/") if p]
        return parts[0] if parts else ""

    def unc(self, path: str) -> str:
        parts = [p for p in posixpath.normpath(path or "/").split("/") if p]
        if not parts:
            raise BackendError("Open a share first: put it in the site's start folder, e.g. /Public")
        return "\\\\" + self.site.host + "\\" + "\\".join(parts)

    # ---- listing
    def list(self, path: str) -> list[Entry]:
        if path in ("", "/"):
            share = self._share_of(self.site.remote_dir)
            if not share:
                raise BackendError("Which share? Enter it as the site's start folder, e.g. /Public "
                                   "(SMB servers don't list their shares to this client).")
            return [Entry(name=share, path="/" + share, is_dir=True)]
        out = []
        try:
            for d in self.smb.scandir(self.unc(path), **self._kw):
                st = d.stat()
                is_dir = d.is_dir()
                out.append(Entry(name=d.name, path=self.join(path, d.name), is_dir=is_dir,
                                 size=0 if is_dir else int(st.st_size), mtime=float(st.st_mtime)))
        except Exception as e:  # noqa: BLE001
            raise BackendError(_explain(e)) from None
        return out

    def stat(self, path: str) -> Entry | None:
        p = self.normalize(path)
        if p == "/":
            return Entry(name="/", path="/", is_dir=True)
        try:
            st = self.smb.stat(self.unc(p), **self._kw)
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001
            if _is_missing(e):
                return None
            raise BackendError(_explain(e)) from None
        is_dir = statmod.S_ISDIR(st.st_mode)
        return Entry(name=self.basename(p) or p, path=p, is_dir=is_dir, size=0 if is_dir else int(st.st_size),
                     mtime=float(st.st_mtime))

    # ---- changes
    def _do(self, fn, *args, **kw):
        try:
            return fn(*args, **self._kw, **kw)
        except Exception as e:  # noqa: BLE001
            raise BackendError(_explain(e)) from None

    def mkdir(self, path: str) -> None:
        self._do(self.smb.mkdir, self.unc(path))

    def remove(self, path: str) -> None:
        self._do(self.smb.remove, self.unc(path))

    def rmdir(self, path: str) -> None:
        self._do(self.smb.rmdir, self.unc(path))

    def rename(self, src: str, dst: str) -> None:
        self._do(self.smb.replace, self.unc(src), self.unc(dst))      # replaces an existing target

    def set_mtime(self, path: str, mtime: float) -> None:
        self._do(self.smb.utime, self.unc(path), times=(mtime, mtime))

    # ---- data
    def download(self, path: str, fp, offset: int = 0, progress: ProgressFn | None = None) -> None:
        try:
            with self.smb.open_file(self.unc(path), mode="rb", **self._kw) as f:
                if offset:
                    f.seek(offset)
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    fp.write(chunk)
                    if progress:
                        progress(len(chunk))
        except BackendError:
            raise
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "Cancelled":
                raise
            raise BackendError(_explain(e)) from None

    def upload(self, fp, path: str, offset: int = 0, progress: ProgressFn | None = None) -> None:
        unc = self.unc(path)
        try:
            if offset:
                self.smb.truncate(unc, offset, **self._kw)
            with self.smb.open_file(unc, mode="ab" if offset else "wb", **self._kw) as f:
                while True:
                    chunk = fp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    if progress:
                        progress(len(chunk))
        except BackendError:
            raise
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "Cancelled":
                raise
            raise BackendError(_explain(e)) from None

    def write_bytes(self, path: str, data: bytes, atomic: bool = True) -> None:
        if not atomic:
            return super().write_bytes(path, data, atomic=False)
        tmp = self.join(self.parent(path), f".{self.basename(path)}.{uuid.uuid4().hex[:6]}.blamixfiles-tmp")
        import io
        self.upload(io.BytesIO(data), tmp)
        try:
            self.rename(tmp, path)
        except Exception:
            try:
                self.remove(tmp)
            except Exception:  # noqa: BLE001
                pass
            raise


def _is_missing(e: Exception) -> bool:
    text = str(e)
    return any(k in text for k in ("STATUS_OBJECT_NAME_NOT_FOUND", "STATUS_OBJECT_PATH_NOT_FOUND", "No such file"))


def _explain(e: Exception) -> str:
    text = str(e)
    for key, msg in (("STATUS_LOGON_FAILURE", "Wrong user name or password"),
                     ("STATUS_ACCESS_DENIED", "Access denied"),
                     ("STATUS_BAD_NETWORK_NAME", "No share with that name on this server"),
                     ("STATUS_OBJECT_NAME_NOT_FOUND", "Not found"),
                     ("STATUS_OBJECT_PATH_NOT_FOUND", "Folder not found"),
                     ("STATUS_OBJECT_NAME_COLLISION", "Something with that name already exists"),
                     ("STATUS_DIRECTORY_NOT_EMPTY", "The folder isn't empty"),
                     ("STATUS_SHARING_VIOLATION", "The file is in use on the server")):
        if key in text:
            return msg
    return text or type(e).__name__
