"""End-to-end through the real window (offscreen): connect, browse, upload, edit + save."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

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
