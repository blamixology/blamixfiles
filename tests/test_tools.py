"""1.1 tools: search, folder size, bulk rename, server-to-server copy, queue order, schedules, sync hooks,
SSH keys, diagnostics (core + CLI; the dialogs are in test_gui.py)."""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

from blamixfiles.core import engine as E
from blamixfiles.core import rename as R
from blamixfiles.core import schedule as SC
from blamixfiles.core import search as F
from blamixfiles.core.backends import open_backend
from blamixfiles.core.backends.local import LocalBackend
from blamixfiles.models import Site
from servers import PASSWORD, USER, FTPTestServer, SFTPTestServer


def tree(base):
    (base / "a" / "deep" / "deeper").mkdir(parents=True)
    (base / "a" / "app.log").write_bytes(b"x" * 2000)
    (base / "a" / "deep" / "old.log").write_bytes(b"x" * 10)
    (base / "a" / "deep" / "deeper" / "photo.JPG").write_bytes(b"x" * 50_000)
    (base / "readme.txt").write_text("hi")
    (base / "node_modules" / "pkg").mkdir(parents=True)
    (base / "node_modules" / "pkg" / "x.log").write_text("skip me")
    old = time.time() - 40 * 86400
    os.utime(base / "a" / "deep" / "old.log", (old, old))


# ------------------------------------------------------------------ search / size
def test_parse_size_and_age():
    assert F.parse_size("10M") == 10 * 1024 ** 2 and F.parse_size("1.5k") == 1536 and F.parse_size("512") == 512
    assert F.parse_age("7d") == 7 * 86400 and F.parse_age("30m") == 1800
    for bad in ("ten", "5 parsecs"):
        with pytest.raises(ValueError):
            F.parse_size(bad)
    with pytest.raises(ValueError):
        F.parse_age("5")


def test_find_and_folder_size_local(tmp_path):
    base = tmp_path / "t"                                  # (tmp_path also holds the test's data folder)
    tree(base)
    b = LocalBackend()
    root = str(base)

    def names(q):
        return sorted(e.name for e in F.find(b, root, q))
    assert names(F.Query(name="*.log")) == ["app.log", "old.log"]          # node_modules skipped
    assert names(F.Query(name="PHOTO")) == ["photo.JPG"]                     # part of the name, any case
    assert names(F.Query(name="PHOTO", case_sensitive=True)) == []
    assert names(F.Query(name="*.log", min_size=1000)) == ["app.log"]
    assert names(F.Query(name="*.log", older_than=time.time() - 30 * 86400)) == ["old.log"]
    assert names(F.Query(kind="folder", name="deep*")) == ["deep", "deeper"]
    assert names(F.Query(name="*.log", max_depth=2)) == ["app.log"]
    first = next(F.find(b, root, F.Query()))                                 # nearest first
    assert os.path.dirname(first.path) == root
    stop = iter([False, True])
    assert len(list(F.find(b, root, F.Query(), lambda: next(stop, True)))) <= 3
    u = F.folder_size(b, root)
    assert u.bytes == 2000 + 10 + 50_000 + 2 + len("skip me") and u.files == 5 and u.folders == 5


def test_find_and_size_on_ftp(tmp_path):
    srv_root = tmp_path / "srv"
    srv_root.mkdir()
    tree(srv_root)
    with FTPTestServer(srv_root) as srv:
        b = open_backend(Site(protocol="ftp", host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD))
        try:
            hits = [e.path for e in F.find(b, "/", F.Query(name="*.JPG"))]
            assert hits == ["/a/deep/deeper/photo.JPG"]
            assert F.folder_size(b, "/a").bytes == 52_010
        finally:
            b.close()


