"""WebDAV (Nextcloud, ownCloud, Apache mod_dav, NAS boxes, …) over HTTP or HTTPS.

Paths are the decoded URL paths on the server (e.g. /remote.php/dav/files/mike/Photos);
they're percent-encoded per request. Self-signed HTTPS certificates can be pinned by
fingerprint, like FTPS.
"""
from __future__ import annotations

import hashlib
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
            raise BackendError("WebDAV can't resume uploads")
        start = fp.tell()
        fp.seek(0, 2)
        size = fp.tell() - start
        fp.seek(start)

        def body() -> Iterator[bytes]:
            while chunk := fp.read(BLOCK):
                yield chunk
                if progress:
                    progress(len(chunk))
        headers = {"Content-Length": str(size), "Content-Type": "application/octet-stream"}
        self._req("PUT", path, content=body(), headers=headers, timeout=httpx.Timeout(None, connect=15))

    def write_bytes(self, path: str, data: bytes, atomic: bool = True) -> None:
        # a WebDAV PUT replaces the file in one step on the server: no temp file needed
        import io
        self.upload(io.BytesIO(data), path)
