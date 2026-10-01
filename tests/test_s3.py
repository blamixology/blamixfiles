"""S3 against an in-process S3 API (moto)."""
from __future__ import annotations

import io
import os

import pytest

pytest.importorskip("boto3")
pytest.importorskip("moto")

from blamixfiles.core import engine as E  # noqa: E402
from blamixfiles.core import sync as S  # noqa: E402
from blamixfiles.core.backends import open_backend  # noqa: E402
from blamixfiles.core.backends.local import LocalBackend  # noqa: E402
from blamixfiles.models import Site  # noqa: E402
from servers import S3TestServer  # noqa: E402


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    with S3TestServer() as srv:
        site = Site(protocol="s3", host=srv.endpoint, username="AKIATEST", password="secret")
        b = open_backend(site)
        b.mkdir("/photos")                       # a bucket
        yield b, site
        b.close()


def test_buckets_folders_files(s3):
    b, _ = s3
    assert [e.name for e in b.list("/")] == ["photos"] and b.list("/")[0].is_dir
    b.mkdir("/photos/2026")
    b.upload(io.BytesIO(b"jpegdata"), "/photos/2026/beach.jpg")
    b.upload(io.BytesIO(b"x" * 10), "/photos/top.txt")
    names = {e.name: e for e in b.list("/photos")}
    assert names["2026"].is_dir and names["top.txt"].size == 10
    assert [e.name for e in b.list("/photos/2026")] == ["beach.jpg"]     # marker object hidden
    assert b.stat("/photos/2026").is_dir and b.stat("/photos/2026/beach.jpg").size == 8
    assert b.stat("/photos/nope.txt") is None and b.stat("/nobucket") is None
    b.rename("/photos/top.txt", "/photos/2026/moved.txt")
    assert b.stat("/photos/top.txt") is None and b.read_bytes("/photos/2026/moved.txt") == b"x" * 10
    with pytest.raises(Exception, match="folders"):
        b.rename("/photos/2026", "/photos/2027")
    b.remove_tree("/photos/2026")
    assert b.list("/photos") == []
    with pytest.raises(Exception, match="bucket"):
        b.remove_tree("/photos")


def test_ranged_download_and_multipart(s3, monkeypatch):
    import blamixfiles.core.backends.s3 as mod
    b, _ = s3
    monkeypatch.setattr(mod, "MULTIPART", 5 * 1024 * 1024)          # force a multipart upload
    data = os.urandom(12 * 1024 * 1024)
    moved = []
    b.upload(io.BytesIO(data), "/photos/big.bin", progress=moved.append)
    assert sum(moved) == len(data)
    out = io.BytesIO()
    out.write(data[:1000])
    b.download("/photos/big.bin", out, offset=1000)
    assert out.getvalue() == data


def test_engine_and_sync_with_s3(s3, tmp_path):
    b, site = s3
    local = tmp_path / "site"
    (local / "css").mkdir(parents=True)
    (local / "index.html").write_text("<h1>hi</h1>")
    (local / "css" / "app.css").write_text("body{}")
    b.upload(io.BytesIO(b"old"), "/photos/www/stale.html")
    opt = S.SyncOptions(direction="upload", mirror=True, tolerance=60)
    plan = S.compare(S.scan(LocalBackend(), str(local), opt.excludes), S.scan(b, "/photos/www", opt.excludes),
                     str(local), "/photos/www", opt)
    eng = E.TransferEngine(lambda s: open_backend(s), workers=3)
    S.apply(plan, site, eng, b, LocalBackend())
    assert eng.wait(30) and all(j.status == E.DONE for j in eng.jobs), [j.error for j in eng.jobs]
    assert b.read_bytes("/photos/www/css/app.css") == b"body{}" and b.stat("/photos/www/stale.html") is None
    again = S.compare(S.scan(LocalBackend(), str(local), opt.excludes), S.scan(b, "/photos/www", opt.excludes),
                      str(local), "/photos/www", opt)
    assert again.actions == [], again.summary()
    # download a "folder" back
    back = tmp_path / "back"
    back.mkdir()
    eng.download(site, b.stat("/photos/www"), str(back))
    assert eng.wait(30) and (back / "www" / "index.html").read_text() == "<h1>hi</h1>"
    eng.shutdown()


