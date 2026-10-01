"""Folder compare & sync."""
from __future__ import annotations

import os
import time

import pytest

from blamixfiles.core import engine as E
from blamixfiles.core import sync as S
from blamixfiles.core.backends import open_backend
from blamixfiles.core.backends.local import LocalBackend
from blamixfiles.models import Site
from servers import PASSWORD, USER, FTPTestServer, SFTPTestServer, WebDAVTestServer
from test_backends import connect


def write(p, text, mtime=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def plan_for(local_root, remote_root, **opt):
    lb = LocalBackend()
    o = S.SyncOptions(**opt)
    loc = S.scan(lb, str(local_root), o.excludes)
    rem = S.scan(lb, str(remote_root), o.excludes)
    return S.compare(loc, rem, str(local_root), str(remote_root), o)


def kinds(plan):
    return {(a.kind, a.rel) for a in plan.actions}


@pytest.fixture
def trees(tmp_path):
    now = time.time()
    L, R = tmp_path / "L", tmp_path / "R"
    write(L / "same.txt", "same", now - 1000)
    write(R / "same.txt", "same", now - 1000)
    write(L / "new-here.txt", "x")
    write(L / "sub" / "deep" / "new.css", "body{}")
    write(R / "only-there.txt", "y")
    write(R / "old-dir" / "a.txt", "a")
    write(R / "old-dir" / "b.txt", "b")
    write(L / "edited.php", "v2 longer", now)
    write(R / "edited.php", "v1", now - 5000)
    write(L / "touched.txt", "abc", now)            # same size, newer here
    write(R / "touched.txt", "abc", now - 5000)
    write(L / "uploaded-before.txt", "zz", now - 5000)   # same size, server copy newer (earlier upload)
    write(R / "uploaded-before.txt", "zz", now)
    write(L / ".git" / "HEAD", "ref")                # excluded by default
    write(L / "node_modules" / "x.js", "1")
    return L, R


def test_upload_plan(trees):
    L, R = trees
    p = plan_for(L, R, direction="upload")
    assert kinds(p) == {("upload", "new-here.txt"), ("mkdir-remote", "sub"), ("mkdir-remote", "sub/deep"),
                        ("upload", "sub/deep/new.css"), ("upload", "edited.php"), ("upload", "touched.txt")}
    assert p.unchanged == 2                          # same.txt + uploaded-before.txt
    assert not any(a.kind in S.DELETES for a in p.actions)   # no mirror -> never deletes


def test_mirror_deletes_are_listed_and_collapsed(trees):
    L, R = trees
    p = plan_for(L, R, direction="upload", mirror=True)
    dels = {a.rel for a in p.actions if a.kind == S.DELETE_REMOTE}
    assert dels == {"only-there.txt", "old-dir"}     # the folder once, not each file inside


def test_download_and_two_way(trees):
    L, R = trees
    p = plan_for(L, R, direction="download")
    assert ("download", "only-there.txt") in kinds(p) and ("mkdir-local", "old-dir") in kinds(p)
    assert ("download", "edited.php") in kinds(p)        # one-way: the server copy wins
    p = plan_for(L, R, direction="both")
    k = kinds(p)
    assert ("upload", "edited.php") in k and ("download", "only-there.txt") in k
    assert ("download", "uploaded-before.txt") in k      # same size, newer there -> copied in two-way
    assert not any(a.kind in S.DELETES for a in p.actions)  # two-way never deletes


def test_conflicts(tmp_path):
    t = time.time() - 100
    write(tmp_path / "L" / "a.txt", "one", t)
    write(tmp_path / "R" / "a.txt", "three", t)
    write(tmp_path / "L" / "x", "file")
    (tmp_path / "R" / "x").mkdir(parents=True)
    p = plan_for(tmp_path / "L", tmp_path / "R", direction="both")
    assert {a.rel for a in p.actions if a.kind == S.CONFLICT} == {"a.txt", "x"}


def test_size_only_and_excludes(trees):
    L, R = trees
    p = plan_for(L, R, direction="upload", compare="size", excludes=["*.css", "sub"])
    k = kinds(p)
    assert ("upload", "touched.txt") not in k          # same size -> equal when comparing size only
    assert not any(rel.startswith("sub") for _, rel in k)
    assert any(rel.startswith(".git") for _, rel in k)  # our excludes replaced the defaults


def test_second_run_is_a_no_op(trees, tmp_path):
    L, R = trees
    lb = LocalBackend()
    eng = E.TransferEngine(lambda s: LocalBackend(), workers=2)
    site = Site(host="local")
    p = plan_for(L, R, direction="upload", mirror=True)
    S.apply(p, site, eng, lb, lb)
    assert eng.wait(20) and all(j.status == E.DONE for j in eng.jobs)
    again = plan_for(L, R, direction="upload", mirror=True)
    assert again.actions == [], again.summary()
    assert (R / "sub" / "deep" / "new.css").read_text() == "body{}"
    assert not (R / "old-dir").exists() and not (R / "only-there.txt").exists()
    eng.shutdown()


def test_disabled_actions_are_skipped(trees):
    L, R = trees
    lb = LocalBackend()
    eng = E.TransferEngine(lambda s: LocalBackend(), workers=1)
    p = plan_for(L, R, direction="upload", mirror=True)
    for a in p.actions:
        if a.kind == S.DELETE_REMOTE or a.rel == "edited.php":
            a.enabled = False
    S.apply(p, Site(host="x"), eng, lb, lb)
    eng.wait(20)
    assert (R / "only-there.txt").exists() and (R / "edited.php").read_text() == "v1"
    eng.shutdown()


@pytest.mark.parametrize("proto", ["sftp", "ftp", "webdav"])
def test_sync_against_real_server(tmp_path, proto):
    """Upload-mirror a tree to a server, then check the next compare finds nothing,
    even on FTP where upload times replace the original ones."""
    L, root = tmp_path / "site", tmp_path / "srv"
    root.mkdir()
    write(L / "index.html", "<h1>hi</h1>")
    write(L / "css" / "app.css", "body{}")
    write(L / "img" / "logo.svg", "<svg/>")
    write(root / "www" / "stale.html", "old")
    Server = {"sftp": SFTPTestServer, "ftp": FTPTestServer, "webdav": WebDAVTestServer}[proto]
    with Server(root) as srv:
        site = Site(protocol=proto, host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD)
        remote = connect(site)
        opt = S.SyncOptions(direction="upload", mirror=True, tolerance=2 if proto == "sftp" else 60)
        if proto == "webdav":
            remote.makedirs("/www")
        loc = S.scan(LocalBackend(), str(L), opt.excludes)
        rem = S.scan(remote, "/www", opt.excludes)
        plan = S.compare(loc, rem, str(L), "/www", opt)
        assert plan.counts() == {"mkdir-remote": 2, "upload": 3, "delete-remote": 1}
        eng = E.TransferEngine(lambda s: open_backend(s), workers=3)
        S.apply(plan, site, eng, remote, LocalBackend())
        assert eng.wait(20) and all(j.status == E.DONE for j in eng.jobs), [j.error for j in eng.jobs]
        assert (root / "www" / "css" / "app.css").read_text() == "body{}"
        assert not (root / "www" / "stale.html").exists()
        again = S.compare(S.scan(LocalBackend(), str(L), opt.excludes), S.scan(remote, "/www", opt.excludes),
                          str(L), "/www", opt)
        assert again.actions == [], again.summary()
        eng.shutdown()
        remote.close()


def test_profiles_in_vault(tmp_path):
    from blamixfiles.models import open_or_create
    store = open_or_create(tmp_path / "v.bfv", "pw", create=True)
    site = Site(name="web", host="h")
    store.upsert(site)
    prof = S.SyncProfile("Deploy site", site.id, "C:/proj/dist", "/var/www",
                         S.SyncOptions(direction="upload", mirror=True))
    store.save_profile(prof.to_dict())
    again = open_or_create(tmp_path / "v.bfv", "pw", create=False)
    loaded = S.SyncProfile.from_dict(again.find_profile("deploy SITE"))
    assert loaded.options.mirror and loaded.remote_dir == "/var/www"
    again.delete(site.id)                            # deleting the site drops its profiles
    assert again.find_profile("Deploy site") is None
