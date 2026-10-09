"""Make the README screenshots: ./dev.sh screenshots  (or python packaging/screenshots.py [out-dir]).

Runs the real app offscreen against a throwaway vault and a local test FTP server with demo files, so the
pictures are reproducible and show no real servers or paths. Writes PNGs (and the terminal UI as SVG)
to docs/screenshots/.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1.25")
if sys.platform == "win32":                     # the offscreen platform doesn't find Windows' fonts by itself
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"))
HOME = Path(tempfile.mkdtemp(prefix="blamixfiles-shots-"))
os.environ["BLAMIXFILES_HOME"] = str(HOME / "data")

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "screenshots"

NGINX = """server {
    listen 443 ssl http2;
    server_name shop.example.com;

    root /var/www/shop/public;
    index index.php index.html;

    ssl_certificate     /etc/letsencrypt/live/shop.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/shop.example.com/privkey.pem;

    # static files: cache for a month
    location ~* \\.(css|js|png|jpg|webp|svg|woff2)$ {
        expires 30d;
        add_header Cache-Control "public";
    }

    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }

    location ~ \\.php$ {
        include fastcgi_params;
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
        fastcgi_param SCRIPT_FILENAME $realpath_root$fastcgi_script_name;
    }
}
"""


def demo_files(local: Path, remote: Path) -> None:
    """A small website project here, and the (slightly older) deployed copy on the 'server'."""
    old = time.time() - 9 * 86400
    for base in (local, remote):
        for d in ("public/css", "public/js", "public/img", "config", "src/Controller", "logs"):
            (base / "shop" / d).mkdir(parents=True, exist_ok=True)
    files = {
        "public/index.php": "<?php\nrequire __DIR__ . '/../src/bootstrap.php';\n",
        "public/css/site.css": "body { font-family: system-ui; }\n" * 40,
        "public/js/app.js": "document.addEventListener('DOMContentLoaded', () => init());\n" * 30,
        "config/app.php": "<?php return ['debug' => false];\n",
        "src/Controller/CartController.php": "<?php\nfinal class CartController {}\n" * 20,
        "src/bootstrap.php": "<?php\n// boot\n",
        "README.md": "# Shop\n",
    }
    for rel, text in files.items():
        (local / "shop" / rel).write_text(text)
        (remote / "shop" / rel).write_text(text)
    # local changes the sync will find
    (local / "shop" / "public/css/site.css").write_text("body { font-family: Inter, system-ui; }\n" * 42)
    (local / "shop" / "public/js/checkout.js").write_text("export function pay() {}\n" * 12)
    (local / "shop" / "config/app.php").write_text("<?php return ['debug' => false, 'cache' => true];\n")
    for i in range(1, 9):
        (remote / "shop" / "public/img" / f"IMG_{4810 + i}.jpg").write_bytes(os.urandom(40_000 * i))
        (local / "shop" / "public/img" / f"IMG_{4810 + i}.jpg").write_bytes(b"x")
    for name, size in (("access.log", 4_800_000), ("error.log", 380_000), ("access.log.1", 9_600_000)):
        (remote / "shop" / "logs" / name).write_bytes(b"x" * size)
    (remote / "etc" / "nginx" / "sites-enabled").mkdir(parents=True)
    (remote / "etc" / "nginx" / "sites-enabled" / "shop.conf").write_text(NGINX)
    for p in remote.rglob("*"):
        os.utime(p, (old, old))


def main() -> None:
    import faulthandler
    faulthandler.dump_traceback_later(170, exit=True)       # never hang a build
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QPainter
    from PySide6.QtWidgets import QApplication

    from servers import PASSWORD, USER, FTPTestServer

    from blamixfiles.core import engine as E
    from blamixfiles.models import Site, Store
    from blamixfiles.settings import Settings
    from blamixfiles.ui import theme
    from blamixfiles.vault import Vault

    OUT.mkdir(parents=True, exist_ok=True)
    local, remote = HOME / "local", HOME / "srv"
    local.mkdir()
    remote.mkdir()
    demo_files(local, remote)

    app = QApplication([])
    theme.set_theme("Midnight", app)
    from blamixfiles.ui.bridge import init_bridge
    from blamixfiles.ui.main_window import MainWindow
    init_bridge()
    theme.apply_palette(app)

    def pump(seconds: float = 0.3) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    def until(cond, timeout: float = 20) -> None:
        end = time.time() + timeout
        while not cond():
            if time.time() > end:
                raise RuntimeError("timed out")
            pump(0.05)

    masks: list[tuple[str, str]] = []                 # (real text, what the picture shows)

    def mask(widget) -> None:
        """No temp folders, user names or test ports in the pictures."""
        from PySide6.QtWidgets import QLabel, QLineEdit, QPlainTextEdit
        for kind in (QLineEdit, QLabel):
            for w in widget.findChildren(kind):
                text = w.text()
                new = text
                for real, shown in masks:
                    new = new.replace(real, shown)
                if new != text:
                    w.setText(new)
                    if isinstance(w, QLineEdit):
                        w.setCursorPosition(0)
        for w in widget.findChildren(QPlainTextEdit):
            text = w.toPlainText()
            new = text
            for real, shown in masks:
                new = new.replace(real, shown)
            if new != text:
                w.setPlainText(new)

    def save(widget, name: str) -> None:
        pump(0.4)
        mask(widget)
        pump(0.1)
        widget.grab().save(str(OUT / f"{name}.png"))
        print("wrote", OUT / f"{name}.png")

    def save_over(win, dialog, name: str) -> None:
        """The main window with a dialog drawn where it would sit (offscreen there's no compositor)."""
        pump(0.4)
        mask(win)
        mask(dialog)
        pump(0.1)
        base = win.grab()
        top = dialog.grab()
        x = (base.width() - top.width()) // 2
        y = max(40, (base.height() - top.height()) // 3)
        p = QPainter(base)
        p.fillRect(base.rect(), Qt.GlobalColor.transparent)
        p.setOpacity(0.55)
        p.fillRect(base.rect(), Qt.GlobalColor.black)
        p.setOpacity(1.0)
        p.drawPixmap(QPoint(x, y), top)
        p.end()
        base.save(str(OUT / f"{name}.png"))
        print("wrote", OUT / f"{name}.png")

    with FTPTestServer(remote) as srv:
        store = Store(Vault.create(HOME / "v.bfv", "demo", n_log2=10), {})
        demo = [
            Site(name="shop.example.com", protocol="ftp", host="127.0.0.1", port=srv.port, username=USER,
                 password=PASSWORD, group="Clients/Shop", color="#4dabf7", local_dir=str(local / "shop"),
                 remote_dir="/shop"),
            Site(name="shop (production)", protocol="sftp", host="shop.example.com", username="deploy",
                 group="Clients/Shop", production=True),
            Site(name="backups", protocol="s3", host="s3.eu-central-003.backblazeb2.com", group="Storage"),
            Site(name="Team Drive", protocol="gdrive", group="Storage"),
            Site(name="NAS", protocol="smb", host="nas.local", remote_dir="/Public", group="Office"),
            Site(name="nextcloud", protocol="webdavs", host="cloud.example.com", group="Office"),
            Site(name="blog", protocol="sftp", host="blog.example.org", username="www", color="#69db7c"),
        ]
        for s in demo:
            store.upsert(s)
        store.save_profile({"name": "Deploy shop", "site_id": demo[0].id, "local_dir": str(local / "shop"),
                            "remote_dir": "/shop", "options": {}})
        masks += [(str(local), r"C:\Projects"), (f"127.0.0.1:{srv.port}", "ftp.shop.example.com"),
                  ("tester@", "deploy@")]
        settings = Settings()
        win = MainWindow(store, settings)
        win.resize(1440, 880)
        win.show()
        win.site_tree.expandAll()
        win.open_site(store.sites[demo[0].id])
        tab = win.site_tabs()[0]
        tab.local.set_tree_visible(False)               # (its tree would show the temp folder's parents)
        until(lambda: tab.remote.path == "/shop" and tab.remote.entries)
        until(lambda: tab.local.entries)
        # a queue with some history
        win.engine.set_paused(True)
        site = store.sites[demo[0].id]
        jobs = [E.Job("upload", site, str(local / "shop/public/js/checkout.js"), "/shop/public/js/checkout.js", size=300),
                E.Job("upload", site, str(local / "shop/public/css/site.css"), "/shop/public/css/site.css", size=1700),
                E.Job("download", site, "/shop/logs/access.log", str(local / "access.log"), size=4_800_000),
                E.Job("download", site, "/shop/logs/access.log.1", str(local / "access.log.1"), size=9_600_000)]
        jobs[0].status, jobs[0].done = E.DONE, 300
        jobs[1].status, jobs[1].done = E.DONE, 1700
        jobs[2].status, jobs[2].done, jobs[2].started = E.RUNNING, 3_100_000, time.time() - 2
        win.engine.add(jobs)
        tab.remote.tree.setCurrentItem(tab.remote.tree.topLevelItem(1))
        save(win, "main")

        # editor
        from blamixfiles.core.vfs import Entry
        win.open_editor(tab.remote_session, Entry(name="shop.conf", path="/etc/nginx/sites-enabled/shop.conf",
                                                  size=len(NGINX)))
        until(lambda: win.tabs.count() >= 2 and "nginx" in win.tabs.tabText(win.tabs.currentIndex()) + "nginx"
              and getattr(win.tabs.currentWidget(), "ed", None) is not None
              and "server_name" in win.tabs.currentWidget().ed.toPlainText())
        save(win, "editor")
        win.tabs.setCurrentWidget(tab)

        # compare & sync
        from blamixfiles.ui.sync_dialog import SyncDialog
        dlg = SyncDialog(win, tab, None, auto_compare=True)
        dlg.resize(1180, 640)
        dlg.show()
        until(lambda: dlg.plan is not None)
        save(dlg, "sync")
        dlg.close()

        # search
        from blamixfiles.ui.tools import RenameDialog, SearchDialog
        sd = SearchDialog(tab.remote, win)
        sd.resize(960, 520)
        sd.name.setText("*.log")
        sd.larger.setText("100k")
        sd.show()
        sd.start()
        until(lambda: not sd._running)
        save(sd, "search")
        sd.close()

        # bulk rename
        tab.remote.open_dir("/shop/public/img")
        until(lambda: tab.remote.path == "/shop/public/img" and len(tab.remote.entries) == 8)
        rd = RenameDialog(tab.remote, sorted(tab.remote.entries, key=lambda e: e.name), win)
        rd.resize(900, 560)
        rd.template.setText("holiday-{n:02}{ext}")
        rd.case.setCurrentIndex(rd.case.findData("lower"))
        rd.show()
        save(rd, "rename")
        rd.close()
        tab.remote.open_dir("/shop")
        until(lambda: tab.remote.path == "/shop")

        # command palette over the window
        from blamixfiles.ui.palette import CommandPalette
        entries = [(s.label, s.address.replace(f"127.0.0.1:{srv.port}", "ftp.shop.example.com"), None, "server")
                   for s in store.sites.values()][:5]
        entries += [("Deploy shop", "sync profile", None, "sync"), ("Compare & sync current tab", "action", None, "sync"),
                    ("Search here", "action", None, "search")]
        pal = CommandPalette(entries, win, anchor=win.quick)
        pal.show()
        pal.q.setText("sh")
        save_over(win, pal, "palette")
        pal.close()

        # light theme
        win.set_theme("Light")
        save(win, "light")
        win.set_theme("Nord")
        save(win, "nord")
        win.set_theme("Midnight")
        win.force_quit = True              # (no "transfers not finished" question)
        win.engine.cancel()
        tab.close()
        win.engine.shutdown()
        win.close()

    # terminal UI (SVG, straight from Textual)
    with FTPTestServer(remote) as srv:
        from blamixfiles import tui as T
        from blamixfiles.models import open_or_create
        from blamixfiles.paths import vault_path
        vault_path().unlink(missing_ok=True)
        st = open_or_create(vault_path(), "demo", create=True)
        s = Site(name="shop.example.com", protocol="ftp", host="127.0.0.1", port=srv.port, username=USER,
                 password=PASSWORD, group="Clients/Shop", remote_dir="/shop")
        st.upsert(s)
        st.upsert(Site(name="blog", protocol="sftp", host="blog.example.org", username="www"))

        async def tui() -> None:
            a = T.BlamixFilesTUI(st)
            async with a.run_test(size=(140, 36)) as pilot:
                while not isinstance(a.screen, T.SitesScreen):
                    await pilot.pause(0.05)
                a.open_site(s.copy(), saved=s)
                for _ in range(200):
                    if isinstance(a.screen, T.BrowserScreen) and a.screen.remote.entries:
                        break
                    await pilot.pause(0.05)
                if not isinstance(a.screen, T.BrowserScreen):
                    raise RuntimeError("the terminal UI didn't connect")
                a.screen.local.load(str(local / "shop"))
                await pilot.pause(1.0)
                a.screen.local.query_one(".path").update(r"This computer  C:\Projects\shop")   # (no temp path)
                await pilot.pause(0.2)
                a.save_screenshot(str(OUT / "tui.svg"))
                print("wrote", OUT / "tui.svg")
        asyncio.run(tui())
    os._exit(0)                      # (skip Qt's teardown; the pictures are written)


if __name__ == "__main__":
    main()
