"""'Open in BlamixShell': finding BlamixShell and the command line it gets
(BlamixShell --connect <user@host:port> [--key FILE] [--jump SERVER], since BlamixShell 1.17)."""
import os

import pytest

pytest.importorskip("PySide6")

from blamixfiles.models import Site  # noqa: E402
from blamixfiles.ui import terminal as T  # noqa: E402


def test_target_arguments():
    jump = Site(name="bastion", protocol="sftp", host="gw.example.com", username="ops", port=2200)
    web = Site(name="web", protocol="sftp", host="10.0.0.5", username="deploy", jump_id=jump.id,
               auth="key", key_path="~/.ssh/id_web")
    sites = {jump.id: jump}
    args = T.blamixshell_target(web, sites.get)
    assert args[:2] == ["--connect", "deploy@10.0.0.5"]
    assert args[2] == "--key" and args[3] == os.path.expanduser("~/.ssh/id_web")
    assert args[4:] == ["--jump", "ops@gw.example.com:2200"]
    v6 = Site(protocol="scp", host="fe80::1", port=2222)
    assert T.blamixshell_target(v6, sites.get) == ["--connect", "[fe80::1]:2222"]
    pw = Site(protocol="sftp", host="h", username="u", auth="password", password="secret")
    assert "secret" not in " ".join(T.blamixshell_target(pw, sites.get))       # never a password


def test_find_blamixshell(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_blamixshell_candidates", lambda: [])
    monkeypatch.setattr(T.shutil, "which", lambda name: None)
    assert T.find_blamixshell() is None
    app = tmp_path / "BlamixShell.exe"
    app.write_text("")
    assert T.find_blamixshell(str(app)) == [str(app)]                         # the packaged app: --connect directly
    cli = tmp_path / "blamixshell"
    cli.write_text("")
    assert T.find_blamixshell(str(cli)) == [str(cli), "gui"]                   # pip: the GUI is a sub-command
    monkeypatch.setattr(T, "_blamixshell_candidates", lambda: [str(app)])
    assert T.find_blamixshell() == [str(app)]
    monkeypatch.setattr(T, "_blamixshell_candidates", lambda: [])
    monkeypatch.setattr(T.shutil, "which", lambda name: "/usr/bin/blamixshell" if name == "blamixshell" else None)
    assert T.find_blamixshell() == ["/usr/bin/blamixshell", "gui"]


def test_open_in_blamixshell_starts_it(tmp_path, monkeypatch):
    started = []

    class P:
        def __init__(self, cmd, **kw):
            started.append(cmd)
    monkeypatch.setattr(T.subprocess, "Popen", P)
    monkeypatch.setattr(T, "find_blamixshell", lambda configured="": ["BlamixShell.exe"])
    site = Site(protocol="sftp", host="example.com", username="me")
    assert T.open_in_blamixshell(site, lambda sid: None) == ""
    assert started == [["BlamixShell.exe", "--connect", "me@example.com"]]
    monkeypatch.setattr(T, "find_blamixshell", lambda configured="": None)
    assert "isn't installed" in T.open_in_blamixshell(site, lambda sid: None)
