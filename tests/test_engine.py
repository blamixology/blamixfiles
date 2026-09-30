"""Transfer queue: folders, parallel files, overwrite policies, resume, cancel, reconnect."""
from __future__ import annotations

import os

import pytest

from blamixfiles.core import engine as E
from blamixfiles.core.backends import open_backend
from blamixfiles.core.backends.local import LocalBackend
from blamixfiles.models import Site
from servers import PASSWORD, USER, FTPTestServer, SFTPTestServer
from test_backends import connect


@pytest.fixture(params=["sftp", "ftp"])
def served(request, tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    Server = SFTPTestServer if request.param == "sftp" else FTPTestServer
    with Server(root) as srv:
        site = Site(protocol=request.param, host="127.0.0.1", port=srv.port,
                    username=USER, password=PASSWORD, parallel=3)
        connect(site).close()          # trust the host key once
        yield site, root


def make_tree(base):
    (base / "proj" / "src" / "deep").mkdir(parents=True)
    (base / "proj" / "README.md").write_text("# hi\n")
    (base / "proj" / "src" / "app.py").write_bytes(os.urandom(150_000))
    (base / "proj" / "src" / "deep" / "x.bin").write_bytes(os.urandom(70_000))
    (base / "proj" / "empty").mkdir()


def files(root):
    return {str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def test_upload_then_download_folder(served, tmp_path):
    site, root = served
    local = tmp_path / "local"
    local.mkdir()
    make_tree(local)
    eng = E.TransferEngine(lambda s: open_backend(s), workers=3)
    eng.upload(site, str(local / "proj"), "/")
    assert eng.wait(30)
    assert all(j.status == E.DONE for j in eng.jobs), [(j.name, j.error) for j in eng.jobs]
    assert files(root / "proj") == files(local / "proj")
    assert (root / "proj" / "empty").is_dir()

    back = tmp_path / "back"
    back.mkdir()
    b = open_backend(site)
    entry = b.stat("/proj")
    b.close()
    eng.download(site, entry, str(back))
    assert eng.wait(30)
    assert files(back / "proj") == files(local / "proj")
    # timestamps are preserved on download
    src_m = (root / "proj" / "README.md").stat().st_mtime
    assert abs((back / "proj" / "README.md").stat().st_mtime - src_m) < 2
    eng.shutdown()


def test_policies(served, tmp_path):
    site, root = served
    f = tmp_path / "a.txt"
    f.write_text("NEW CONTENT")
    (root / "a.txt").write_text("old")
    eng = E.TransferEngine(lambda s: open_backend(s), workers=1, policy="skip")
    j = eng.upload(site, str(f), "/")
    eng.wait(10)
    assert j.status == E.SKIPPED and (root / "a.txt").read_text() == "old"

    eng.policy = "overwrite"
    j = eng.upload(site, str(f), "/")
    eng.wait(10)
    assert j.status == E.DONE and (root / "a.txt").read_text() == "NEW CONTENT"

    # resume: remote has the first half
    big = tmp_path / "big.bin"
    data = os.urandom(400_000)
    big.write_bytes(data)
    (root / "big.bin").write_bytes(data[:123_456])
    eng.policy = "resume"
    j = eng.upload(site, str(big), "/")
    eng.wait(10)
    assert j.status == E.DONE and j.resumed_from == 123_456
    assert (root / "big.bin").read_bytes() == data

    # "ask" goes through the callback
    asked = []
    eng.policy = "ask"
    eng.ask = lambda job, existing: asked.append(existing.size) or "skip"
    j = eng.upload(site, str(f), "/")
    eng.wait(10)
    assert asked == [len("NEW CONTENT")] and j.status == E.SKIPPED
    eng.shutdown()


def test_cancel_and_retry(served, tmp_path):
    site, root = served
    big = tmp_path / "huge.bin"
    big.write_bytes(os.urandom(3_000_000))
    started = []

    def on_change(job):
        if job.status == E.RUNNING and not started:   # cancel as soon as it starts
            started.append(job.id)
            eng.cancel(job.id)

    eng = E.TransferEngine(lambda s: open_backend(s), workers=1, on_change=on_change)
    j = eng.upload(site, str(big), "/")
    eng.wait(20)
    assert j.status == E.CANCELLED
    eng.on_change = lambda job: None
    eng.policy = "resume"
    eng.retry(j.id)
    eng.wait(20)
    assert j.status == E.DONE, j.error
    assert (root / "huge.bin").read_bytes() == big.read_bytes()
    eng.shutdown()


def test_login_failure_is_not_retried(served, tmp_path):
    site, _ = served
    bad = site.copy()
    bad.password = "wrong"
    f = tmp_path / "x.txt"
    f.write_text("x")
    eng = E.TransferEngine(lambda s: open_backend(s), workers=1)
    j = eng.upload(bad, str(f), "/")
    eng.wait(20)
    assert j.status == E.FAILED and j.attempts == 1
    assert "failed" in j.error.lower()
    eng.shutdown()


def test_reconnects_after_dropped_connection(served, tmp_path):
    site, root = served
    made = []

    def connector(s):
        b = open_backend(s)
        made.append(b)
        return b

    eng = E.TransferEngine(connector, workers=1)
    for name in ("one.txt", "two.txt"):
        (tmp_path / name).write_text(name)
    j1 = eng.upload(site, str(tmp_path / "one.txt"), "/")
    eng.wait(10)
    made[0].close()                      # the server "hung up"
    j2 = eng.upload(site, str(tmp_path / "two.txt"), "/")
    eng.wait(20)
    assert j1.status == j2.status == E.DONE, j2.error
    assert (root / "two.txt").read_text() == "two.txt"
    eng.shutdown()


def test_local_backend_roundtrip(tmp_path):
    tmp_path = tmp_path / "local"
    tmp_path.mkdir()
    b = LocalBackend()
    p = str(tmp_path / "f.txt")
    b.write_bytes(p, b"hello")
    assert b.read_bytes(p) == b"hello"
    assert b.stat(p).size == 5 and b.stat(str(tmp_path / "nope")) is None
    assert [e.name for e in b.list(str(tmp_path))] == ["f.txt"]