def test_bad_keys_message(monkeypatch):
    from blamixfiles.core.errors import friendly
    with S3TestServer() as srv:
        # moto accepts any key by default; check the mapping on a synthetic error instead
        import botocore.exceptions as be
        from blamixfiles.core.backends.s3 import S3Backend
        err = be.ClientError({"Error": {"Code": "InvalidAccessKeyId", "Message": "x"}}, "ListBuckets")
        assert "access key" in friendly(S3Backend._error(err, "/"))
        assert srv.port


class _Stop(Exception):
    pass


def _progress_with_skip(moved, skipped, stop_after=None):
    def cb(n):
        moved.append(n)
        if stop_after is not None and sum(moved) >= stop_after:
            raise _Stop()
    cb.skip = skipped.append
    return cb


def test_multipart_upload_resumes_after_interruption(s3, monkeypatch):
    import blamixfiles.core.backends.s3 as mod
    b, _ = s3
    monkeypatch.setattr(mod, "MULTIPART", 5 * mod.MB)
    monkeypatch.setattr(mod, "PART", 5 * mod.MB)
    monkeypatch.setattr(mod, "PARALLEL", 1)                      # deterministic order
    data = os.urandom(23 * mod.MB)                                # 5 parts: 4 x 5 MB + 3 MB
    moved, skipped = [], []
    with pytest.raises(_Stop):
        b.upload(io.BytesIO(data), "/photos/huge.bin", progress=_progress_with_skip(moved, skipped, 10 * mod.MB))
    assert b.stat("/photos/huge.bin") is None                     # not finished...
    assert len(b._pending("photos", "huge.bin")) == 1             # ...but S3 kept the parts

    moved, skipped = [], []
    b.upload(io.BytesIO(data), "/photos/huge.bin", progress=_progress_with_skip(moved, skipped))
    assert sum(skipped) == 10 * mod.MB                            # 2 parts reused
    assert sum(moved) == len(data) - 10 * mod.MB                  # only the rest was sent
    assert b.read_bytes("/photos/huge.bin") == data
    assert b._pending("photos", "huge.bin") == []


def test_multipart_resume_ignores_parts_of_a_changed_file(s3, monkeypatch):
    import blamixfiles.core.backends.s3 as mod
    b, _ = s3
    monkeypatch.setattr(mod, "MULTIPART", 5 * mod.MB)
    monkeypatch.setattr(mod, "PART", 5 * mod.MB)
    monkeypatch.setattr(mod, "PARALLEL", 1)
    old = os.urandom(12 * mod.MB)
    with pytest.raises(_Stop):
        b.upload(io.BytesIO(old), "/photos/f.bin", progress=_progress_with_skip([], [], 5 * mod.MB))
    new = old[:5 * mod.MB] + os.urandom(7 * mod.MB)               # first part same, rest edited
    moved, skipped = [], []
    b.upload(io.BytesIO(new), "/photos/f.bin", progress=_progress_with_skip(moved, skipped))
    assert sum(skipped) == 5 * mod.MB and sum(moved) == 7 * mod.MB
    assert b.read_bytes("/photos/f.bin") == new

    # a completely different file: the old unfinished upload is dropped, not reused
    with pytest.raises(_Stop):
        b.upload(io.BytesIO(old), "/photos/g.bin", progress=_progress_with_skip([], [], 5 * mod.MB))
    other = os.urandom(12 * mod.MB)
    moved, skipped = [], []
    b.upload(io.BytesIO(other), "/photos/g.bin", progress=_progress_with_skip(moved, skipped))
    assert skipped == [] and b.read_bytes("/photos/g.bin") == other
    assert b._pending("photos", "g.bin") == []


def test_abort_unfinished(s3, monkeypatch):
    import blamixfiles.core.backends.s3 as mod
    b, _ = s3
    monkeypatch.setattr(mod, "MULTIPART", 5 * mod.MB)
    monkeypatch.setattr(mod, "PART", 5 * mod.MB)
    with pytest.raises(_Stop):
        b.upload(io.BytesIO(os.urandom(11 * mod.MB)), "/photos/x.bin", progress=_progress_with_skip([], [], 1))
    assert b.abort_unfinished("/photos/x.bin") == 1 and b._pending("photos", "x.bin") == []
