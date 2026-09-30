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


@pytest.mark.parametrize("name,windows,expect", [
    ("report.pdf", False, "report.pdf"),
    ("a\\b.txt", False, "a\\b.txt"),            # a legal (if odd) name on Linux/macOS
    ("what?.txt", True, "what_.txt"),
    ("trailing. ", True, "trailing"),
    ("CON.txt", True, "_CON.txt"),
])
def test_safe_local_name(name, windows, expect):
    assert E.safe_local_name(name, windows=windows) == expect


@pytest.mark.parametrize("name,windows", [
    ("..", False), (".", True), ("../x", False), ("a/b", True), ("..\\..\\evil.exe", True), ("x\x00", False),
])
def test_unsafe_names_refused(name, windows):
    from blamixfiles.core.vfs import BackendError
    with pytest.raises(BackendError):
        E.safe_local_name(name, windows=windows)


def test_malicious_listing_cannot_escape(tmp_path):
    """A hostile server returns '../escape.txt' in a folder listing."""
    from blamixfiles.core.vfs import Backend, Capabilities, Entry

    class Evil(Backend):
        caps = Capabilities()
        connected = True

        def list(self, path):
            return [Entry(name="../escape.txt", path="/d/../escape.txt", size=3),
                    Entry(name="ok.txt", path="/d/ok.txt", size=2)]

        def download(self, path, fp, offset=0, progress=None):
            fp.write(b"hi" if path.endswith("ok.txt") else b"bad")

    dest = tmp_path / "dest"
    dest.mkdir()
    eng = E.TransferEngine(lambda s: Evil(), workers=1)
    site = Site(host="evil")
    eng.download(site, Entry(name="d", path="/d", is_dir=True), str(dest))
    assert eng.wait(10)
    assert (dest / "d" / "ok.txt").read_bytes() == b"hi"
    assert not (tmp_path / "escape.txt").exists() and not (dest / "escape.txt").exists()
    assert any(j.status == E.FAILED and "unsafe" in j.error for j in eng.jobs)
    eng.shutdown()


# ------------------------------------------------------------------ speed limits
def test_token_bucket_limits_rate():
    import time as _t

    from blamixfiles.core.ratelimit import TokenBucket
    b = TokenBucket(200_000)                  # 200 KB/s
    start = _t.monotonic()
    for _ in range(20):
        b.consume(32 * 1024)                  # 640 KB
    took = _t.monotonic() - start
    assert 2.4 < took < 4.5, took             # ~3 s (minus the initial burst allowance)
    b.rate = 0
    start = _t.monotonic()
    b.consume(10_000_000)
    assert _t.monotonic() - start < 0.05      # unlimited again


def test_upload_respects_limit_and_cancel(served, tmp_path):
    import time as _t
    site, root = served
    f = tmp_path / "limited.bin"
    f.write_bytes(os.urandom(400_000))
    eng = E.TransferEngine(lambda s: open_backend(s), workers=1, limit_up=200_000)
    start = _t.monotonic()
    j = eng.upload(site, str(f), "/")
    assert eng.wait(20)
    assert j.status == E.DONE and _t.monotonic() - start > 1.2
    # a very slow limit must not make cancel hang
    eng.set_limit("upload", 1_000)
    j2 = eng.upload(site, str(f), "/")
    _t.sleep(0.5)
    t0 = _t.monotonic()
    eng.cancel(j2.id)
    assert eng.wait(5) and j2.status == E.CANCELLED and _t.monotonic() - t0 < 2
    eng.shutdown()


# ------------------------------------------------------------------ saved queue
def test_queue_survives_restart(served, tmp_path):
    from blamixfiles.core.queue_store import QueueStore
    site, root = served
    files = []
    for i in range(3):
        p = tmp_path / f"f{i}.txt"
        p.write_text(f"file {i}")
        files.append(p)
    db = tmp_path / "queue.db"

    # session 1: queue three uploads while paused, then "crash"
    store = QueueStore(db)
    eng = E.TransferEngine(lambda s: open_backend(s), workers=1, store=store)
    eng.set_paused(True)
    for p in files:
        eng.upload(site, str(p), "/")
    eng.shutdown()
    store.close()

    # session 2: they come back, in order, paused; resuming uploads them
    store = QueueStore(db)
    jobs = store.load({site.id: site})
    assert [os.path.basename(j.src) for j in jobs] == ["f0.txt", "f1.txt", "f2.txt"]
    eng = E.TransferEngine(lambda s: open_backend(s), workers=1, store=store)
    eng.restore(jobs)
    assert eng.paused
    eng.set_paused(False)
    assert eng.wait(20)
    assert all(j.status == E.DONE for j in jobs)
    assert sorted(p.name for p in root.iterdir()) == ["f0.txt", "f1.txt", "f2.txt"]
    assert store.load({site.id: site}) == []          # finished jobs are forgotten
    eng.shutdown()


def test_saved_queue_drops_deleted_sites_and_discard(tmp_path):
    from blamixfiles.core.queue_store import QueueStore
    store = QueueStore(tmp_path / "q.db")
    keep, gone = Site(host="a"), Site(host="b")
    for s in (keep, gone):
        store.sync(E.Job("upload", s, "/x", "/y", status=E.QUEUED))
    assert len(store.load({keep.id: keep})) == 1          # 'gone' was deleted from the vault
    assert len(store.load({keep.id: keep, gone.id: gone})) == 1
    eng = E.TransferEngine(lambda s: None, workers=1, store=store)
    eng.restore(store.load({keep.id: keep}))
    eng.discard_unfinished()
    assert store.load({keep.id: keep}) == []
    eng.shutdown()


def test_interrupted_transfer_resumes(tmp_path):
    from blamixfiles.core.queue_store import QueueStore
    store = QueueStore(tmp_path / "q.db")
    s = Site(host="a")
    store.sync(E.Job("upload", s, "/big.iso", "/remote/big.iso", size=10, status=E.RUNNING))
    (job,) = store.load({s.id: s})
    assert job.status == E.QUEUED and job.policy == "resume"


def test_quick_connect_jobs_are_not_saved(tmp_path):
    from blamixfiles.core.queue_store import QueueStore
    saved = Site(host="saved")
    store = QueueStore(tmp_path / "q.db", keep_site=lambda sid: sid == saved.id)
    store.sync(E.Job("upload", Site(host="quick"), "/a", "/b"))
    store.sync(E.Job("upload", saved, "/a", "/b"))
    assert len(store.load({saved.id: saved})) == 1
