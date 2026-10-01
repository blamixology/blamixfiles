"""Nextcloud chunked uploads (v2) against a fake Nextcloud (wsgidav + the chunking API)."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("wsgidav")

import blamixfiles.core.backends.webdav as W  # noqa: E402
from blamixfiles.core import engine as E  # noqa: E402
from blamixfiles.core.backends import open_backend  # noqa: E402
from blamixfiles.models import Site  # noqa: E402
from servers import PASSWORD, USER, WebDAVTestServer  # noqa: E402

FILES = f"/remote.php/dav/files/{USER}"


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(W, "CHUNKED", 5 * W.MB)
    monkeypatch.setattr(W, "CHUNK", 5 * W.MB)


def _site(srv, remote_dir=FILES):
    return Site(protocol="webdav", host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD,
                remote_dir=remote_dir)


class _Stop(Exception):
    pass


def test_chunked_upload_and_resume(tmp_path, small_chunks):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "big.bin"
    data = os.urandom(23 * W.MB)                      # chunks: 4 x 5 MB + 3 MB
    src.write_bytes(data)
    with WebDAVTestServer(root, nextcloud=True) as srv:
        b = open_backend(_site(srv))

        sent = []

        def stop_after_two_chunks(n):
            sent.append(n)
            if sum(sent) >= 12 * W.MB:                 # inside the third chunk
                raise _Stop()
        with open(src, "rb") as f, pytest.raises(_Stop):
            b.upload(f, f"{FILES}/big.bin", progress=stop_after_two_chunks)
        assert not (root / "big.bin").exists()
        b.close()

        # a new connection (like after a restart) continues where it stopped
        b = open_backend(_site(srv))
        srv.chunk_puts.clear()
        moved, skipped = [], []

        def cb(n):
            moved.append(n)
        cb.skip = skipped.append
        with open(src, "rb") as f:
            b.upload(f, f"{FILES}/big.bin", progress=cb)
        assert sum(skipped) == 10 * W.MB and sum(moved) == 13 * W.MB
        assert srv.chunk_puts == ["00003", "00004", "00005"]
        assert (root / "big.bin").read_bytes() == data
        assert int((root / "big.bin").stat().st_mtime) == int(src.stat().st_mtime)   # X-OC-Mtime
        b.close()


def test_small_files_and_engine_use_normal_puts(tmp_path, small_chunks):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "up").mkdir()
    (tmp_path / "up" / "small.txt").write_bytes(b"hi")
    (tmp_path / "up" / "big.bin").write_bytes(os.urandom(11 * W.MB))
    with WebDAVTestServer(root, nextcloud=True) as srv:
        site = _site(srv)
        eng = E.TransferEngine(lambda s: open_backend(s), workers=2)
        job = eng.upload(site, str(tmp_path / "up"), FILES)
        assert eng.wait(60)
        eng.shutdown()
        assert job.status == E.DONE
        assert (root / "up" / "small.txt").read_bytes() == b"hi"
        assert (root / "up" / "big.bin").read_bytes() == (tmp_path / "up" / "big.bin").read_bytes()
        assert srv.chunk_puts == ["00001", "00002", "00003"]          # only the big one was chunked


def test_falls_back_to_plain_put_without_chunking_api(tmp_path, small_chunks):
    root = tmp_path / "root"
    (root / "remote.php" / "dav" / "files" / USER).mkdir(parents=True)
    data = os.urandom(7 * W.MB)
    with WebDAVTestServer(root) as srv:                                 # plain WebDAV, Nextcloud-like path
        b = open_backend(_site(srv))
        import io
        b.upload(io.BytesIO(data), f"{FILES}/x.bin")
        assert b.chunking is False
        assert (root / "remote.php" / "dav" / "files" / USER / "x.bin").read_bytes() == data
        b.close()
