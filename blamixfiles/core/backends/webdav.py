"""WebDAV (Nextcloud, ownCloud, Apache mod_dav, NAS boxes, …) over HTTP or HTTPS.

Paths are the decoded URL paths on the server (e.g. /remote.php/dav/files/mike/Photos);
they're percent-encoded per request. Self-signed HTTPS certificates can be pinned by
fingerprint, like FTPS.
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import ssl
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import BinaryIO, Iterator
from urllib.parse import quote, unquote, urlsplit

import httpx

from ..vfs import Backend, BackendError, Capabilities, Entry, ProgressFn
from .ftp import CertificateChanged, UntrustedCertificate

BLOCK = 64 * 1024
CHUNKED = 32 * 1024 * 1024        # Nextcloud: files this big go up in chunks, and can resume
CHUNK = 16 * 1024 * 1024          # chunk size (Nextcloud v2: 5 MB .. 5 GB, at most 10,000 chunks)
MB = 1024 * 1024
_NC_PATH = re.compile(r"^(?P<root>.*?/remote\.php/dav)/files/(?P<user>[^/]+)/")
DAV = "{DAV:}"
PROPFIND_BODY = (b'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop>'
                 b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>")


def _fingerprint(host: str, port: int) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=15) as s, ctx.wrap_socket(s, server_hostname=host) as t:
        der = t.getpeercert(binary_form=True) or b""
    fp = hashlib.sha256(der).hexdigest().upper()
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))


class WebDAVBackend(Backend):
    name = "webdav"
    caps = Capabilities(resume=False, rename=True, chmod=False, set_mtime=False,
                        symlinks=False, atomic_replace=True)

    def __init__(self, site):
        self.site = site
        self.secure = site.protocol == "webdavs"
        self.http: httpx.Client | None = None
        self.base = f"{'https' if self.secure else 'http'}://{site.host}:{site.effective_port}"
        self.chunking: bool | None = None     # Nextcloud chunked uploads: None = not tried yet

    # ------------------------------------------------------------ connection
    def connect(self) -> None:
        site = self.site
        verify: bool | ssl.SSLContext = True
        if self.secure:
            if site.tls_pinned:
                fp = _fingerprint(site.host, site.effective_port)
                if fp != site.tls_pinned:
                    raise CertificateChanged(
                        "The server's TLS certificate changed since you trusted it "
                        f"(now {fp}). This can mean someone is intercepting the connection.")
                verify = False
            elif not site.tls_verify:
                verify = False
        auth = (site.username, site.password) if site.username and site.auth != "anonymous" else None
        self.http = httpx.Client(auth=auth, verify=verify, timeout=httpx.Timeout(30, connect=15),
                                 follow_redirects=False, headers={"User-Agent": "BlamixFiles"})
        try:
            r = self._req("PROPFIND", site.remote_dir or "/", headers={"Depth": "0"},
                          content=PROPFIND_BODY, ok=(207, 200, 404))
        except ConnectionError as e:
            self.close()
            if self.secure and "CERTIFICATE_VERIFY_FAILED" in str(e):
                reason = str(e).split("certificate verify failed:", 1)[-1].split("(_ssl")[0].strip()
                raise UntrustedCertificate(_fingerprint(site.host, site.effective_port), site.host,
                                           reason or "certificate verify failed") from None
            raise
        if r.status_code == 404:
            self.close()
            raise BackendError(f"Folder not found on the server: {site.remote_dir or '/'}")

    def close(self) -> None:
        if self.http is not None:
            try:
                self.http.close()
            except Exception:
                pass
        self.http = None

    @property
    def connected(self) -> bool:
        return self.http is not None

    def home(self) -> str:
        return (self.site.remote_dir or "/").rstrip("/") or "/"

    # ------------------------------------------------------------ requests
    def _url(self, path: str) -> str:
        return self.base + quote(path if path.startswith("/") else "/" + path)

    def _req(self, method: str, path: str, ok: tuple = (200, 201, 204, 207), **kw) -> httpx.Response:
        try:
            r = self.http.request(method, self._url(path), **kw)
        except httpx.TransportError as e:
            raise ConnectionError(str(e) or e.__class__.__name__) from e
        if r.status_code in ok:
            return r
        raise self._error(r, path)

    @staticmethod
    def _error(r: httpx.Response, path: str) -> Exception:
        code = r.status_code
        if code == 401:
            return BackendError("Login failed: check the username and password (Nextcloud: use an app password).")
        if code == 403:
            return PermissionError(13, "Permission denied", path)
        if code == 404:
            return FileNotFoundError(2, "No such file or folder", path)
        if code == 405 and r.request.method == "MKCOL":
            return FileExistsError(17, "Already exists", path)
        if code == 507:
            return BackendError("The server is out of space (507 Insufficient Storage).")
        return BackendError(f"{r.request.method} {path}: HTTP {code} {r.reason_phrase}")

    # ------------------------------------------------------------ listing
    def _parse(self, xml: bytes, parent: str | None) -> list[Entry]:
        out = []
        root = ET.fromstring(xml)
        for resp in root.iter(f"{DAV}response"):
            href = resp.findtext(f"{DAV}href") or ""
            path = unquote(urlsplit(href).path)
            if parent is not None and path.rstrip("/") == parent.rstrip("/"):
                continue                          # the folder itself
            props = None
            for ps in resp.iter(f"{DAV}propstat"):
                status = ps.findtext(f"{DAV}status") or ""
                if " 200 " in status or status.endswith(" 200"):
                    props = ps.find(f"{DAV}prop")
            if props is None:
                continue
            rt = props.find(f"{DAV}resourcetype")
            is_dir = rt is not None and rt.find(f"{DAV}collection") is not None
            size = int(props.findtext(f"{DAV}getcontentlength") or 0)
            lm = props.findtext(f"{DAV}getlastmodified") or ""
            try:
                mtime = parsedate_to_datetime(lm).timestamp() if lm else 0.0
            except (TypeError, ValueError):
                mtime = 0.0
            clean = path.rstrip("/") or "/"
            name = clean.rsplit("/", 1)[-1]
            out.append(Entry(name=name, path=clean, is_dir=is_dir, size=0 if is_dir else size, mtime=mtime))
        return out

    def list(self, path: str) -> list[Entry]:
        r = self._req("PROPFIND", path.rstrip("/") + "/", headers={"Depth": "1"}, content=PROPFIND_BODY)
        return self._parse(r.content, path)

    def stat(self, path: str) -> Entry | None:
        r = self._req("PROPFIND", path, headers={"Depth": "0"}, content=PROPFIND_BODY, ok=(207, 200, 404))
        if r.status_code == 404:
            return None
        entries = self._parse(r.content, None)
        return entries[0] if entries else None

    # ------------------------------------------------------------ changes
    def mkdir(self, path: str) -> None:
        self._req("MKCOL", path.rstrip("/") + "/")

    def remove(self, path: str) -> None:
        self._req("DELETE", path)

    def rmdir(self, path: str) -> None:
        self._req("DELETE", path.rstrip("/") + "/")

    def remove_tree(self, path: str) -> None:       # DELETE on a collection is recursive
        if path.rstrip("/") in ("", "/"):
            raise BackendError("Refusing to delete the whole share")
        self._req("DELETE", path.rstrip("/") + "/")

    def rename(self, src: str, dst: str) -> None:
        self._req("MOVE", src, headers={"Destination": self._url(dst), "Overwrite": "T"})

    # ------------------------------------------------------------ data
    def download(self, path: str, fp: BinaryIO, offset: int = 0,
                 progress: ProgressFn | None = None) -> None:
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with self.http.stream("GET", self._url(path), headers=headers) as r:
                if r.status_code not in (200, 206):
                    raise self._error(r, path)
                skip = offset if (offset and r.status_code == 200) else 0   # server ignored Range
                for chunk in r.iter_bytes(BLOCK):
                    if skip:
                        cut = min(skip, len(chunk))
                        chunk, skip = chunk[cut:], skip - cut
                        if not chunk:
                            continue
                    fp.write(chunk)
                    if progress:
                        progress(len(chunk))
        except httpx.TransportError as e:
            raise ConnectionError(str(e)) from e

    def upload(self, fp: BinaryIO, path: str, offset: int = 0,
               progress: ProgressFn | None = None) -> None:
        if offset:
            raise BackendError("WebDAV can't append to a file")
        start = fp.tell()
        fp.seek(0, 2)
        size = fp.tell() - start
        fp.seek(start)
        if size >= CHUNKED and self.chunking is not False and _NC_PATH.match(path):
            if self._nc_upload(fp, start, size, path, progress):
                return
            fp.seek(start)
        self._put(path, self._stream(fp, size, progress), size)

    @staticmethod
    def _stream(fp, length: int, progress) -> Iterator[bytes]:
        left = length
        while left:
            chunk = fp.read(min(BLOCK, left))
            if not chunk:
                break
            left -= len(chunk)
            yield chunk
            if progress:
                progress(len(chunk))

    def _put(self, path: str, body, size: int, headers: dict | None = None) -> None:
        h = {"Content-Length": str(size), "Content-Type": "application/octet-stream", **(headers or {})}
        self._req("PUT", path, content=body, headers=h, timeout=httpx.Timeout(None, connect=15))

    # ---- Nextcloud chunked upload (v2)
    # The file goes up as numbered chunks into a per-transfer folder under
    # /remote.php/dav/uploads/<user>/, then one MOVE assembles it at the destination.
    # The folder's name comes from (destination, size, modification time), so if the
    # upload stops, the next upload of the same unchanged file finds the chunks the
    # server already has and sends only the rest. Nextcloud removes abandoned upload
    # folders by itself after a while.
    @staticmethod
    def _chunk_size(size: int) -> int:
        need = -(-size // 10_000)
        return max(CHUNK, -(-need // MB) * MB)

    def _nc_upload(self, fp, start: int, size: int, path: str, progress) -> bool:
        """True when uploaded; False when the server doesn't do chunked uploads."""
        m = _NC_PATH.match(path)
        root, user = m.group("root"), m.group("user")
        try:
            mtime = int(os.fstat(fp.fileno()).st_mtime)
        except (AttributeError, OSError, ValueError):
            mtime = 0
        tid = "blamixfiles-" + hashlib.sha1(f"{path}|{size}|{mtime}".encode()).hexdigest()[:32]
        folder = f"{root}/uploads/{user}/{tid}"
        dest = {"Destination": self._url(path), "OC-Total-Length": str(size)}
        r = self._req("MKCOL", folder, headers=dest, ok=(201, 405, 400, 403, 404, 409, 415, 501))
        if r.status_code not in (201, 405):        # no chunking API here (not Nextcloud, or too old)
            self.chunking = False
            return False
        self.chunking = True
        chunk = self._chunk_size(size)
        count = -(-size // chunk)
        have: dict[str, int] = {}
        if r.status_code == 405:                   # exists: an earlier, unfinished upload of this file
            try:
                have = {e.name: e.size for e in self.list(folder)}
            except Exception:  # noqa: BLE001
                have = {}
        skip = getattr(progress, "skip", None)
        for n in range(1, count + 1):
            length = min(chunk, size - (n - 1) * chunk)
            name = f"{n:05d}"
            if have.get(name) == length:
                if skip:
                    skip(length)
                continue
            fp.seek(start + (n - 1) * chunk)
            self._put(f"{folder}/{name}", self._stream(fp, length, progress), length, dest)
        headers = {**dest, "Overwrite": "T"}
        if mtime:
            headers["X-OC-Mtime"] = str(mtime)
        self._req("MOVE", f"{folder}/.file", headers=headers, timeout=httpx.Timeout(None, connect=15))
        return True

    def write_bytes(self, path: str, data: bytes, atomic: bool = True) -> None:
        # a WebDAV PUT replaces the file in one step on the server: no temp file needed
        import io
        self.upload(io.BytesIO(data), path)
