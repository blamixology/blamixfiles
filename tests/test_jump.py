"""Jump hosts (ProxyJump): reach a server through another saved SSH site."""
from __future__ import annotations

import io

import pytest

from blamixfiles.core import engine as E
from blamixfiles.core import ssh
from blamixfiles.core.backends import open_backend
from blamixfiles.models import Site
from servers import PASSWORD, USER, SFTPTestServer


def trusted_open(site):
    for _ in range(4):
        try:
            return open_backend(site)
        except ssh.UnknownHostKey as e:
            ssh.trust_host_key(e.host_id, e.key)
    raise AssertionError("host keys kept changing")


@pytest.fixture
def two_servers(tmp_path):
    (tmp_path / "bastion").mkdir()
    (tmp_path / "inner").mkdir()
    with SFTPTestServer(tmp_path / "bastion") as bastion, SFTPTestServer(tmp_path / "inner") as inner:
        jump = Site(name="bastion", protocol="sftp", host="127.0.0.1", port=bastion.port,
                    username=USER, password=PASSWORD)
        target = Site(name="app-01", protocol="sftp", host="127.0.0.1", port=inner.port,
                      username=USER, password=PASSWORD, jump_id=jump.id)
        sites = {jump.id: jump, target.id: target}
        ssh.set_site_resolver(sites.get)
        yield bastion, inner, jump, target, tmp_path / "inner"
        ssh.set_site_resolver(lambda _id: None)


def test_connect_through_jump_host(two_servers):
    bastion, inner, jump, target, inner_root = two_servers
    b = trusted_open(target)
    b.upload(io.BytesIO(b"via bastion"), "/hello.txt")
    assert (inner_root / "hello.txt").read_text() == "via bastion"
    assert ("127.0.0.1", inner.port) in bastion.forwarded          # really went through the bastion
    jump_client = b.client.jump
    assert jump_client is not None
    b.close()
    assert jump_client.get_transport() is None or not jump_client.get_transport().is_active()


def test_transfers_reuse_the_tunnel(two_servers, tmp_path):
    from blamixfiles.core.backends.sftp import SFTPBackend
    _, _, _, target, inner_root = two_servers
    main = trusted_open(target)
    src = tmp_path / "f.bin"
    src.write_bytes(b"x" * 300_000)
    eng = E.TransferEngine(lambda s: SFTPBackend.on_client(s, main.client), workers=2)
    eng.upload(target, str(src), "/")
    assert eng.wait(20) and eng.jobs[0].status == E.DONE, eng.jobs[0].error
    assert (inner_root / "f.bin").stat().st_size == 300_000
    eng.shutdown()
    main.close()


def test_missing_or_looping_jump_host(two_servers):
    _, _, jump, target, _ = two_servers
    ssh.set_site_resolver(lambda _id: None)
    with pytest.raises(ssh.AuthConfigError, match="no longer exists"):
        open_backend(target)
    loop_a = Site(host="a", jump_id="b", id="a")
    loop_b = Site(host="b", jump_id="a", id="b")
    ssh.set_site_resolver({"a": loop_a, "b": loop_b}.get)
    with pytest.raises(ssh.AuthConfigError, match="too long"):
        ssh.open_client(loop_a)


def test_terminal_command_includes_jump_chain():
    from blamixfiles.ui.terminal import ssh_command
    outer = Site(host="gw.example.com", username="ops", port=2200, id="outer")
    bastion = Site(host="10.0.0.1", username="jump", jump_id="outer", id="bastion")
    app = Site(host="10.0.1.5", username="deploy", auth="key", key_path="~/.ssh/deploy", jump_id="bastion")
    cmd = ssh_command(app, "/var/www/my site", {"outer": outer, "bastion": bastion}.get)
    assert cmd[:2] == ["ssh", "-t"]
    assert cmd[cmd.index("-J") + 1] == "ops@gw.example.com:2200,jump@10.0.0.1"
    assert "deploy@10.0.1.5" in cmd and cmd[-1] == "cd '/var/www/my site' && exec \"$SHELL\" -l"
