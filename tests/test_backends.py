"""One contract suite, run against every protocol backend (SFTP, FTP, FTPS)."""
from __future__ import annotations

import io
import os
import stat
import time
from contextlib import ExitStack

import pytest

from blamixfiles.core import ssh
from blamixfiles.core.backends import open_backend
from blamixfiles.core.backends.ftp import FTPBackend, UntrustedCertificate, parse_list_line
from blamixfiles.models import Site
from servers import PASSWORD, USER, FTPTestServer, SFTPTestServer


def connect(site):
    try:
        return open_backend(site)
    except ssh.UnknownHostKey as e:          # first connect: trust, like the dialog does
        ssh.trust_host_key(e.host_id, e.key)
        return open_backend(site)
    except UntrustedCertificate as e:        # self-signed test cert: pin it
        site.tls_pinned = e.fingerprint
        return open_backend(site)


@pytest.fixture(params=["sftp", "ftp", "ftps"])
def remote(request, tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    with ExitStack() as stack:
        if request.param == "sftp":
            srv = stack.enter_context(SFTPTestServer(root))
        else:
            srv = stack.enter_context(FTPTestServer(root, tls=request.param == "ftps", certdir=tmp_path))
        site = Site(protocol=request.param, host="127.0.0.1", port=srv.port,
                    username=USER, password=PASSWORD)
        b = connect(site)
        yield b, root, site
        b.close()


def test_list_mkdir_stat(remote):
    b, root, _ = remote
    (root / "hello.txt").write_text("hi")
    b.mkdir("/sub")
    names = {e.name: e for e in b.list("/")}
    assert set(names) == {"hello.txt", "sub"}
    assert names["sub"].is_dir and not names["hello.txt"].is_dir
    assert names["hello.txt"].size == 2
    assert abs(names["hello.txt"].mtime - (root / "hello.txt").stat().st_mtime) < 61
    assert b.stat("/hello.txt").size == 2
    assert b.stat("/nope.txt") is None


def test_upload_download_roundtrip(remote):
    b, root, _ = remote
    data = os.urandom(300_000)
    moved = []
    b.upload(io.BytesIO(data), "/blob.bin", progress=moved.append)
    assert (root / "blob.bin").read_bytes() == data
    assert sum(moved) == len(data)
    out = io.BytesIO()
    b.download("/blob.bin", out)
    assert out.getvalue() == data


def test_resume_both_directions(remote):
    b, root, _ = remote
    data = os.urandom(200_000)
    (root / "part.bin").write_bytes(data[:50_000])
    src = io.BytesIO(data)
    src.seek(50_000)
    b.upload(src, "/part.bin", offset=50_000)
    assert (root / "part.bin").read_bytes() == data

    out = io.BytesIO()
    out.write(data[:120_000])
    b.download("/part.bin", out, offset=120_000)
    assert out.getvalue() == data


def test_rename_remove_tree(remote):
    b, root, _ = remote
    (root / "a").mkdir()
    (root / "a" / "b").mkdir()
    (root / "a" / "b" / "f.txt").write_text("x")
    (root / "a" / "g.txt").write_text("y")
    b.rename("/a/g.txt", "/a/h.txt")
    assert (root / "a" / "h.txt").exists()
    b.remove_tree("/a")
    assert not (root / "a").exists()


def test_write_bytes_replaces_atomically(remote):
    b, root, site = remote
    target = root / "nginx.conf"
    target.write_bytes(b"old\n")
    if site.protocol == "sftp":
        os.chmod(target, 0o640)
    b.write_bytes("/nginx.conf", b"new content\n")
    assert target.read_bytes() == b"new content\n"
    assert not any(p.name.endswith(".blamixfiles-tmp") for p in root.iterdir())
    if site.protocol == "sftp":
        assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_set_mtime_when_supported(remote):
    b, root, _ = remote
    (root / "t.txt").write_text("x")
    if not b.caps.set_mtime:
        pytest.skip("server has no MFMT")
    when = time.time() - 86400 * 3
    b.set_mtime("/t.txt", when)
    assert abs((root / "t.txt").stat().st_mtime - when) < 2


def test_ftps_untrusted_cert_then_pinned(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    with FTPTestServer(root, tls=True, certdir=tmp_path) as srv:
        site = Site(protocol="ftps", host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD)
        with pytest.raises(UntrustedCertificate) as ei:
            FTPBackend(site).connect()
        site.tls_pinned = ei.value.fingerprint
        b = FTPBackend(site)
        b.connect()
        assert b.list("/") == []
        b.close()


def test_sftp_asks_about_unknown_host_key(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    with SFTPTestServer(root) as srv:
        site = Site(protocol="sftp", host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD)
        with pytest.raises(ssh.UnknownHostKey):
            open_backend(site)


@pytest.mark.parametrize("line,expect", [
    ("drwxr-xr-x    2 www  www      4096 Mar 03 10:15 html",
     dict(name="html", is_dir=True, size=4096)),
    ("-rw-r--r--    1 root root      220 Jan 01  2021 .bashrc",
     dict(name=".bashrc", is_dir=False, size=220)),
    ("lrwxrwxrwx    1 root root        7 Feb 10  2024 bin -> usr/bin",
     dict(name="bin", is_link=True)),
    ("-rw-r--r-- 1 u g 12 Jun  5 09:01 name with spaces.txt",
     dict(name="name with spaces.txt", size=12)),
    ("03-15-24  02:30PM       <DIR>          wwwroot", dict(name="wwwroot", is_dir=True)),
    ("03-15-24  09:05AM             1234 web.config", dict(name="web.config", size=1234)),
])
def test_list_parser(line, expect):
    d = parse_list_line(line)
    assert d is not None
    for k, v in expect.items():
        assert d[k] == v
