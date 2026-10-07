"""Google Drive (API v3), "My Drive". Drive works with ids, not paths, so paths are looked up one
folder at a time and remembered. Two things Drive allows that file systems don't:
  * two files with the same name in one folder: the oldest one is used;
  * Google Docs / Sheets / Slides have no bytes to download: they're shown, but skipped with a message.
Deleting moves to the Drive trash. Uploads use resumable sessions (8 MB pieces)."""
from __future__ import annotations

import os

from ..vfs import BackendError, Capabilities, Entry, ProgressFn
from .cloud import CloudBackend

FOLDER = "application/vnd.google-apps.folder"
NATIVE = "application/vnd.google-apps."
PIECE = 32 * 256 * 1024             # 8 MiB, a multiple of 256 KiB as Drive requires
FIELDS = "id,name,mimeType,size,modifiedTime"


def _q(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


class GDriveBackend(CloudBackend):
    name = "gdrive"
    caps = Capabilities(resume=False, rename=True, chmod=False, set_mtime=True, symlinks=False,
                        atomic_replace=False)
    api_env = "BLAMIXFILES_GDRIVE_API"
    api_default = "https://www.googleapis.com"

    def __init__(self, site):
        super().__init__(site)
        self.ids: dict[str, dict] = {"/": {"id": "root", "mimeType": FOLDER, "name": "/"}}

    def check(self) -> None:
        self.request("GET", "/drive/v3/about", params={"fields": "user(emailAddress)"})

    def error(self, r):
        try:
            err = r.json().get("error", {})
            reason = ((err.get("errors") or [{}])[0]).get("reason", "") or err.get("status", "")
            msg = err.get("message", "")
        except (ValueError, AttributeError):
            reason, msg = "", r.text[:200]
        friendly = {"notFound": "Not found", "storageQuotaExceeded": "Your Google Drive is full",
                    "insufficientFilePermissions": "Access denied", "forbidden": "Access denied",
                    "authError": "The sign-in expired: sign in again"}
        return BackendError(friendly.get(reason) or f"Google Drive: {msg or reason or r.status_code}")

    # ---- path -> file
    def _meta(self, path: str) -> dict | None:
        path = self.normalize(path)
        if path in self.ids:
            return self.ids[path]
        parent = self._meta(self.parent(path))
        if parent is None or parent["mimeType"] != FOLDER:
            return None
        name = self.basename(path)
        r = self.request("GET", "/drive/v3/files", params={
            "q": f"name = '{_q(name)}' and '{parent['id']}' in parents and trashed = false",
            "fields": f"files({FIELDS})", "orderBy": "createdTime", "pageSize": "10"})
        files = r.json().get("files", [])
        if not files:
            return None
        self.ids[path] = files[0]
        return files[0]

    def _need(self, path: str) -> dict:
        m = self._meta(path)
        if m is None:
            raise BackendError("Not found")
        return m

    def _forget(self, path: str) -> None:
        path = self.normalize(path)
        for p in [p for p in self.ids if p == path or p.startswith(path.rstrip("/") + "/")]:
            if p != "/":
                self.ids.pop(p, None)

    def _entry(self, m: dict, path: str) -> Entry:
        is_dir = m.get("mimeType") == FOLDER
        return Entry(name=m["name"], path=path, is_dir=is_dir, size=0 if is_dir else int(m.get("size", 0) or 0),
                     mtime=self.iso(m.get("modifiedTime", "")))

    # ---- listing
    def list(self, path: str) -> list[Entry]:
        folder = self._need(path)
        out, seen = [], set()
        token = None
        while True:
            params = {"q": f"'{folder['id']}' in parents and trashed = false", "pageSize": "1000",
                      "fields": f"nextPageToken,files({FIELDS})", "orderBy": "createdTime"}
            if token:
                params["pageToken"] = token
            body = self.request("GET", "/drive/v3/files", params=params).json()
            for m in body.get("files", []):
                if m["name"] in seen or "/" in m["name"]:
                    continue                            # duplicate name / not addressable as a path
                seen.add(m["name"])
                child = self.join(path, m["name"])
                self.ids[self.normalize(child)] = m
                out.append(self._entry(m, child))
            token = body.get("nextPageToken")
            if not token:
                return out

    def stat(self, path: str) -> Entry | None:
        """Always asks Drive (by the remembered id): sizes and times change behind our back."""
        path = self.normalize(path)
        m = self._meta(path)
        if m is None:
            return None
        if m["id"] != "root":
            try:
                m = self.request("GET", f"/drive/v3/files/{m['id']}", params={"fields": FIELDS + ",trashed"}).json()
            except BackendError as e:
                if str(e) != "Not found":
                    raise
                m = {"trashed": True}
            if m.get("trashed"):
                self._forget(path)
                m = self._meta(path)                     # (maybe another file has that name now)
                if m is None:
                    return None
            else:
                self.ids[path] = m
        return self._entry(m, path)

    # ---- changes
    def mkdir(self, path: str) -> None:
        parent = self._need(self.parent(path))
        m = self.request("POST", "/drive/v3/files", params={"fields": FIELDS},
                         json={"name": self.basename(path), "mimeType": FOLDER, "parents": [parent["id"]]}).json()
        self.ids[self.normalize(path)] = m

    def remove(self, path: str) -> None:
        m = self._need(path)
        self.request("PATCH", f"/drive/v3/files/{m['id']}", json={"trashed": True})
        self._forget(path)

    rmdir = remove

    def remove_tree(self, path: str) -> None:
        self.remove(path)                               # trashing a folder takes its contents along

    def rename(self, src: str, dst: str) -> None:
        m = self._need(src)
        params = {"fields": FIELDS}
        if self.parent(src) != self.parent(dst):
            params["addParents"] = self._need(self.parent(dst))["id"]
            params["removeParents"] = self._need(self.parent(src))["id"]
        new = self.request("PATCH", f"/drive/v3/files/{m['id']}", params=params,
                           json={"name": self.basename(dst)}).json()
        self._forget(src)
        self.ids[self.normalize(dst)] = new

    def set_mtime(self, path: str, mtime: float) -> None:
        m = self._need(path)
        self.request("PATCH", f"/drive/v3/files/{m['id']}", json={"modifiedTime": self.to_iso(mtime)})

    # ---- data
    def download(self, path: str, fp, offset: int = 0, progress: ProgressFn | None = None) -> None:
        m = self._need(path)
        if m.get("mimeType", "").startswith(NATIVE):
            raise BackendError(f"{m['name']} is a Google Docs/Sheets/Slides file: export it from Drive first")
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with self.stream("GET", f"/drive/v3/files/{m['id']}", params={"alt": "media"}, headers=headers) as r:
            for chunk in r.iter_bytes(1024 * 1024):
                fp.write(chunk)
                if progress:
                    progress(len(chunk))

    def upload(self, fp, path: str, offset: int = 0, progress: ProgressFn | None = None) -> None:
        existing = self._meta(path)
        try:
            total = os.fstat(fp.fileno()).st_size - fp.tell()
        except (AttributeError, OSError, ValueError):
            import io
            data = fp.read()
            total, fp = len(data), io.BytesIO(data)
        if existing is not None and existing.get("mimeType") == FOLDER:
            raise BackendError("A folder with that name is in the way")
        if existing is not None:
            r = self.request("PATCH", f"/upload/drive/v3/files/{existing['id']}",
                             params={"uploadType": "resumable", "fields": FIELDS},
                             headers={"X-Upload-Content-Length": str(total)}, json={})
        else:
            parent = self._need(self.parent(path))
            r = self.request("POST", "/upload/drive/v3/files", params={"uploadType": "resumable", "fields": FIELDS},
                             headers={"X-Upload-Content-Length": str(total)},
                             json={"name": self.basename(path), "parents": [parent["id"]]})
        url = r.headers["Location"]
        sent = 0
        meta = None
        while True:
            chunk = fp.read(PIECE)
            if not chunk and sent:
                break
            end = sent + len(chunk) - 1
            rng = f"bytes {sent}-{end}/{total}" if chunk else f"bytes */{total}"
            resp = self.http.put(url, content=chunk, headers={"Content-Range": rng})
            if resp.status_code in (200, 201):
                meta = resp.json()
            elif resp.status_code != 308:
                raise self.error(resp)
            sent += len(chunk)
            if progress and chunk:
                progress(len(chunk))
            if meta is not None or not chunk:
                break
        if meta is not None:
            self.ids[self.normalize(path)] = meta
