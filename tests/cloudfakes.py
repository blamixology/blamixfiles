"""In-process fakes of the Google Drive, Dropbox and OneDrive APIs (only what BlamixFiles uses), plus
their OAuth token endpoints. Each keeps an in-memory file tree, checks the bearer token on every call,
and can expire tokens to test the refresh path."""
from __future__ import annotations

import itertools
import json
import re
import threading
import time
import urllib.parse

from werkzeug.serving import make_server
from werkzeug.wrappers import Request, Response


class Node:
    _ids = itertools.count(1)

    def __init__(self, name: str, folder: bool, parent=None, data: bytes = b""):
        self.id = f"id{next(self._ids)}"
        self.name, self.folder, self.parent, self.data = name, folder, parent, data
        self.mtime = time.time()
        self.trashed = False
        self.children: list[Node] = []

    def path(self) -> str:
        parts, n = [], self
        while n.parent is not None:
            parts.append(n.name)
            n = n.parent
        return "/" + "/".join(reversed(parts))


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def parse_iso(s: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class FakeCloud:
    """Base: a tree, tokens, and a werkzeug server on a free port."""

    def __init__(self):
        self.root = Node("", True)
        self.tokens = {"tok-0"}
        self.refresh_tokens = {"refresh-0"}
        self.n = 0
        self.calls: list[str] = []
        self.uploads: dict[str, dict] = {}
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.port = self.server.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        self.lock = threading.RLock()

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()

    # ---- tree helpers
    def find(self, path: str):
        n = self.root
        for part in [p for p in path.split("/") if p]:
            n = next((c for c in n.children if c.name == part and not c.trashed), None)
            if n is None:
                return None
        return n

    def by_id(self, nid: str, start=None):
        start = start or self.root
        if start.id == nid or (nid == "root" and start is self.root):
            return start
        for c in start.children:
            hit = self.by_id(nid, c)
            if hit is not None:
                return hit
        return None

    def add(self, path: str, data: bytes | None = None) -> Node:
        parent = self.find(path.rsplit("/", 1)[0] or "/")
        n = Node(path.rsplit("/", 1)[1], data is None, parent, data or b"")
        parent.children.append(n)
        return n

    def expire(self) -> None:
        self.tokens.clear()

    # ---- wsgi
    def app(self, environ, start_response):
        req = Request(environ)
        with self.lock:
            self.calls.append(f"{req.method} {req.path}")
            if req.path == "/token":
                resp = self.token(req)
            else:
                auth = req.headers.get("Authorization", "")
                if not self.public(req) and auth.removeprefix("Bearer ") not in self.tokens:
                    resp = Response(json.dumps(self.auth_error()), 401, content_type="application/json")
                else:
                    resp = self.handle(req)
        return resp(environ, start_response)

    def public(self, req) -> bool:
        return False

    def auth_error(self) -> dict:
        return {"error": "invalid token"}

    def token(self, req) -> Response:
        f = req.form
        if f.get("grant_type") == "authorization_code" and f.get("code") == "good-code" and f.get("code_verifier"):
            pass
        elif f.get("grant_type") == "refresh_token" and f.get("refresh_token") in self.refresh_tokens:
            pass
        else:
            return Response(json.dumps({"error": "invalid_grant", "error_description": "bad code"}), 400,
                            content_type="application/json")
        self.n += 1
        tok = f"tok-{self.n}"
        self.tokens.add(tok)
        new_refresh = f"refresh-{self.n}"
        self.refresh_tokens.add(new_refresh)
        return Response(json.dumps({"access_token": tok, "refresh_token": new_refresh, "expires_in": 3600}),
                        content_type="application/json")

    @staticmethod
    def ranged(req, data: bytes) -> Response:
        m = re.match(r"bytes=(\d+)-", req.headers.get("Range", ""))
        if m:
            return Response(data[int(m.group(1)):], 206)
        return Response(data, 200)


# ======================================================================= Dropbox
class FakeDropbox(FakeCloud):
    PAGE = 3

    def meta(self, n: Node) -> dict:
        if n.folder:
            return {".tag": "folder", "name": n.name, "path_display": n.path()}
        return {".tag": "file", "name": n.name, "path_display": n.path(), "size": len(n.data),
                "client_modified": iso(n.mtime), "server_modified": iso(n.mtime)}

    def err(self, summary: str, status: int = 409) -> Response:
        return Response(json.dumps({"error_summary": summary + "/..", "error": {}}), status,
                        content_type="application/json")

    def ok(self, body) -> Response:
        return Response(json.dumps(body), content_type="application/json")

    def handle(self, req) -> Response:
        p = req.path
        arg = json.loads(req.headers.get("Dropbox-API-Arg", "null") or "null")
        body = req.get_json(silent=True) or {}
        if p == "/2/users/get_current_account":
            return self.ok({"email": "me@example.com"})
        if p == "/2/files/list_folder":
            n = self.find(body["path"] or "/")
            if n is None:
                return self.err("path/not_found")
            items = [self.meta(c) for c in n.children if not c.trashed]
            return self.ok({"entries": items[:self.PAGE], "has_more": len(items) > self.PAGE,
                            "cursor": json.dumps([body["path"], self.PAGE])})
        if p == "/2/files/list_folder/continue":
            path, start = json.loads(body["cursor"])
            n = self.find(path or "/")
            items = [self.meta(c) for c in n.children if not c.trashed]
            return self.ok({"entries": items[start:start + self.PAGE], "has_more": len(items) > start + self.PAGE,
                            "cursor": json.dumps([path, start + self.PAGE])})
        if p == "/2/files/get_metadata":
            n = self.find(body["path"])
            return self.ok(self.meta(n)) if n else self.err("path/not_found")
        if p == "/2/files/create_folder_v2":
            if self.find(body["path"]):
                return self.err("path/conflict/folder")
            return self.ok({"metadata": self.meta(self.add(body["path"]))})
        if p == "/2/files/delete_v2":
            n = self.find(body["path"])
            if n is None:
                return self.err("path_lookup/not_found")
            n.parent.children.remove(n)
            return self.ok({"metadata": self.meta(n)})
        if p == "/2/files/move_v2":
            n, dst = self.find(body["from_path"]), body["to_path"]
            if n is None:
                return self.err("from_lookup/not_found")
            if self.find(dst):
                return self.err("to/conflict/file")
            n.parent.children.remove(n)
            parent = self.find(dst.rsplit("/", 1)[0] or "/")
            n.parent, n.name = parent, dst.rsplit("/", 1)[1]
            parent.children.append(n)
            return self.ok({"metadata": self.meta(n)})
        if p == "/2/files/download":
            n = self.find(arg["path"])
            return self.ranged(req, n.data) if n else self.err("path/not_found")
        if p == "/2/files/upload":
            return self.ok(self.meta(self._write(arg["path"], req.get_data())))
        if p == "/2/files/upload_session/start":
            sid = f"s{len(self.uploads)}"
            self.uploads[sid] = {"data": req.get_data()}
            return self.ok({"session_id": sid})
        if p == "/2/files/upload_session/append_v2":
            up = self.uploads[arg["cursor"]["session_id"]]
            assert arg["cursor"]["offset"] == len(up["data"]), "wrong offset"
            up["data"] += req.get_data()
            return self.ok(None)
        if p == "/2/files/upload_session/finish":
            up = self.uploads.pop(arg["cursor"]["session_id"])
            assert arg["cursor"]["offset"] == len(up["data"]), "wrong offset"
            return self.ok(self.meta(self._write(arg["commit"]["path"], up["data"] + req.get_data())))
        return Response("not found", 404)

    def _write(self, path: str, data: bytes) -> Node:
        n = self.find(path)
        if n is None:
            n = self.add(path, data)
        n.data, n.mtime = data, time.time()
        return n


# ======================================================================= OneDrive (Graph)
class FakeOneDrive(FakeCloud):
    def meta(self, n: Node) -> dict:
        m = {"id": n.id, "name": n.name, "size": 0 if n.folder else len(n.data),
             "lastModifiedDateTime": iso(n.mtime), "fileSystemInfo": {"lastModifiedDateTime": iso(n.mtime)}}
        m["folder" if n.folder else "file"] = {}
        return m

    def auth_error(self) -> dict:
        return {"error": {"code": "InvalidAuthenticationToken", "message": "expired"}}

    def public(self, req) -> bool:
        return req.path.startswith(("/dl/", "/upload/"))

    def err(self, code: str, status: int = 404) -> Response:
        return Response(json.dumps({"error": {"code": code, "message": code}}), status, content_type="application/json")

    def ok(self, body, status=200) -> Response:
        return Response(json.dumps(body), status, content_type="application/json")

    def handle(self, req) -> Response:
        p = urllib.parse.unquote(req.path)
        if p.startswith("/dl/"):
            n = self.by_id(p[4:])
            return self.ranged(req, n.data)
        if p.startswith("/upload/"):
            up = self.uploads[p[8:]]
            m = re.match(r"bytes (\d+)-(\d+)/(\d+)", req.headers["Content-Range"])
            start, end, total = map(int, m.groups())
            assert start == len(up["data"]), "wrong offset"
            assert (end - start + 1) % (320 * 1024) == 0 or end + 1 == total, "piece not a multiple of 320 KiB"
            up["data"] += req.get_data()
            if len(up["data"]) < total:
                return self.ok({"nextExpectedRanges": [f"{len(up['data'])}-"]}, 202)
            del self.uploads[p[8:]]
            return self.ok(self.meta(self._write(up["path"], up["data"])), 201)
        m = re.match(r"^/me/drive/root(?::(?P<path>.*?):)?(?P<rest>/children|/content|/createUploadSession)?$", p)
        if not m:
            return Response("no route " + p, 404)
        path, rest = m.group("path") or "/", m.group("rest") or ""
        n = self.find(path)
        if rest == "/children" and req.method == "GET":
            if n is None:
                return self.err("itemNotFound")
            return self.ok({"value": [self.meta(c) for c in n.children if not c.trashed]})
        if rest == "/children" and req.method == "POST":
            body = req.get_json()
            if self.find(path.rstrip("/") + "/" + body["name"]):
                return self.err("nameAlreadyExists", 409)
            return self.ok(self.meta(self.add(path.rstrip("/") + "/" + body["name"])), 201)
        if rest == "/content" and req.method == "GET":
            return Response("", 302, headers={"Location": f"{self.base}/dl/{n.id}"}) if n else self.err("itemNotFound")
        if rest == "/content" and req.method == "PUT":
            return self.ok(self.meta(self._write(path, req.get_data())), 201)
        if rest == "/createUploadSession":
            sid = f"u{len(self.uploads)}{time.time_ns()}"
            self.uploads[sid] = {"path": path, "data": b""}
            return self.ok({"uploadUrl": f"{self.base}/upload/{sid}"})
        if n is None:
            return self.err("itemNotFound")
        if req.method == "GET":
            return self.ok(self.meta(n))
        if req.method == "DELETE":
            n.parent.children.remove(n)
            return Response("", 204)
        if req.method == "PATCH":
            body = req.get_json()
            if "fileSystemInfo" in body:
                n.mtime = parse_iso(body["fileSystemInfo"]["lastModifiedDateTime"])
            if "parentReference" in body:
                ref = body["parentReference"]["path"].removeprefix("/drive/root").lstrip(":") or "/"
                new_parent = self.find(ref)
                n.parent.children.remove(n)
                n.parent = new_parent
                new_parent.children.append(n)
            if "name" in body:
                old = next((c for c in n.parent.children if c.name == body["name"] and c is not n), None)
                if old is not None:                                    # conflictBehavior=replace
                    n.parent.children.remove(old)
                n.name = body["name"]
            return self.ok(self.meta(n))
        return Response("method", 405)

    def _write(self, path: str, data: bytes) -> Node:
        n = self.find(path)
        if n is None:
            n = self.add(path, data)
        n.data, n.mtime = data, time.time()
        return n


# ======================================================================= Google Drive
class FakeDrive(FakeCloud):
    FOLDER = "application/vnd.google-apps.folder"

    def meta(self, n: Node) -> dict:
        m = {"id": "root" if n is self.root else n.id, "name": n.name,
             "mimeType": self.FOLDER if n.folder else getattr(n, "mime", "application/octet-stream"),
             "modifiedTime": iso(n.mtime)}
        if not n.folder:
            m["size"] = str(len(n.data))
        return m

    def public(self, req) -> bool:
        return req.path.startswith("/upload-session/")

    def err(self, reason: str, status: int = 404) -> Response:
        return Response(json.dumps({"error": {"errors": [{"reason": reason}], "message": reason}}), status,
                        content_type="application/json")

    def ok(self, body, status=200, headers=None) -> Response:
        return Response(json.dumps(body), status, content_type="application/json", headers=headers or {})

    def handle(self, req) -> Response:
        p = req.path
        a = req.args
        if p == "/drive/v3/about":
            return self.ok({"user": {"emailAddress": "me@example.com"}})
        if p == "/drive/v3/files" and req.method == "GET":
            q = a.get("q", "")
            pm = re.search(r"'([^']+)' in parents", q)
            parent = self.by_id(pm.group(1))
            kids = [c for c in parent.children if not c.trashed] if parent else []
            nm = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
            if nm:
                name = nm.group(1).replace("\\'", "'").replace("\\\\", "\\")
                kids = [c for c in kids if c.name == name]
            return self.ok({"files": [self.meta(c) for c in kids]})
        if p == "/drive/v3/files" and req.method == "POST":
            body = req.get_json()
            parent = self.by_id(body["parents"][0])
            n = Node(body["name"], body.get("mimeType") == self.FOLDER, parent)
            parent.children.append(n)
            return self.ok(self.meta(n))
        m = re.match(r"^/drive/v3/files/([^/]+)$", p)
        if m:
            n = self.by_id(m.group(1))
            if n is None or n.trashed:
                return self.err("notFound")
            if req.method == "GET" and a.get("alt") == "media":
                if getattr(n, "mime", "").startswith("application/vnd.google-apps."):
                    return self.err("fileNotDownloadable", 403)
                return self.ranged(req, n.data)
            if req.method == "GET":
                return self.ok(dict(self.meta(n), trashed=n.trashed))
            if req.method == "PATCH":
                body = req.get_json() or {}
                if body.get("trashed"):
                    n.trashed = True
                if "name" in body:
                    n.name = body["name"]
                if "modifiedTime" in body:
                    n.mtime = parse_iso(body["modifiedTime"])
                if a.get("addParents"):
                    n.parent.children.remove(n)
                    n.parent = self.by_id(a["addParents"])
                    n.parent.children.append(n)
                return self.ok(self.meta(n))
        if p == "/upload/drive/v3/files" and req.method == "POST":
            body = req.get_json()
            sid = f"g{len(self.uploads)}{time.time_ns()}"
            self.uploads[sid] = {"new": body, "data": b"", "total": int(req.headers["X-Upload-Content-Length"])}
            return self.ok({}, headers={"Location": f"{self.base}/upload-session/{sid}"})
        m = re.match(r"^/upload/drive/v3/files/([^/]+)$", p)
        if m and req.method == "PATCH":
            sid = f"g{len(self.uploads)}{time.time_ns()}"
            self.uploads[sid] = {"id": m.group(1), "data": b"", "total": int(req.headers["X-Upload-Content-Length"])}
            return self.ok({}, headers={"Location": f"{self.base}/upload-session/{sid}"})
        if p.startswith("/upload-session/"):
            up = self.uploads[p.rsplit("/", 1)[1]]
            rng = req.headers["Content-Range"]
            chunk = req.get_data()
            if not rng.startswith("bytes */"):
                start, end = map(int, re.match(r"bytes (\d+)-(\d+)/", rng).groups())
                assert start == len(up["data"]), "wrong offset"
                assert len(chunk) % (256 * 1024) == 0 or len(up["data"]) + len(chunk) == up["total"]
                up["data"] += chunk
            if len(up["data"]) < up["total"]:
                return Response("", 308, headers={"Range": f"bytes=0-{len(up['data']) - 1}"})
            if "id" in up:
                n = self.by_id(up["id"])
            else:
                parent = self.by_id(up["new"]["parents"][0])
                n = Node(up["new"]["name"], False, parent)
                parent.children.append(n)
            n.data, n.mtime = up["data"], time.time()
            return self.ok(self.meta(n))
        return Response("no route " + p, 404)
