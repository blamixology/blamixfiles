"""OneDrive (Microsoft Graph), personal and work/school accounts. Paths are addressed directly
(/me/drive/root:/a/b:); deletes go to the OneDrive recycle bin. Files up to 4 MB go up in one request,
bigger ones through an upload session (pieces of 10 MB, a multiple of 320 KiB as Graph requires)."""
from __future__ import annotations

import urllib.parse

from ..vfs import BackendError, Capabilities, Entry, ProgressFn
from .cloud import CloudBackend

SMALL = 4 * 1024 * 1024
PIECE = 32 * 320 * 1024          # 10 MiB, a multiple of 320 KiB


class OneDriveBackend(CloudBackend):
    name = "onedrive"
    caps = Capabilities(resume=False, rename=True, chmod=False, set_mtime=True, symlinks=False,
                        atomic_replace=True)
    api_env = "BLAMIXFILES_ONEDRIVE_API"
    api_default = "https://graph.microsoft.com/v1.0"

    def check(self) -> None:
        self.request("GET", "/me/drive/root", params={"select": "id"})

    def error(self, r):
        try:
            err = r.json().get("error", {})
            code, msg = err.get("code", ""), err.get("message", "")
        except (ValueError, AttributeError):
            code, msg = "", r.text[:200]
        friendly = {"itemNotFound": "Not found", "nameAlreadyExists": "Something with that name already exists",
                    "accessDenied": "Access denied", "quotaLimitReached": "Your OneDrive is full",
                    "InvalidAuthenticationToken": "The sign-in expired: sign in again"}
        return BackendError(friendly.get(code) or f"OneDrive: {msg or code or r.status_code}")

    @staticmethod
    def _item(path: str) -> str:
        if path in ("", "/"):
            return "/me/drive/root"
        return "/me/drive/root:" + urllib.parse.quote("/" + path.strip("/")) + ":"

    def _entry(self, m: dict, parent: str) -> Entry:
        is_dir = "folder" in m
        when = (m.get("fileSystemInfo") or {}).get("lastModifiedDateTime") or m.get("lastModifiedDateTime", "")
        return Entry(name=m["name"], path=self.join(parent, m["name"]), is_dir=is_dir,
                     size=0 if is_dir else int(m.get("size", 0)), mtime=self.iso(when))

    # ---- listing
    def list(self, path: str) -> list[Entry]:
        url = (self._item(path) + ("/children" if path in ("", "/") else "/children"))
        out = []
        params = {"$top": "1000"}
        while url:
            body = self.request("GET", url, params=params).json()
            out += [self._entry(m, path or "/") for m in body.get("value", [])]
            url, params = body.get("@odata.nextLink", ""), None
        return out

    def stat(self, path: str) -> Entry | None:
        if path in ("", "/"):
            return Entry(name="/", path="/", is_dir=True)
        try:
            m = self.request("GET", self._item(path)).json()
        except BackendError as e:
            if str(e) == "Not found":
                return None
            raise
        return self._entry(m, self.parent(path))

    # ---- changes
    def mkdir(self, path: str) -> None:
        self.request("POST", self._item(self.parent(path)) + "/children",
                     json={"name": self.basename(path), "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})

    def remove(self, path: str) -> None:
        self.request("DELETE", self._item(path))

    rmdir = remove

    def remove_tree(self, path: str) -> None:
        self.remove(path)

    def rename(self, src: str, dst: str) -> None:
        body = {"name": self.basename(dst)}
        if self.parent(src) != self.parent(dst):
            parent = self.parent(dst)
            body["parentReference"] = {"path": "/drive/root" + ("" if parent == "/" else ":" + parent)}
        self.request("PATCH", self._item(src), json=body,
                     params={"@microsoft.graph.conflictBehavior": "replace"})

    def set_mtime(self, path: str, mtime: float) -> None:
        self.request("PATCH", self._item(path), json={"fileSystemInfo": {"lastModifiedDateTime": self.to_iso(mtime)}})

    # ---- data
    def download(self, path: str, fp, offset: int = 0, progress: ProgressFn | None = None) -> None:
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with self.stream("GET", self._item(path) + "/content", headers=headers) as r:
            for chunk in r.iter_bytes(1024 * 1024):
                fp.write(chunk)
                if progress:
                    progress(len(chunk))

    def upload(self, fp, path: str, offset: int = 0, progress: ProgressFn | None = None) -> None:
        first = fp.read(SMALL + 1)
        if len(first) <= SMALL:
            self.request("PUT", self._item(path) + "/content", content=first,
                         headers={"Content-Type": "application/octet-stream"})
            if progress and first:
                progress(len(first))
            return
        import os
        try:
            total = os.fstat(fp.fileno()).st_size - fp.tell() + len(first)
        except (AttributeError, OSError, ValueError):
            rest = fp.read()
            total = len(first) + len(rest)
            import io
            fp = io.BytesIO(rest)
        sess = self.request("POST", self._item(path) + "/createUploadSession",
                            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}}).json()
        url = sess["uploadUrl"]
        sent = 0
        pending = first
        while sent < total:
            if len(pending) < PIECE:
                pending += fp.read(PIECE - len(pending))
            chunk, pending = pending[:PIECE], pending[PIECE:]
            end = sent + len(chunk) - 1
            # the upload URL is pre-authorised: no Authorization header (Graph rejects one)
            r = self.http.put(url, content=chunk, headers={"Content-Range": f"bytes {sent}-{end}/{total}"})
            if r.status_code not in (200, 201, 202):
                raise self.error(r)
            sent += len(chunk)
            if progress:
                progress(len(chunk))
