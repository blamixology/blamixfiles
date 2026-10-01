"""Checksum verification after transfers, and sync comparing by content."""
from __future__ import annotations

import hashlib
import io
import os
import time

import pytest

from blamixfiles.core import engine as E
from blamixfiles.core import ssh
from blamixfiles.core import sync as S
from blamixfiles.core.backends import open_backend
from blamixfiles.core.backends.local import LocalBackend
from blamixfiles.models import Site
from servers import PASSWORD, USER, FTPTestServer, OpenSSHServer


def test_local_and_engine_verify(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(os.urandom(200_000))
    dst = tmp_path / "out"
    dst.mkdir()
    eng = E.TransferEngine(lambda s: LocalBackend(), workers=1, verify=True)
    j = eng.upload(Site(host="local"), str(src), str(dst), remote_join=os.path.join)
    assert eng.wait(10) and j.status == E.DONE and j.verified == "sha256"
    eng.shutdown()


def test_mismatch_fails_the_job(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello")
    (tmp_path / "out").mkdir()

    class Liar(LocalBackend):
        def checksum(self, path, algos=("sha256", "md5")):
            return "sha256", "0" * 64
    eng = E.TransferEngine(lambda s: Liar(), workers=1, verify=True)
    j = eng.upload(Site(host="x"), str(src), str(tmp_path / "out"), remote_join=os.path.join)
    eng.wait(10)
    assert j.status == E.FAILED and "mismatch" in j.error.lower() and j.verified == "mismatch"
    eng.shutdown()


def test_server_without_hashing_is_reported(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    src = tmp_path / "f.txt"
    src.write_text("data")
    with FTPTestServer(root) as srv:          # pyftpdlib has no HASH/XSHA256
        site = Site(protocol="ftp", host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD)
        eng = E.TransferEngine(lambda s: open_backend(s), workers=1, verify=True)
        j = eng.upload(site, str(src), "/")
        assert eng.wait(10) and j.status == E.DONE and j.verified == "unsupported"
        eng.shutdown()


def test_ftp_hash_reply_parsing():
    from blamixfiles.core.backends.ftp import FTPBackend
    b = FTPBackend(Site(protocol="ftp", host="x"))
    b.features = {"HASH"}
    digest = hashlib.sha256(b"x").hexdigest()

    class Fake:
        def sendcmd(self, cmd):
            if cmd.startswith("OPTS"):
                return "200 SHA-256"
            return f"213 SHA-256 0-1 {digest} /f.txt"
    b.ftp = Fake()
    assert b.checksum("/f.txt") == ("sha256", digest)


@pytest.mark.skipif(not OpenSSHServer.available(), reason="needs OpenSSH sshd and root")
@pytest.mark.parametrize("proto", ["sftp", "scp"])
def test_ssh_checksums(tmp_path, proto):
    with OpenSSHServer(tmp_path / "sshd") as srv:
        site = Site(protocol=proto, host="127.0.0.1", port=srv.port, username=srv.user,
                    auth="key", key_path=str(srv.client_key))
        try:
            b = open_backend(site)
        except ssh.UnknownHostKey as e:
            ssh.trust_host_key(e.host_id, e.key)
            b = open_backend(site)
        f = tmp_path / "x.bin"
        f.write_bytes(b"abc" * 1000)
        assert b.checksum(str(f)) == ("sha256", hashlib.sha256(f.read_bytes()).hexdigest())
        assert b.checksum(str(tmp_path / "missing")) is None
        b.close()


def test_s3_etag_md5(monkeypatch):
    pytest.importorskip("moto")
    from servers import S3TestServer
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    with S3TestServer() as srv:
        b = open_backend(Site(protocol="s3", host=srv.endpoint, username="k", password="s"))
        b.mkdir("/bucket1")
        b.upload(io.BytesIO(b"hello"), "/bucket1/h.txt")
        assert b.checksum("/bucket1/h.txt") == ("md5", hashlib.md5(b"hello").hexdigest())
        assert b.checksum("/bucket1/h.txt", ("sha256",)) is None


def test_sync_checksum_mode_finds_same_size_edits(tmp_path):
    L, R = tmp_path / "L", tmp_path / "R"
    L.mkdir()
    R.mkdir()
    old = time.time() - 9999
    for d, text in ((L, "version A"), (R, "version B")):        # same size, different bytes
        (d / "conf.ini").write_text(text)
        os.utime(d / "conf.ini", (old, old))                   # and identical timestamps
    (L / "same.txt").write_text("same")
    (R / "same.txt").write_text("same")
    lb = LocalBackend()
    opt = S.SyncOptions(direction="upload", compare="checksum")
    loc, rem = S.scan(lb, str(L), opt.excludes), S.scan(lb, str(R), opt.excludes)
    plan = S.compare(loc, rem, str(L), str(R), opt)
    assert plan.actions == [] and len(plan.to_check) == 2       # mtime mode would miss it
    S.resolve_checksums(plan, lb, lb)
    assert [(a.kind, a.rel, a.reason) for a in plan.actions] == [("upload", "conf.ini", "content differs (sha256)")]
    assert plan.unchanged == 1