# ------------------------------------------------------------------ bulk rename
def test_rename_rules():
    names = ["IMG_001.JPG", "IMG_002.JPG", "notes.txt"]
    plan = R.plan(names, R.Rule(find="IMG_", replace="holiday-"))
    assert [it.new for it in plan] == ["holiday-001.JPG", "holiday-002.JPG", "notes.txt"]
    assert [it.changes for it in plan] == [True, True, False]
    plan = R.plan(names, R.Rule(case="lower"))
    assert plan[0].new == "img_001.jpg"
    plan = R.plan(names[:2], R.Rule(template="photo-{n:03}{ext}", start=7))
    assert [it.new for it in plan] == ["photo-007.JPG", "photo-008.JPG"]
    plan = R.plan(["a1.txt", "b22.txt"], R.Rule(find=r"\d+", replace="#", regex=True))
    assert [it.new for it in plan] == ["a#.txt", "b#.txt"]
    plan = R.plan(["x.txt"], R.Rule(find="x", replace="a\\b"))                # backslash taken literally
    assert plan[0].new == "a\\b.txt" and plan[0].problem == "invalid character"
    plan = R.plan(["a.txt", "b.txt"], R.Rule(template="same{ext}"))
    assert all(it.problem == "same new name as another file" for it in plan)
    plan = R.plan(["a.txt"], R.Rule(find="a", replace="b"), existing={"a.txt", "b.txt"})
    assert plan[0].problem == "a file with this name already exists"
    plan = R.plan(["a.txt", "b.txt"], R.Rule(find="(a|b)", replace="(", regex=True))
    assert plan[0].problem.startswith("rule error") or plan[0].new
    plan = R.plan(["a.txt"], R.Rule(template="{nope}"))
    assert plan[0].problem.startswith("rule error")
    plan = R.plan(["x.txt"], R.Rule(find="x", replace="a:b"), windows=True)
    assert plan[0].problem == "invalid character"


def test_rename_apply_swaps_names(tmp_path):
    for n, text in (("a.txt", "A"), ("b.txt", "B"), ("c.txt", "C")):
        (tmp_path / n).write_text(text)
    plan = R.plan(["a.txt", "b.txt"], R.Rule(find="(a|b)", replace=r"\1", regex=True))
    # swap a <-> b with a template: a -> b, b -> a
    items = [R.Item("a.txt", "b.txt"), R.Item("b.txt", "a.txt"), R.Item("c.txt", "d.txt")]
    assert R.apply(LocalBackend(), str(tmp_path), items) == []
    assert (tmp_path / "b.txt").read_text() == "A" and (tmp_path / "a.txt").read_text() == "B"
    assert (tmp_path / "d.txt").read_text() == "C" and not list(tmp_path.glob("*.blamixfiles-rename"))
    assert plan


# ------------------------------------------------------------------ server -> server, queue order
def test_relay_between_servers_and_within_one(tmp_path):
    a_root, b_root = tmp_path / "a", tmp_path / "b"
    (a_root / "site" / "img").mkdir(parents=True)
    (a_root / "site" / "index.html").write_text("<h1>hi</h1>")
    (a_root / "site" / "img" / "logo.png").write_bytes(os.urandom(300_000))
    b_root.mkdir()
    with FTPTestServer(a_root) as fa, SFTPTestServer(b_root) as sb:
        src = Site(name="ftp", protocol="ftp", host="127.0.0.1", port=fa.port, username=USER, password=PASSWORD)
        dst = Site(name="sftp", protocol="sftp", host="127.0.0.1", port=sb.port, username=USER, password=PASSWORD)
        from test_backends import connect
        connect(dst).close()                                         # trust the host key
        log = tmp_path / "t.log"
        eng = E.TransferEngine(lambda s: open_backend(s), workers=2, log_path=log)
        b = open_backend(src)
        entry = b.stat("/site")
        b.close()
        root_job = eng.relay(src, entry, dst, "/")
        assert eng.wait(30), "relay did not finish"
        assert root_job.status == E.DONE
        assert (b_root / "site" / "index.html").read_text() == "<h1>hi</h1>"
        assert (b_root / "site" / "img" / "logo.png").read_bytes() == (a_root / "site" / "img" / "logo.png").read_bytes()
        # inside one server: another folder
        b = open_backend(src)
        f = b.stat("/site/index.html")
        b.close()
        eng.relay(src, f, src, "/site/img")
        assert eng.wait(20)
        assert (a_root / "site" / "img" / "index.html").read_text() == "<h1>hi</h1>"
        # the policy applies: skip what exists
        eng.policy = "skip"
        j = eng.relay(src, f, src, "/site/img")
        assert eng.wait(20) and j.status == E.SKIPPED
        eng.shutdown()
        assert any("\trelay\t" in ln for ln in E.read_log(log))


