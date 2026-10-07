"""Dropbox (API v2). Paths map one to one: "/" is the Dropbox root ("" in the API).
Big files go up in an upload session (8 MB pieces); small ones in a single request."""
from __future__ import annotations

import json

from ..vfs import BackendError, Capabilities, Entry, ProgressFn
from .cloud import CHUNK, CloudBackend

SINGLE_LIMIT = 16 * 1024 * 1024


class DropboxBackend(CloudBackend):
    name = "dropbox"
    caps = Capabilities(resume=False, rename=True, chmod=False, set_mtime=False, symlinks=False,
                        atomic_replace=False)
    api_env = "BLAMIXFILES_DROPBOX_API"
    api_default = "https://api.dropboxapi.com"

    def __init__(self, site):
        super().__init__(site)
        import os
        self.content = os.environ.get("BLAMIXFILES_DROPBOX_CONTENT", "https://content.dropboxapi.com").rstrip("/")

    def check(self) -> None:
        self.request("POST", "/2/users/get_current_account")

    def error(self, r):
        try:
            body = r.json()
            summary = body.get("error_summary", "") or str(body.get("error", ""))
        except ValueError:
            summary = r.text[:200]
        for key, msg in (("not_found", "Not found"), ("conflict", "Something with that name already exists"),
                         ("insufficient_space", "Your Dropbox is full"), ("no_write_permission", "Access denied"),
                         ("expired_access_token", "The sign-in expired: sign in again")):
            if key in summary:
                return BackendError(msg)
        return BackendError(f"Dropbox: {summary or r.status_code}")

    @staticmethod
    def _p(path: str) -> str:
        return "" if path in ("", "/") else "/" + path.strip("/")

    def _entry(self, m: dict, parent: str | None = None) -> Entry:
        is_dir = m.get(".tag") == "folder"
        path = m.get("path_display") or self.join(parent or "/", m["name"])
        return Entry(name=m["name"], path=path, is_dir=is_dir, size=0 if is_dir else int(m.get("size", 0)),
                     mtime=self.iso(m.get("client_modified") or m.get("server_modified", "")))

    # ---- listing
    def list(self, path: str) -> list[Entry]:
        r = self.request("POST", "/2/files/list_folder", json={"path": self._p(path), "limit": 2000})
        body = r.json()
        out = [self._entry(m, path) for m in body["entries"] if m.get(".tag") in ("file", "folder")]
        while body.get("has_more"):
            body = self.request("POST", "/2/files/list_folder/continue", json={"cursor": body["cursor"]}).json()
            out += [self._entry(m, path) for m in body["entries"] if m.get(".tag") in ("file", "folder")]
        return out

    def stat(self, path: str) -> Entry | None:
        if path in ("", "/"):
            return Entry(name="/", path="/", is_dir=True)
        try:
            m = self.request("POST", "/2/files/get_metadata", json={"path": self._p(path)}).json()
        except BackendError as e:
            if str(e) == "Not found":
                return None
            raise
        return self._entry(m)

    # ---- changes
    def mkdir(self, path: str) -> None:
        self.request("POST", "/2/files/create_folder_v2", json={"path": self._p(path), "autorename": False})

    def remove(self, path: str) -> None:
        self.request("POST", "/2/files/delete_v2", json={"path": self._p(path)})

    rmdir = remove

    def remove_tree(self, path: str) -> None:
        self.remove(path)                               # one call deletes a whole folder

    def rename(self, src: str, dst: str) -> None:
        self.request("POST", "/2/files/move_v2", json={"from_path": self._p(src), "to_path": self._p(dst),
                                                       "autorename": False})

    # ---- data
    def download(self, path: str, fp, offset: int = 0, progress: ProgressFn | None = None) -> None:
        headers = {"Dropbox-API-Arg": json.dumps({"path": self._p(path)})}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        with self.stream("POST", self.content + "/2/files/download", headers=headers) as r:
            for chunk in r.iter_bytes(1024 * 1024):
                fp.write(chunk)
                if progress:
                    progress(len(chunk))

    def upload(self, fp, path: str, offset: int = 0, progress: ProgressFn | None = None) -> None:
        commit = {"path": self._p(path), "mode": "overwrite", "mute": True}
        first = fp.read(SINGLE_LIMIT + 1)
        if len(first) <= SINGLE_LIMIT:
            self.request("POST", self.content + "/2/files/upload", content=first,
                         headers={"Dropbox-API-Arg": json.dumps(commit), "Content-Type": "application/octet-stream"})
            if progress and first:
                progress(len(first))
            return
        # upload session: start with the first piece, append the middle ones, finish with the last
        sid = None
        sent = 0
        pending = first
        while True:
            chunk, pending = pending[:CHUNK], pending[CHUNK:]
            if len(pending) < CHUNK:
                pending += fp.read(CHUNK)
            last = not pending
            if sid is None:
                r = self._session("start", {"close": False}, chunk)
                sid = r.json()["session_id"]
            elif not last:
                self._session("append_v2", {"cursor": {"session_id": sid, "offset": sent}}, chunk)
            else:
                self._session("finish", {"cursor": {"session_id": sid, "offset": sent}, "commit": commit}, chunk)
                if progress:
                    progress(len(chunk))
                return
            sent += len(chunk)
            if progress:
                progress(len(chunk))
            if last:                                    # it all fit in the start call
                self._session("finish", {"cursor": {"session_id": sid, "offset": sent}, "commit": commit}, b"")
                return

    def _session(self, step: str, arg: dict, data: bytes):
        return self.request("POST", self.content + "/2/files/upload_session/" + step, content=data,
                            headers={"Dropbox-API-Arg": json.dumps(arg), "Content-Type": "application/octet-stream"})
