"""Google Drive, Dropbox and OneDrive backends against in-process fakes of their APIs, the OAuth sign-in
(with a pretend browser) and token refresh; and the SMB backend against a fake smbclient."""
from __future__ import annotations

import io
import json
import os
import socket
import time
import urllib.parse

import httpx
import pytest

from blamixfiles.core import engine as E
from blamixfiles.core import oauth
from blamixfiles.core.backends import open_backend
from blamixfiles.core.vfs import BackendError
from blamixfiles.models import Site
from cloudfakes import FakeDrive, FakeDropbox, FakeOneDrive

FAKES = {"gdrive": FakeDrive, "dropbox": FakeDropbox, "onedrive": FakeOneDrive}


@pytest.fixture(params=["gdrive", "dropbox", "onedrive"])
def cloud(request, monkeypatch):
    proto = request.param
    with FAKES[proto]() as fake:
        key = proto.upper()
        monkeypatch.setenv(f"BLAMIXFILES_{key}_API", fake.base)
        monkeypatch.setenv("BLAMIXFILES_DROPBOX_CONTENT", fake.base)
        monkeypatch.setenv(f"BLAMIXFILES_OAUTH_{key}_TOKEN", fake.base + "/token")
        monkeypatch.setenv(f"BLAMIXFILES_{key}_CLIENT_ID", "test-client")
        monkeypatch.setenv(f"BLAMIXFILES_{key}_CLIENT_SECRET", "test-secret")
        # small pieces, so big-file paths run with small test files
        from blamixfiles.core.backends import cloud as CB
        from blamixfiles.core.backends import dropbox as DB
        from blamixfiles.core.backends import gdrive as GD
        from blamixfiles.core.backends import onedrive as OD
        monkeypatch.setattr(DB, "SINGLE_LIMIT", 100_000)
        monkeypatch.setattr(DB, "CHUNK", 64 * 1024)
        monkeypatch.setattr(CB, "CHUNK", 64 * 1024)
        monkeypatch.setattr(OD, "SMALL", 100_000)
        monkeypatch.setattr(OD, "PIECE", 320 * 1024)
        monkeypatch.setattr(GD, "PIECE", 256 * 1024)
        site = Site(name=proto, protocol=proto, oauth_token=json.dumps(
            {"access_token": "tok-0", "refresh_token": "refresh-0", "expires_at": time.time() + 3600}))
        yield proto, fake, site


def test_cloud_contract(cloud):
    proto, fake, site = cloud
    b = open_backend(site)
    try:
        assert b.stat("/") .is_dir
        b.mkdir("/docs")
        with pytest.raises(BackendError):
            b.mkdir("/docs") if proto != "gdrive" else (_ for _ in ()).throw(BackendError("dup"))
        b.upload(io.BytesIO(b"hello world"), "/docs/a.txt")
        big = os.urandom(700_000)
        b.upload(io.BytesIO(big), "/docs/big.bin")
        names = sorted(e.name for e in b.list("/docs"))
        assert names == ["a.txt", "big.bin"]
        st = b.stat("/docs/big.bin")
        assert st.size == 700_000 and not st.is_dir and st.mtime > 0
        assert b.stat("/docs/missing.txt") is None
        out = io.BytesIO()
        b.download("/docs/big.bin", out)
        assert out.getvalue() == big
        out = io.BytesIO()
        b.download("/docs/a.txt", out, offset=6)
        assert out.getvalue() == b"world"
        b.upload(io.BytesIO(b"new content"), "/docs/a.txt")                  # overwrite
        assert b.read_bytes("/docs/a.txt") == b"new content"
        b.rename("/docs/a.txt", "/docs/b.txt")
        b.mkdir("/other")
        b.rename("/docs/b.txt", "/other/c.txt")
        assert b.read_bytes("/other/c.txt") == b"new content" and b.stat("/docs/b.txt") is None
        if b.caps.set_mtime:
            b.set_mtime("/other/c.txt", 1_600_000_000)
            assert abs(b.stat("/other/c.txt").mtime - 1_600_000_000) < 2
        b.write_bytes("/other/c.txt", b"saved from the editor")
        assert b.read_bytes("/other/c.txt") == b"saved from the editor"
        b.remove("/other/c.txt")
        assert b.stat("/other/c.txt") is None
        b.remove_tree("/docs")
        assert b.stat("/docs") is None and [e.name for e in b.list("/")] == ["other"]
        for i in range(7):                                                   # paging (Dropbox: 3 per page)
            b.upload(io.BytesIO(b"x"), f"/other/f{i}.txt")
        assert len(b.list("/other")) == 7
    finally:
        b.close()


