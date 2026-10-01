"""Folder watch: change detection, settling, ignore list, uploads through the engine and the CLI."""
from __future__ import annotations

import os
import threading
import time

from blamixfiles.core import engine as E
from blamixfiles.core import watch as W
from blamixfiles.core.backends import open_backend
from blamixfiles.models import Site
from servers import PASSWORD, USER, SFTPTestServer
from test_backends import connect


def bump(path, data):
    """Write and make sure the mtime moves (coarse filesystem clocks)."""
    path.write_bytes(data)
    t = time.time() + 5
    os.utime(path, (t, t))


def test_detects_new_and_changed_files_after_they_settle(tmp_path):
    (tmp_path / "old.txt").write_text("already here")
    w = W.FolderWatcher(str(tmp_path), lambda r: None, settle=1.0)
    w.snapshot = w.scan()                           # baseline (start() does this)
    assert w.poll(now=0) == []                      # existing files are not uploaded

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.css").write_text("a{}")
    assert w.poll(now=10) == []                     # seen, but not settled yet
    assert w.poll(now=11.2) == ["sub/new.css"]
    assert w.poll(now=20) == []                     # reported once

    bump(tmp_path / "old.txt", b"edited")
    assert w.poll(now=30) == []
    bump(tmp_path / "old.txt", b"edited again, still being written")
    assert w.poll(now=30.9) == []                   # changed again: the clock restarts
    assert w.poll(now=31.5) == []
    assert w.poll(now=32) == ["old.txt"]


def test_ignores_temp_files_and_project_junk(tmp_path):
    w = W.FolderWatcher(str(tmp_path), lambda r: None, settle=0)
    w.snapshot = w.scan()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_text("x")
    (tmp_path / "node_modules" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "lib" / "a.js").write_text("x")
    for junk in (".index.php.swp", "index.php~", "report.docx.tmp", "~$report.docx", ".DS_Store"):
        (tmp_path / junk).write_text("x")
    (tmp_path / "index.php").write_text("<?php")
    assert w.poll(now=0) == ["index.php"]
    assert W.parse_ignore("dist, *.map;\nbuild") == ("dist", "*.map", "build")


def test_temp_file_that_disappears_is_not_reported(tmp_path):
    w = W.FolderWatcher(str(tmp_path), lambda r: None, settle=1)
    w.snapshot = w.scan()
    (tmp_path / "save-in-progress").write_text("x")
    w.poll(now=0)
    (tmp_path / "save-in-progress").unlink()
    assert w.poll(now=5) == [] and w._pending == {}


def test_watch_uploads_through_the_engine(tmp_path):
    root = tmp_path / "srv"
    (root / "www").mkdir(parents=True)
    local = tmp_path / "site"
    local.mkdir()
    (local / "index.html").write_text("v1")
    with SFTPTestServer(root) as srv:
        site = Site(protocol="sftp", host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD)
        connect(site).close()
        eng = E.TransferEngine(lambda s: open_backend(s), workers=2)
        w = W.FolderWatcher(str(local), lambda rels: W.queue_uploads(eng, site, str(local), "/www", rels),
                            interval=0.1, settle=0.3)
        w.start()
        try:
            (local / "index.html").write_text("v2 with more bytes")
            (local / "css").mkdir()
            (local / "css" / "app.css").write_text("body{}")
            deadline = time.time() + 15
            while time.time() < deadline and not (root / "www" / "css" / "app.css").exists():
                time.sleep(0.1)
            assert eng.wait(15)
        finally:
            w.stop()
            eng.shutdown()
        assert (root / "www" / "index.html").read_text() == "v2 with more bytes"
        assert (root / "www" / "css" / "app.css").read_text() == "body{}"     # folder created
        assert all(j.status == E.DONE for j in eng.jobs)
        (local / "index.html").unlink()
        assert (root / "www" / "index.html").exists()                         # deletes never go up


def test_queue_uploads_does_not_double_queue(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    eng = E.TransferEngine(lambda s: None, workers=1)
    eng.set_paused(True)
    site = Site(protocol="sftp", host="h")
    try:
        assert len(W.queue_uploads(eng, site, str(tmp_path), "/r", ["a.txt"])) == 1
        assert W.queue_uploads(eng, site, str(tmp_path), "/r", ["a.txt"]) == []
        j = eng.jobs[0]
        assert j.dst == "/r/a.txt" and j.make_parents and j.policy == "overwrite"
    finally:
        eng.shutdown()


def test_cli_watch(tmp_path, capsys, monkeypatch):
    from blamixfiles import cli
    from blamixfiles.core import ssh
    from blamixfiles.models import open_or_create
    from blamixfiles.paths import vault_path
    root = tmp_path / "srv"
    (root / "www").mkdir(parents=True)
    local = tmp_path / "site"
    local.mkdir()
    with SFTPTestServer(root) as srv:
        site = Site(name="web", protocol="sftp", host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD)
        open_or_create(vault_path(), "master", create=True).upsert(site)
        monkeypatch.setenv("BLAMIXFILES_VAULT_PASSWORD", "master")
        try:
            ssh.open_client(site)
        except ssh.UnknownHostKey as e:
            ssh.trust_host_key(e.host_id, e.key)

        def edit_then_stop():
            time.sleep(1.0)
            (local / "page.php").write_text("<?php echo 1;")
            deadline = time.time() + 15
            while time.time() < deadline and not (root / "www" / "page.php").exists():
                time.sleep(0.1)
            time.sleep(0.3)
            cli.WATCH_STOP.set()                       # like Ctrl+C
        threading.Thread(target=edit_then_stop, daemon=True).start()
        try:
            cli.main(["watch", str(local), "web:/www", "--interval", "0.2"])
        except SystemExit as e:
            code = e.code
        cli._store_cache.clear()
        out = capsys.readouterr().out
        assert code == 0, out
        assert "Watching" in out and "/www/page.php" in out
        assert (root / "www" / "page.php").read_text() == "<?php echo 1;"