def test_queue_move():
    eng = E.TransferEngine(lambda s: None, workers=1)
    eng.set_paused(True)
    site = Site(name="x")
    jobs = [E.Job("upload", site, f"/f{i}", f"/r{i}") for i in range(4)]
    eng.add(jobs)
    eng.move(jobs[3].id, "top")
    assert [j.src for j in eng.jobs] == ["/f3", "/f0", "/f1", "/f2"]
    eng.move(jobs[3].id, "down")
    assert [j.src for j in eng.jobs] == ["/f0", "/f3", "/f1", "/f2"]
    eng.move(jobs[0].id, "bottom")
    assert [j.src for j in eng.jobs] == ["/f3", "/f1", "/f2", "/f0"]
    eng.move(jobs[1].id, "up")
    assert [j.src for j in eng.jobs] == ["/f1", "/f3", "/f2", "/f0"]
    jobs[2].status = E.DONE                                        # finished jobs can't be moved
    eng.move(jobs[2].id, "top")
    assert eng.jobs[0].src == "/f1"
    eng.cancel()
    eng.shutdown()


# ------------------------------------------------------------------ schedules
def test_schedule_specs():
    assert SC.When.parse(daily="2:05").daily == "02:05"
    assert SC.When.parse(every="6h").every_hours == 6
    for bad in (dict(daily="25:00"), dict(every="0"), dict(every="24"), dict()):
        with pytest.raises(ValueError):
            SC.When.parse(**bad)
    line = SC.cron_line("Deploy web", SC.When(daily="02:30"))
    assert line.startswith("30 2 * * * ") and line.endswith("# blamixfiles-sync:Deploy web")
    assert "'Deploy web'" in line and "--yes" in line
    assert SC.cron_line("x", SC.When(every_hours=6)).startswith("0 */6 * * * ")
    lines = ["MAILTO=me", SC.cron_line("Deploy web", SC.When(daily="02:30")), SC.cron_line("x", SC.When(every_hours=6))]
    assert SC.parse_cron(lines) == {"Deploy web": "every day at 02:30", "x": "every 6 hour(s)"}
    assert SC.cron_without(lines, "x") == lines[:2]
    args = SC.windows_create_args("Deploy web", SC.When(daily="02:30"))
    assert args[:4] == ["schtasks", "/Create", "/F", "/TN"] and args[4] == "\\BlamixFiles\\Sync Deploy web"
    assert args[-4:] == ["/SC", "DAILY", "/ST", "02:30"] and "sync" in args[6] and "--yes" in args[6]
    assert SC.windows_create_args("a/b", SC.When(every_hours=3))[-4:] == ["/SC", "HOURLY", "/MO", "3"]
    csv = '"\\BlamixFiles\\Sync Deploy web","10/8/2026 2:30:00 AM","Ready"\n"\\Other\\Task","N/A","Ready"\n'
    assert SC.parse_schtasks_csv(csv) == {"Deploy web": "next run 10/8/2026 2:30:00 AM"}


def test_schedule_install_remove_with_cron(monkeypatch):
    crontab = {"lines": ["0 0 * * * other-job"]}

    class R_:
        def __init__(self, out="", rc=0):
            self.stdout, self.stderr, self.returncode = out, "", rc

    def fake_run(cmd, stdin=None):
        if cmd == ["crontab", "-l"]:
            return R_("\n".join(crontab["lines"]) + "\n")
        if cmd == ["crontab", "-"]:
            crontab["lines"] = stdin.splitlines()
            return R_()
        raise AssertionError(cmd)
    monkeypatch.setattr(SC, "_run", fake_run)
    monkeypatch.setattr(SC.sys, "platform", "linux")
    SC.install("Deploy", SC.When(daily="01:00"))
    SC.install("Deploy", SC.When(every_hours=2))                  # replaces, doesn't add a second line
    assert crontab["lines"][0] == "0 0 * * * other-job" and len(crontab["lines"]) == 2
    assert SC.listing() == {"Deploy": "every 2 hour(s)"}
    assert SC.remove("Deploy") and crontab["lines"] == ["0 0 * * * other-job"]
    assert not SC.remove("Deploy")