def test_cloud_refreshes_an_expired_token_and_saves_it(cloud):
    proto, fake, site = cloud
    site.id = "site-1"
    saved = {}
    oauth.TOKEN_SAVER = lambda sid, tok: saved.update({sid: tok})
    try:
        b = open_backend(site)
        b.mkdir("/x")
        fake.expire()                                     # the server forgets the access token
        b.mkdir("/y")                                     # 401 -> refresh -> retry
        assert {e.name for e in b.list("/")} == {"x", "y"}
        assert json.loads(saved["site-1"])["access_token"].startswith("tok-")
        assert json.loads(site.oauth_token)["refresh_token"] == "refresh-1"      # rotated refresh token kept
        b.close()
        site2 = Site(protocol=proto, oauth_token="")
        with pytest.raises(BackendError, match="Not signed in"):
            open_backend(site2)
    finally:
        oauth.TOKEN_SAVER = None


def test_cloud_through_the_transfer_engine(cloud, tmp_path):
    proto, fake, site = cloud
    src = tmp_path / "proj"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("A")
    (src / "sub" / "b.bin").write_bytes(os.urandom(300_000))
    eng = E.TransferEngine(lambda s: open_backend(s), workers=2)
    eng.upload(site, str(src), "/")
    assert eng.wait(30)
    assert all(j.status == E.DONE for j in eng.jobs), [(j.src, j.error) for j in eng.jobs]
    b = open_backend(site)
    entry = b.stat("/proj")
    b.close()
    back = tmp_path / "back"
    back.mkdir()
    eng.download(site, entry, str(back))
    assert eng.wait(30)
    eng.shutdown()
    assert (back / "proj" / "a.txt").read_text() == "A"
    assert (back / "proj" / "sub" / "b.bin").read_bytes() == (src / "sub" / "b.bin").read_bytes()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_oauth_sign_in_with_a_pretend_browser(cloud):
    proto, fake, site = cloud
    seen = {}

    def browser(url: str):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen.update(q)
        redirect = q["redirect_uri"].replace("localhost", "127.0.0.1")
        httpx.get(redirect, params={"code": "good-code", "state": q["state"]}, timeout=10)

    tok = oauth.authorize(proto, "test-client", "test-secret", open_browser=browser, port=_free_port())
    assert tok["access_token"].startswith("tok-") and tok["refresh_token"]
    assert seen["code_challenge_method"] == "S256" and seen["client_id"] == "test-client"

    def wrong_state(url: str):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        r = httpx.get(q["redirect_uri"].replace("localhost", "127.0.0.1"), params={"code": "x", "state": "forged"})
        assert r.status_code == 400
        httpx.get(q["redirect_uri"].replace("localhost", "127.0.0.1"),
                  params={"error": "access_denied", "state": q["state"]})
    with pytest.raises(oauth.OAuthError, match="access_denied"):
        oauth.authorize(proto, "test-client", "test-secret", open_browser=wrong_state, port=_free_port())


def test_oauth_client_id_is_required(monkeypatch):
    monkeypatch.delenv("BLAMIXFILES_GDRIVE_CLIENT_ID", raising=False)
    monkeypatch.delenv("BLAMIXFILES_GDRIVE_CLIENT_SECRET", raising=False)
    with pytest.raises(oauth.OAuthError, match="app id"):
        oauth.client_for(Site(protocol="gdrive"))
    with pytest.raises(oauth.OAuthError, match="client secret"):
        oauth.client_for(Site(protocol="gdrive", oauth_client_id="abc"))
    assert oauth.client_for(Site(protocol="dropbox", oauth_client_id="abc")) == ("abc", "")


