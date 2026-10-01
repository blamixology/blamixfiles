"""SCP against a real OpenSSH server (skipped where sshd can't be started)."""
from __future__ import annotations

import io
import os

import pytest

from blamixfiles.core import engine as E
from blamixfiles.core import ssh
from blamixfiles.core.backends import open_backend
from blamixfiles.models import Site
from servers import OpenSSHServer

pytestmark = pytest.mark.skipif(not OpenSSHServer.available(), reason="needs OpenSSH sshd and root")


@pytest.fixture
def scp(tmp_path):
    with OpenSSHServer(tmp_path / "sshd") as srv:
        site = Site(protocol="scp", host="127.0.0.1", port=srv.port, username=srv.user,
                    auth="key", key_path=str(srv.client_key))
        try:
            b = open_backend(site)
        except ssh.UnknownHostKey as e:
            ssh.trust_host_key(e.host_id, e.key)
            b = open_backend(site)
        root = tmp_path / "remote"
        root.mkdir()
        yield b, root, site
        b.close()


def test_scp_list_stat_ops(scp):
    b, root, _ = scp
    (root / "a file.txt").write_text("hello")
    (root / "sub").mkdir()
    os.symlink(root / "sub", root / "link-to-sub")
    names = {e.name: e for e in b.list(str(root))}
    assert set(names) == {"a file.txt", "sub", "link-to-sub"}
    assert names["a file.txt"].size == 5 and not names["a file.txt"].is_dir
    assert names["sub"].is_dir and names["link-to-sub"].is_dir and names["link-to-sub"].is_link
    assert abs(names["a file.txt"].mtime - (root / "a file.txt").stat().st_mtime) < 2
    assert b.stat(str(root / "nope")) is None
    assert b.stat(str(root / "a file.txt")).size == 5
    b.mkdir(str(root / "new dir"))
    b.rename(str(root / "a file.txt"), str(root / "new dir" / "moved.txt"))
    assert (root / "new dir" / "moved.txt").read_text() == "hello"
    b.makedirs(str(root / "x" / "y" / "z"))
    b.remove_tree(str(root / "x"))
    assert not (root / "x").exists()


def test_scp_transfer_roundtrip(scp):
    b, root, _ = scp
    data = os.urandom(700_000)
    moved = []
    b.upload(io.BytesIO(data), str(root / "blob.bin"), progress=moved.append)
    assert (root / "blob.bin").read_bytes() == data and sum(moved) == len(data)
    out = io.BytesIO()
    b.download(str(root / "blob.bin"), out)
    assert out.getvalue() == data
    part = io.BytesIO()                     # "resume": SCP re-sends, we keep only the rest
    part.write(data[:100_000])
    b.download(str(root / "blob.bin"), part, offset=100_000)
    assert part.getvalue() == data
    from blamixfiles.core.vfs import BackendError
    with pytest.raises(BackendError, match="No such file"):
        b.download(str(root / "missing.bin"), io.BytesIO())


def test_scp_editor_save_and_engine(scp, tmp_path):
    b, root, site = scp
    conf = root / "app.conf"
    conf.write_text("port=80\n")
    os.chmod(conf, 0o640)
    b.write_bytes(str(conf), b"port=8080\n")
    assert conf.read_text() == "port=8080\n" and (conf.stat().st_mode & 0o777) == 0o640
    local = tmp_path / "up"
    (local / "d").mkdir(parents=True)
    (local / "d" / "f.txt").write_text("f")
    eng = E.TransferEngine(lambda s: open_backend(s), workers=2)
    eng.upload(site, str(local / "d"), str(root))
    assert eng.wait(20) and all(j.status == E.DONE for j in eng.jobs), [j.error for j in eng.jobs]
    assert (root / "d" / "f.txt").read_text() == "f"
    eng.shutdown()