# ------------------------------------------------------------------ sync hooks
def test_sync_hooks_command_and_webhook(tmp_path):
    import http.server
    import threading
    from blamixfiles.core import sync as S
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = tmp_path / "hook.txt"
    cmd = f'"{sys.executable}" -c "import os; open(r\'{out}\', \'w\').write(os.environ[\'BLAMIXFILES_PROFILE\'] + \' \' + os.environ[\'BLAMIXFILES_STATUS\'] + \' \' + os.environ[\'BLAMIXFILES_FILES\'])"'
    notes = S.run_hooks("Deploy", cmd, f"http://127.0.0.1:{srv.server_port}/hook",
                        {"status": "ok", "files": 3, "bytes": 10, "failed": 0})
    srv.shutdown()
    assert out.read_text() == "Deploy ok 3"
    assert got["body"] == {"profile": "Deploy", "status": "ok", "files": 3, "bytes": 10, "failed": 0}
    assert notes[0].startswith("after-sync command exited with 0") and notes[1] == "webhook answered 204"
    notes = S.run_hooks("x", "", "http://127.0.0.1:9/nothing", {"status": "ok"})
    assert notes[0].startswith("webhook failed")
    assert S.SyncProfile.from_dict({"name": "p", "site_id": "s", "local_dir": "l", "remote_dir": "r"}).after_command == ""


# ------------------------------------------------------------------ SSH keys
def test_keygen_and_install(tmp_path):
    import paramiko
    from blamixfiles.core import keys as K
    priv = tmp_path / "keys" / "id_test"
    line = K.generate(priv, "secret phrase", "me@test")
    assert line.startswith("ssh-ed25519 ") and line.endswith(" me@test")
    assert (tmp_path / "keys" / "id_test.pub").read_text().strip() == line
    assert K.read_public_key(str(priv) + ".pub") == line
    assert K.private_mode_ok(priv)
    key = paramiko.Ed25519Key.from_private_key_file(str(priv), password="secret phrase")
    assert line.split()[1] == key.get_base64()
    with pytest.raises(K.KeyError_):
        K.generate(priv)                                            # never overwrite silently
    (tmp_path / "bad.pub").write_text("not a key")
    with pytest.raises(K.KeyError_):
        K.read_public_key(tmp_path / "bad.pub")

    root = tmp_path / "home"
    root.mkdir()
    with SFTPTestServer(root) as srv:
        from test_backends import connect
        site = Site(protocol="sftp", host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD)
        b = connect(site)
        try:
            assert K.install(b, line) is True
            assert K.install(b, line) is False                         # already there: no duplicate
            other = K.generate(tmp_path / "keys" / "id_two", "", "two")
            assert K.install(b, other) is True
            home = b.home()
            text = b.read_bytes(b.join(home, ".ssh/authorized_keys")).decode()
            assert text.splitlines() == [line, other]
            if sys.platform != "win32":                                 # (the test server can't chmod on Windows)
                assert oct(b.stat(b.join(home, ".ssh/authorized_keys")).mode & 0o777) == "0o600"
        finally:
            b.close()


# ------------------------------------------------------------------ diagnostics
def test_diagnostics_report_and_crash_files(tmp_path):
    from blamixfiles import diagnostics as D
    text = D.report({"theme": "Nord", "window_geometry": "AAAA", "limit_up_kb": 0})
    assert "BlamixFiles" in text and "theme = 'Nord'" in text and "window_geometry" not in text
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        path = D.write_crash(*sys.exc_info())
    assert path is not None and "RuntimeError: boom" in path.read_text()
    assert D.list_crashes()[0] == path and D.new_crashes(0) and not D.new_crashes(time.time() + 5)
    assert "Crash reports: 1" in D.report()