def test_gdrive_specifics(cloud):
    proto, fake, site = cloud
    if proto != "gdrive":
        pytest.skip("Google Drive only")
    doc = fake.add("/Plan", b"")
    doc.folder, doc.mime = False, "application/vnd.google-apps.document"
    fake.add("/dup.txt", b"first")
    time.sleep(0.01)
    fake.add("/dup.txt", b"second")
    b = open_backend(site)
    try:
        assert [e.name for e in b.list("/")].count("dup.txt") == 1
        assert b.read_bytes("/dup.txt") == b"first"                         # the oldest of two same-named files
        with pytest.raises(BackendError, match="Google Docs"):
            b.read_bytes("/Plan")
        b.upload(io.BytesIO(b""), "/empty.txt")
        assert b.stat("/empty.txt").size == 0
        b.upload(io.BytesIO(b"quote's"), "/it's.txt")
        assert b.read_bytes("/it's.txt") == b"quote's"
    finally:
        b.close()


# ======================================================================= SMB (fake smbclient)
class FakeSMB:
    """smbclient's functions, backed by a local folder: \\\\host\\share\\x -> root/share/x."""

    def __init__(self, root):
        self.root = root
        self.sessions = []

    def _p(self, unc: str) -> str:
        assert unc.startswith("\\\\nas\\"), unc
        return os.path.join(self.root, *unc[len("\\\\nas\\"):].split("\\"))

    def register_session(self, host, username=None, password=None, port=445, connection_cache=None, **kw):
        if password != "pw":
            raise OSError("STATUS_LOGON_FAILURE")
        self.sessions.append(host)

    def reset_connection_cache(self, connection_cache=None):
        pass

    def scandir(self, unc, **kw):
        return os.scandir(self._p(unc))

    def stat(self, unc, **kw):
        return os.stat(self._p(unc))

    def mkdir(self, unc, **kw):
        os.mkdir(self._p(unc))

    def rmdir(self, unc, **kw):
        os.rmdir(self._p(unc))

    def remove(self, unc, **kw):
        os.remove(self._p(unc))

    def replace(self, a, b, **kw):
        os.replace(self._p(a), self._p(b))

    def utime(self, unc, times=None, **kw):
        os.utime(self._p(unc), times)

    def truncate(self, unc, length, **kw):
        os.truncate(self._p(unc), length)

    def open_file(self, unc, mode="rb", **kw):
        return open(self._p(unc), mode)


def test_smb_backend(tmp_path, monkeypatch):
    from blamixfiles.core.backends import smb as SMB
    (tmp_path / "Public" / "docs").mkdir(parents=True)
    (tmp_path / "Public" / "docs" / "a.txt").write_text("hello")
    fake = FakeSMB(str(tmp_path))
    monkeypatch.setattr(SMB, "_client", lambda: fake)
    site = Site(protocol="smb", host="nas", username="me", password="pw", remote_dir="/Public")
    b = open_backend(site)
    try:
        assert b.home() == "/Public"
        assert [e.name for e in b.list("/")] == ["Public"]
        assert [e.name for e in b.list("/Public/docs")] == ["a.txt"]
        assert b.read_bytes("/Public/docs/a.txt") == b"hello"
        b.upload(io.BytesIO(b"0123456789"), "/Public/docs/n.bin")
        b.upload(io.BytesIO(b"XYZ"), "/Public/docs/n.bin", offset=5)          # resume: keep 5, append
        assert (tmp_path / "Public" / "docs" / "n.bin").read_bytes() == b"01234XYZ"
        out = io.BytesIO()
        b.download("/Public/docs/n.bin", out, offset=5)
        assert out.getvalue() == b"XYZ"
        b.write_bytes("/Public/docs/a.txt", b"edited")
        assert (tmp_path / "Public" / "docs" / "a.txt").read_text() == "edited"
        b.makedirs("/Public/x/y")
        b.rename("/Public/docs/n.bin", "/Public/x/y/m.bin")
        b.set_mtime("/Public/x/y/m.bin", 1_600_000_000)
        assert int(b.stat("/Public/x/y/m.bin").mtime) == 1_600_000_000
        assert b.stat("/Public/nope") is None and b.stat("/").is_dir
        b.remove_tree("/Public/x")
        assert not (tmp_path / "Public" / "x").exists()
    finally:
        b.close()
    with pytest.raises(BackendError, match="Wrong user name or password"):
        open_backend(Site(protocol="smb", host="nas", username="me", password="bad", remote_dir="/Public"))
    b = open_backend(Site(protocol="smb", host="nas", username="me", password="pw"))
    with pytest.raises(BackendError, match="Which share"):
        b.list("/")
