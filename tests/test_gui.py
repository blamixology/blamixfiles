"""End-to-end through the real window (offscreen): connect, browse, upload, edit + save."""
from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from blamixfiles.core import engine as E  # noqa: E402
from blamixfiles.models import Site, Store  # noqa: E402
from blamixfiles.settings import Settings  # noqa: E402
from blamixfiles.vault import Vault  # noqa: E402
from servers import PASSWORD, USER, FTPTestServer, SFTPTestServer  # noqa: E402


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    from blamixfiles.ui.bridge import init_bridge
    from blamixfiles.ui.theme import apply_palette
    init_bridge()
    apply_palette(a)
    return a


def wait(app, cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    import blamixfiles.ui.dialogs as D
    monkeypatch.setattr(D, "ask_host_key", lambda *a: True)        # trust the test server
    monkeypatch.setattr(D, "ask_certificate", lambda *a: True)
    from blamixfiles.ui.main_window import MainWindow
    store = Store(Vault.create(tmp_path / "v.bfv", "pw", n_log2=10), {})
    win = MainWindow(store, Settings())
    win.engine.policy = "overwrite"
    win.resize(1400, 860)
    win.show()
    yield win
    win.engine.cancel()
    for t in win.site_tabs():
        t.close()
    win.engine.shutdown()
    win.hide()


@pytest.mark.parametrize("proto", ["sftp", "ftps"])
def test_browse_upload_edit_save(app, window, tmp_path, proto):
    root = tmp_path / "srv"
    root.mkdir()
    (root / "etc").mkdir()
    (root / "etc" / "nginx.conf").write_bytes(b"server {\r\n    listen 80;\r\n}\r\n")
    local = tmp_path / "home"
    local.mkdir()
    (local / "index.html").write_text("<h1>hi</h1>\n")
    Server = SFTPTestServer if proto == "sftp" else FTPTestServer
    kw = {} if proto == "sftp" else {"tls": True, "certdir": tmp_path}
    with Server(root, **kw) as srv:
        site = Site(name="test", protocol=proto, host="127.0.0.1", port=srv.port,
                    username=USER, password=PASSWORD, local_dir=str(local), production=True)
        window.store.upsert(site)
        window.reload_sites()
        window.open_site(window.store.sites[site.id])
        tab = window.site_tabs()[0]
        assert wait(app, lambda: tab.remote.path and tab.remote.entries), tab.remote.status.text()
        assert {e.name for e in tab.remote.entries} == {"etc"}
        assert wait(app, lambda: tab.local.entries)

        # upload by "dropping" the local file on the remote pane
        tab.remote.open_dir("/etc")
        assert wait(app, lambda: tab.remote.path == "/etc" and tab.remote.entries)
        tab.transfer([e for e in tab.local.entries if e.name == "index.html"], tab.local, "")
        assert wait(app, lambda: (root / "etc" / "index.html").exists() and window.engine.pending() == 0)
        assert wait(app, lambda: any(e.name == "index.html" for e in tab.remote.entries)), "pane refreshed"

        # edit nginx.conf in the built-in editor and save it back
        conf = next(e for e in tab.remote.entries if e.name == "nginx.conf")
        window.open_editor(tab.remote_session, conf)
        from blamixfiles.ui.editor import EditorTab
        ed = window.tabs.currentWidget()
        assert isinstance(ed, EditorTab)
        assert wait(app, lambda: ed.loaded)
        assert ed.lexer_name.lower().startswith("nginx")
        assert "CRLF" in ed.status.text()
        new = ed.ed.toPlainText().replace("80", "8080")
        ed.ed.selectAll()
        ed.ed.insertPlainText(new)              # like typing (setPlainText resets "modified")
        assert ed.dirty and ed.tab_title().startswith("•")
        if proto == "sftp":
            window.grab().save(str(tmp_path.parent / f"shot-editor-{proto}.png"))
        ed.save()
        assert wait(app, lambda: not ed.dirty)
        assert (root / "etc" / "nginx.conf").read_bytes() == b"server {\r\n    listen 8080;\r\n}\r\n"

        # someone else changes it -> conflict is detected (answer Cancel)
        (root / "etc" / "nginx.conf").write_bytes(b"server {\r\n    listen 9090;\r\n}\r\n")
        from PySide6.QtWidgets import QMessageBox
        asked = []
        orig = QMessageBox.exec

        def fake_exec(box):
            asked.append(box.text())
            return 0
        QMessageBox.exec = fake_exec
        try:
            ed.ed.insertPlainText("# note\n")
            ed.save()
            assert wait(app, lambda: asked and not ed._saving)
        finally:
            QMessageBox.exec = orig
        assert "changed by someone else" in asked[0]
        assert b"9090" in (root / "etc" / "nginx.conf").read_bytes()
        ed.ed.document().setModified(False)
        if proto == "sftp":
            window.tabs.setCurrentIndex(window.tabs.indexOf(tab))
            wait(app, lambda: False, 0.3)
            window.grab().save(str(tmp_path.parent / f"shot-browser-{proto}.png"))
        assert all(j.status == E.DONE for j in window.engine.jobs)


def test_saved_queue_offered_on_start_and_speed_menu(app, tmp_path, monkeypatch):
    """Transfers left over from last time come back paused with a Resume banner."""
    import blamixfiles.ui.dialogs as D
    monkeypatch.setattr(D, "ask_host_key", lambda *a: True)
    from blamixfiles.core.queue_store import QueueStore
    from blamixfiles.paths import data_dir
    from blamixfiles.ui.main_window import MainWindow
    root = tmp_path / "srv"
    root.mkdir()
    src = tmp_path / "left-over.txt"
    src.write_text("from last session")
    with SFTPTestServer(root) as srv:
        store = Store(Vault.create(tmp_path / "v.bfv", "pw", n_log2=10), {})
        site = Site(name="srv", protocol="sftp", host="127.0.0.1", port=srv.port,
                    username=USER, password=PASSWORD)
        store.upsert(site)
        qs = QueueStore(data_dir() / "queue.db")
        qs.sync(E.Job("upload", site, str(src), "/left-over.txt", size=17, status=E.RUNNING))
        qs.close()

        win = MainWindow(store, Settings())
        win.show()
        try:
            assert win.engine.paused and win.queue.banner.isVisibleTo(win)
            assert "1 transfer" in win.queue.banner_text.text()
            assert not (root / "left-over.txt").exists()
            win.queue._resume_restored()          # the "Resume" button
            # no tab was open for the site: the connector opens one and connects
            assert wait(app, lambda: (root / "left-over.txt").exists() and win.engine.pending() == 0, 20)
            assert (root / "left-over.txt").read_text() == "from last session"
            assert len(win.site_tabs()) == 1

            win.queue.set_limit("upload", 512)
            assert win.engine.limits["upload"].rate == 512 * 1024
            assert "512 KB/s" in win.queue.speed_btn.toolTip()
            assert Settings()["limit_up_kb"] == 512            # remembered
        finally:
            for t in win.site_tabs():
                t.close()
            win.engine.shutdown()
            win.hide()


def test_sync_dialog_preview_apply_and_profile(app, window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from blamixfiles.ui.sync_dialog import SyncDialog
    root = tmp_path / "srv"
    (root / "www").mkdir(parents=True)
    (root / "www" / "stale.html").write_text("old")
    local = tmp_path / "site"
    (local / "css").mkdir(parents=True)
    (local / "index.html").write_text("<h1>new</h1>")
    (local / "css" / "app.css").write_text("body{}")
    with SFTPTestServer(root) as srv:
        site = Site(name="web", protocol="sftp", host="127.0.0.1", port=srv.port,
                    username=USER, password=PASSWORD, local_dir=str(local), remote_dir="/www",
                    production=True)
        window.store.upsert(site)
        window.open_site(window.store.sites[site.id])
        tab = window.site_tabs()[0]
        assert wait(app, lambda: tab.remote.path == "/www" and tab.local.path == str(local))

        dlg = SyncDialog(window, tab)
        dlg.mirror.setChecked(True)
        dlg.run_compare()
        assert wait(app, lambda: dlg.plan is not None), dlg.summary.text()
        rows = {dlg.tree.topLevelItem(i).text(1): dlg.tree.topLevelItem(i).text(0)
                for i in range(dlg.tree.topLevelItemCount())}
        assert rows == {"index.html": "Upload", "css/": "Create folder on server",
                        "css/app.css": "Upload", "stale.html": "Delete on server"}
        assert "delete on server" in dlg.summary.text()

        warned = []
        monkeypatch.setattr(QMessageBox, "warning",
                            staticmethod(lambda *a, **k: warned.append(a[2]) or QMessageBox.Yes))
        dlg.apply()
        assert wait(app, lambda: (root / "www" / "css" / "app.css").exists() and window.engine.pending() == 0)
        assert "PRODUCTION" in warned[0] and "stale.html" in warned[0]
        assert not (root / "www" / "stale.html").exists()

        # save as a profile, then run it again from the Sync menu: nothing left to do
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Deploy web", True)))
        dlg.save_profile()
        assert window.store.find_profile("deploy web")["options"]["mirror"] is True
        assert any("Deploy web" in a.text() for a in window.sync_menu.actions())
        from blamixfiles.core.sync import SyncProfile
        again = SyncDialog(window, tab, SyncProfile.from_dict(window.store.find_profile("Deploy web")),
                           auto_compare=True)
        assert wait(app, lambda: again.plan is not None)
        assert again.plan.actions == [] and "Nothing to do" in again.summary.text()
        if True:
            again.show()
            dlg.show()
            wait(app, lambda: False, 0.2)
            dlg.grab().save(str(tmp_path.parent / "shot-sync.png"))


def test_folder_tree_drag_out_keepalive_and_restore(app, tmp_path, monkeypatch):
    import blamixfiles.ui.dialogs as D
    monkeypatch.setattr(D, "ask_host_key", lambda *a: True)
    monkeypatch.setattr(D, "ask_certificate", lambda *a: True)
    from blamixfiles.ui.file_pane import LazyRemoteFiles
    from blamixfiles.ui.main_window import MainWindow
    root = tmp_path / "srv"
    (root / "var" / "www" / "assets").mkdir(parents=True)
    (root / "var" / "log").mkdir()
    (root / "var" / "www" / "index.html").write_text("<h1>x</h1>")
    (root / "var" / "www" / "assets" / "a.css").write_text("a{}")
    vault = Vault.create(tmp_path / "v.bfv", "pw", n_log2=10)
    with FTPTestServer(root, tls=True, certdir=tmp_path) as srv:
        store = Store(vault, {})
        site = Site(name="ftp", protocol="ftps", host="127.0.0.1", port=srv.port, username=USER,
                    password=PASSWORD, remote_dir="/var/www", local_dir=str(tmp_path))
        store.upsert(site)
        win = MainWindow(store, Settings())
        win.show()
        win.open_site(store.sites[site.id])
        tab = win.site_tabs()[0]
        assert wait(app, lambda: tab.remote.path == "/var/www" and tab.remote.entries)

        # the tree shows /var/www selected with its subfolder, built from the listing
        cur = tab.remote.folders.currentItem()
        assert cur.data(0, Qt.UserRole) == "/var/www"
        assert [cur.child(i).text(0) for i in range(cur.childCount())] == ["assets"]
        # expanding /var lists its siblings lazily; clicking one navigates
        var = cur.parent()
        var.setData(0, Qt.UserRole + 1, False)
        tab.remote.folders._expanded(var)
        assert wait(app, lambda: var.childCount() == 2)
        tab.remote.folders.navigate.emit("/var/log")
        assert wait(app, lambda: tab.remote.path == "/var/log")
        tab.remote.open_dir("/var/www")
        assert wait(app, lambda: tab.remote.path == "/var/www" and tab.remote.entries)

        # dropping on the desktop: files are downloaded when the OS asks for them
        entries = [e for e in tab.remote.entries]
        md = LazyRemoteFiles(tab.remote, entries)
        assert md.hasFormat("text/uri-list")
        urls = md.urls()
        got = {os.path.basename(u.toLocalFile()) for u in urls}
        assert got == {"index.html", "assets"}
        local = [u.toLocalFile() for u in urls]
        assert open(next(p for p in local if p.endswith("index.html"))).read() == "<h1>x</h1>"
        assert os.path.exists(os.path.join(next(p for p in local if p.endswith("assets")), "a.css"))

        # keep-alive pokes an idle FTP login
        sent = []
        b = tab.remote_session.backend
        orig = b.keepalive
        monkeypatch.setattr(b, "keepalive", lambda: (sent.append(1), orig()))
        tab.remote_session.keepalive()
        assert wait(app, lambda: sent)

        # close -> the tab and its folder come back next time
        win.close()
        assert Settings()["open_tabs"][0]["remote"] == "/var/www"
        win2 = MainWindow(Store(vault, {"sites": [vars(store.sites[site.id])]}), Settings())
        win2.show()
        win2.restore_tabs()
        t2 = win2.site_tabs()[0]
        assert wait(app, lambda: t2.remote.path == "/var/www")
        for t in win2.site_tabs():
            t.close()
        win2.engine.shutdown()
        win2.hide()


def test_bookmarks_and_command_palette(app, window, tmp_path, monkeypatch):
    from blamixfiles.ui import palette as P
    root = tmp_path / "srv"
    (root / "var" / "www").mkdir(parents=True)
    (root / "etc").mkdir()
    with SFTPTestServer(root) as srv:
        site = Site(name="web-01", protocol="sftp", host="127.0.0.1", port=srv.port,
                    username=USER, password=PASSWORD, remote_dir="/etc")
        window.store.upsert(site)
        window.open_site(window.store.sites[site.id])
        tab = window.site_tabs()[0]
        assert wait(app, lambda: tab.remote.path == "/etc")
        tab.remote.open_dir("/var/www")
        assert wait(app, lambda: tab.remote.path == "/var/www")

        # bookmark /var/www from the pane's star menu
        tab.remote._fill_bookmarks()
        add = next(a for a in tab.remote.bm_menu.actions() if a.text().startswith("Bookmark"))
        add.trigger()
        assert window.store.bookmarks == [{"site_id": site.id, "path": "/var/www", "name": "www"}]
        tab.remote.open_dir("/etc")
        assert wait(app, lambda: tab.remote.path == "/etc")

        # Ctrl+K: type "www", Enter -> back in /var/www
        seen = {}

        class Capture(P.CommandPalette):
            def exec(self):
                seen["p"] = self
                self.q.setText("www")
                self._run()
                return 1
        monkeypatch.setattr(P, "CommandPalette", Capture)
        window.show_palette()
        titles = [seen["p"].list.item(i).text() for i in range(seen["p"].list.count())]
        assert titles and titles[0].startswith("/var/www")
        assert wait(app, lambda: tab.remote.path == "/var/www")

        # actions and sites are searchable too
        class Look(P.CommandPalette):
            def exec(self):
                seen["q"] = self
                return 0
        monkeypatch.setattr(P, "CommandPalette", Look)
        window.show_palette()
        pal = seen["q"]
        pal.q.setText("web-0")
        assert pal.list.item(0).text().startswith("web-01")
        pal.q.setText("sftp://me@host.example")
        assert pal.list.item(0).text().startswith("Quick connect")


def test_watch_folder_uploads_changes(app, window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    root = tmp_path / "srv"
    (root / "www").mkdir(parents=True)
    local = tmp_path / "site"
    local.mkdir()
    (local / "index.html").write_text("v1")
    with SFTPTestServer(root) as srv:
        site = Site(name="web", protocol="sftp", host="127.0.0.1", port=srv.port,
                    username=USER, password=PASSWORD, local_dir=str(local), remote_dir="/www")
        window.store.upsert(site)
        window.open_site(window.store.sites[site.id])
        tab = window.site_tabs()[0]
        assert wait(app, lambda: tab.remote.path == "/www" and tab.remote_session.backend is not None)
        asked = []
        monkeypatch.setattr(QMessageBox, "exec", lambda self: asked.append(self.text()) or QMessageBox.Yes)
        tab.watch_btn.setChecked(True)
        assert tab.watcher is not None and asked and "not</b> deleted" in asked[0]
        assert not tab.watch_bar.isHidden()
        tab.watcher.interval, tab.watcher.settle = 0.1, 0.2
        (local / "index.html").write_text("v2, edited")
        up = root / "www" / "index.html"
        assert wait(app, lambda: up.exists() and up.read_text() == "v2, edited", 15)
        assert wait(app, lambda: "1 uploaded" in tab.watch_text.text())
        assert wait(app, lambda: any(e.name == "index.html" for e in tab.remote.entries)), "pane refreshed"
        window.grab().save(str(tmp_path.parent / "shot-watch.png"))
        tab.watch_btn.setChecked(False)
        assert tab.watcher is None and tab.watch_bar.isHidden()


def test_gui_edit_in_other_app_uploads_and_detects_conflicts(app, window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    import blamixfiles.ui.external as X
    root = tmp_path / "srv"
    (root / "www").mkdir(parents=True)
    (root / "www" / "app.js").write_text("console.log(1)\n")
    launched = []
    monkeypatch.setattr(X, "launch", lambda program, path: launched.append(path))
    answers = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: answers.pop(0) if answers else QMessageBox.Yes)
    with SFTPTestServer(root) as srv:
        site = Site(name="web", protocol="sftp", host="127.0.0.1", port=srv.port, username=USER,
                    password=PASSWORD, remote_dir="/www")
        window.store.upsert(site)
        window.open_site(window.store.sites[site.id])
        tab = window.site_tabs()[0]
        assert wait(app, lambda: tab.remote.path == "/www" and tab.remote.entries)
        entry = next(e for e in tab.remote.entries if e.name == "app.js")
        window.edit_external(entry, tab.remote)
        assert wait(app, lambda: launched), "opened in the other app"
        local = launched[0]
        assert open(local).read() == "console.log(1)\n"
        ext = window.external
        ext.edits.settle = 0.2
        assert window.external_btn.isVisibleTo(window) and "1 file" in window.external_btn.text()

        # saved in the other app -> asked -> uploaded
        open(local, "w").write("console.log(2)\n")
        answers[:] = [QMessageBox.Yes]
        assert wait(app, lambda: (root / "www" / "app.js").read_text() == "console.log(2)\n", 10)
        e = ext.edits.edits[0]
        assert wait(app, lambda: e.uploads == 1)

        # someone changes the server copy, then we save again -> conflict -> keep theirs
        time.sleep(1.1)
        (root / "www" / "app.js").write_text("console.log('theirs')\n")
        open(local, "w").write("console.log(3)\n")
        answers[:] = [QMessageBox.Yes, QMessageBox.Cancel]          # upload? yes. overwrite? no
        assert wait(app, lambda: not answers and id(e) not in ext._busy, 10)
        time.sleep(0.3)
        assert (root / "www" / "app.js").read_text() == "console.log('theirs')\n"

        # next save: no conflict any more (their version is the base) -> uploaded
        open(local, "w").write("console.log(4)\n")
        answers[:] = [QMessageBox.Yes]
        assert wait(app, lambda: (root / "www" / "app.js").read_text() == "console.log(4)\n", 10)
        ext.stop(e)
        assert not window.external_btn.isVisibleTo(window)


# ------------------------------------------------------------------ 1.0 features
def test_controls_have_accessible_names(app, tmp_path):
    """Every button / search box in the window and in the site dialog can be named by a screen reader."""
    from PySide6.QtWidgets import QAbstractButton, QLineEdit

    from blamixfiles.ui.dialogs import SiteDialog
    from blamixfiles.ui.main_window import MainWindow
    store = Store(Vault.create(tmp_path / "v.bfv", "pw", n_log2=10), {})
    site = Site(name="web", protocol="ftp", host="127.0.0.1", username=USER, password=PASSWORD, local_dir=str(tmp_path))
    store.upsert(site)
    win = MainWindow(store, Settings())
    win.show()
    app.processEvents()
    unnamed = [(type(w).__name__, w.toolTip()) for w in win.findChildren(QAbstractButton)
               if not (w.accessibleName() or w.text().strip())]
    unnamed += [("QLineEdit", w.placeholderText()) for w in win.findChildren(QLineEdit)
                if not w.accessibleName() and w.isVisibleTo(win) and w.placeholderText()]
    assert not unnamed, unnamed
    assert win.site_tree.accessibleName() == "Servers" and win.queue.tree.accessibleName() == "Transfer queue"
    assert win.sidebar_btn.accessibleName() == "Show/hide the server list"

    dlg = SiteDialog(site, [], win)
    dlg.show()
    app.processEvents()
    assert not [w for w in dlg.findChildren(QAbstractButton) if not (w.accessibleName() or w.text().strip())]
    dlg.close()
    win.engine.shutdown()
    win.close()


def test_site_limits_in_dialog_and_transfer_log_dialog(app, tmp_path):
    from blamixfiles.core.engine import Job, TransferLog
    from blamixfiles.ui.dialogs import SiteDialog, TransferLogDialog
    site = Site(name="web", protocol="sftp", host="h", limit_up_kb=300, limit_down_kb=0)
    dlg = SiteDialog(site, [], None)
    assert dlg.limit_up.value() == 300 and dlg.limit_down.value() == 0
    dlg.limit_down.setValue(150)
    s = dlg._collect()
    assert (s.limit_up_kb, s.limit_down_kb) == (300, 150)
    assert Site.from_dict({"name": "old"}).limit_up_kb == 0          # sites saved before 1.0 still load

    log = tmp_path / "transfers.log"
    j1 = Job("upload", site, "/local/a.txt", "/srv/a.txt", size=5)
    j1.status, j1.done = E.DONE, 5
    j2 = Job("download", site, "/srv/b.txt", "/local/b.txt", size=9)
    j2.status, j2.error = E.FAILED, "connection lost"
    for j in (j1, j2):
        TransferLog(log).add(j)
    d = TransferLogDialog(log)
    text = d.view.toPlainText().splitlines()
    assert "b.txt" in text[0] and "✖" in text[0] and "connection lost" in text[0]        # newest first
    assert "a.txt" in text[1] and "✔" in text[1]
    assert TransferLogDialog(tmp_path / "none.log").view.toPlainText() == "Nothing transferred yet."


def test_f6_switches_between_the_two_lists(app, tmp_path):
    from blamixfiles.ui.main_window import MainWindow
    with FTPTestServer(tmp_path) as srv:
        store = Store(Vault.create(tmp_path / "v.bfv", "pw", n_log2=10), {})
        site = Site(name="ftp", protocol="ftp", host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD,
                    local_dir=str(tmp_path))
        store.upsert(site)
        win = MainWindow(store, Settings())
        win.show()
        win.open_site(store.sites[site.id])
        tab = win.site_tabs()[0]
        assert wait(app, lambda: tab.remote.entries is not None and tab.remote.path)
        tab.local.tree.setFocus()
        win.switch_pane()
        assert wait(app, lambda: tab.remote.tree.hasFocus())
        win.switch_pane()
        assert wait(app, lambda: tab.local.tree.hasFocus())
        win.engine.cancel()
        tab.close()
        win.engine.shutdown()


# ------------------------------------------------------------------ 1.1 tools
def test_tools_search_size_rename_compare_relay(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from blamixfiles.ui import editor as ED
    from blamixfiles.ui import tools as TL
    from blamixfiles.ui.main_window import MainWindow
    srv_root, loc = tmp_path / "srv", tmp_path / "loc"
    (srv_root / "pics").mkdir(parents=True)
    for i in range(3):
        (srv_root / "pics" / f"IMG_{i}.jpg").write_bytes(b"x" * (1000 * (i + 1)))
    (srv_root / "notes.txt").write_text("one\ntwo\n")
    loc.mkdir()
    (loc / "notes.txt").write_text("one\nTWO\n")
    with FTPTestServer(srv_root) as srv:
        store = Store(Vault.create(tmp_path / "v.bfv", "pw", n_log2=10), {})
        site = Site(name="ftp", protocol="ftp", host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD,
                    local_dir=str(loc))
        store.upsert(site)
        win = MainWindow(store, Settings())
        win.engine.policy = "overwrite"
        win.show()
        win.open_site(store.sites[site.id])
        tab = win.site_tabs()[0]
        try:
            assert wait(app, lambda: tab.remote.path == "/" and any(e.name == "pics" for e in tab.remote.entries))
            assert wait(app, lambda: tab.local.path == str(loc) and tab.local.entries)

            # search the server
            dlg = TL.SearchDialog(tab.remote, win)
            dlg.name.setText("*.jpg")
            dlg.larger.setText("1.5k")
            dlg.start()
            assert wait(app, lambda: not dlg._running)
            found = sorted(dlg.results.topLevelItem(i).text(0) for i in range(dlg.results.topLevelItemCount()))
            assert found == ["/pics/IMG_1.jpg", "/pics/IMG_2.jpg"], found
            dlg.larger.setText("lots")
            dlg.start()
            assert "Not a size" in dlg.status.text()
            dlg.reject()

            # folder size, shown in the list
            pics = next(e for e in tab.remote.entries if e.name == "pics")
            tab.remote.calculate_size(pics)
            item = lambda: next(tab.remote.tree.topLevelItem(i) for i in range(tab.remote.tree.topLevelItemCount())  # noqa: E731
                                if getattr(tab.remote.tree.topLevelItem(i), "entry", None) is not None
                                and tab.remote.tree.topLevelItem(i).entry.name == "pics")
            assert wait(app, lambda: item().text(1) not in ("", None))
            assert "6000" in item().toolTip(1) or "5.9" in item().text(1)

            # compare notes.txt here and there
            shown = {}
            monkeypatch.setattr(ED.DiffDialog, "exec", lambda self: shown.setdefault("text", self.findChildren(
                __import__("PySide6.QtWidgets", fromlist=["QPlainTextEdit"]).QPlainTextEdit)[0].toPlainText()))
            notes = next(e for e in tab.local.entries if e.name == "notes.txt")
            tab.local.compare(notes)
            assert wait(app, lambda: "text" in shown)
            assert "-TWO" in shown["text"] and "+two" in shown["text"]

            # copy to "another server" (the same one, another folder)
            monkeypatch.setattr(TL.RelayDialog, "exec", lambda self: (self.path.setText("/pics"), QDialog.Accepted)[1])
            remote_notes = next(e for e in tab.remote.entries if e.name == "notes.txt")
            tab.remote.copy_to_server([remote_notes])
            assert wait(app, lambda: (srv_root / "pics" / "notes.txt").exists() and not win.engine.pending(), 20)

            # bulk rename in /pics
            tab.remote.open_dir("/pics")
            assert wait(app, lambda: tab.remote.path == "/pics" and len(tab.remote.entries) == 4)
            imgs = [e for e in tab.remote.entries if e.name.startswith("IMG_")]
            rd = TL.RenameDialog(tab.remote, imgs, win)
            rd.template.setText("holiday-{n:02}{ext}")
            assert rd.apply_btn.isEnabled() and "3 will be renamed" in rd.summary.text()
            rd.apply()
            assert wait(app, lambda: sorted(p.name for p in (srv_root / "pics").glob("holiday-*")) ==
                        ["holiday-01.jpg", "holiday-02.jpg", "holiday-03.jpg"])
        finally:
            win.engine.cancel()
            tab.close()
            win.engine.shutdown()


def test_queue_reorder_watch_list_and_diagnostics(app, tmp_path):
    from blamixfiles.ui.main_window import MainWindow
    from blamixfiles.ui.tools import DiagnosticsDialog
    store = Store(Vault.create(tmp_path / "v.bfv", "pw", n_log2=10), {})
    win = MainWindow(store, Settings())
    win.engine.set_paused(True)
    site = Site(name="x")
    jobs = win.engine.add([E.Job("upload", site, f"/f{i}", f"/r{i}") for i in range(3)])
    app.processEvents()
    win.queue._move(jobs[2].id, "top")
    order = [win.queue.tree.topLevelItem(i).data(0, Qt.UserRole) for i in range(3)]
    assert order == [jobs[2].id, jobs[0].id, jobs[1].id] and [j.id for j in win.engine.jobs] == order
    win.engine.cancel()

    d = DiagnosticsDialog({"theme": "Nord"})
    assert "theme = 'Nord'" in d.view.toPlainText()
    assert DiagnosticsDialog({}, "Crash:\nboom").view.toPlainText() == "Crash:\nboom"
    win.engine.shutdown()


def test_site_dialog_cloud_and_smb_fields(app):
    from blamixfiles.ui.dialogs import SiteDialog
    dlg = SiteDialog(None, [], None)
    dlg.show()
    form = dlg.form

    def shown(w) -> bool:
        return form.isRowVisible(w)
    dlg.proto.setCurrentIndex(dlg.proto.findData("gdrive"))
    assert shown(dlg.client_id) and shown(dlg.client_secret) and shown(dlg.signin_widget)
    assert not shown(dlg.hp_widget) and not shown(dlg.auth)
    assert "Not signed in" in dlg.signin_state.text()
    dlg.proto.setCurrentIndex(dlg.proto.findData("dropbox"))
    assert shown(dlg.client_id) and not shown(dlg.client_secret)
    dlg.oauth_token = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
    dlg._save()
    assert dlg.result() and dlg.site.protocol == "dropbox" and dlg.site.auth == "oauth"
    assert dlg.site.name == "Dropbox" and dlg.site.oauth_token.startswith("{")

    dlg = SiteDialog(None, [], None)
    dlg.show()
    dlg.proto.setCurrentIndex(dlg.proto.findData("smb"))
    assert dlg.form.isRowVisible(dlg.hp_widget) and not dlg.form.isRowVisible(dlg.client_id)
    assert dlg.form.labelForField(dlg.remote_dir).text() == "Share / folder"
    assert [dlg.auth.itemData(i) for i in range(dlg.auth.count())] == ["password", "ask"]
    dlg.close()
