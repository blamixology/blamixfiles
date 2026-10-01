"""Main window: site list, quick connect, tabs (site = local | remote panes; editor tabs),
transfer queue."""
from __future__ import annotations

import os
import time
import webbrowser

from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTabWidget,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import __version__
from ..core import engine as E
from ..core.queue_store import QueueStore
from ..core.vfs import Entry
from ..models import Site, Store
from ..paths import data_dir
from ..settings import Settings
from .bridge import ask_on_ui, on_ui
from .dialogs import OverwriteDialog, SiteDialog
from .editor import EditorTab
from .file_pane import FilePane
from .queue_view import QueueView
from .session import Session
from .theme import C, icon, style_window

GITHUB = "https://github.com/blamixology/blamixfiles"
KOFI = "https://ko-fi.com/blamixology"


class SiteTab(QWidget):
    """Local files on the left, the server on the right, a connection log below."""

    def __init__(self, win: "MainWindow", site: Site, start_path: str = ""):
        super().__init__()
        self.win = win
        self.site = site
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setFixedHeight(64)
        self.log_view.setStyleSheet(f"border:none; border-top:1px solid {C['border']}; border-radius:0;"
                                    f"font-size:8.5pt; color:{C['muted']};")

        self.local_session = Session(None, self)
        self.remote_session = Session(site, self, on_site_changed=win.site_changed, log=self.log)
        local_start = site.local_dir or win.settings["last_local_dir"] or os.path.expanduser("~")
        self.local = FilePane(self.local_session, "This computer", local_start)
        self.remote = FilePane(self.remote_session, site.label, start_path or site.remote_dir)
        if site.production:
            self.remote.title.setText(f"{site.label}  ·  PRODUCTION")
            self.remote.title.setObjectName("Prod")
        self.local.other, self.remote.other = self.remote, self.local

        mid = QWidget()
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(4, 0, 4, 0)
        ml.addStretch(1)
        up = QToolButton()
        up.setIcon(icon("arrow-right", C["accent"]))
        up.setToolTip("Upload selected")
        up.clicked.connect(self.local.send_selected)
        down = QToolButton()
        down.setIcon(icon("arrow-left", C["accent"]))
        down.setToolTip("Download selected")
        down.clicked.connect(self.remote.send_selected)
        sync = QToolButton()
        sync.setIcon(icon("sync", C["accent"]))
        sync.setToolTip("Compare && sync these two folders (Ctrl+Shift+S)")
        sync.clicked.connect(lambda: win.open_sync(self))
        ml.addWidget(up)
        ml.addWidget(down)
        ml.addSpacing(14)
        ml.addWidget(sync)
        ml.addStretch(1)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.local)
        split.addWidget(mid)
        split.addWidget(self.remote)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(2, 1)
        split.setCollapsible(1, False)
        lay.addWidget(split, 1)
        lay.addWidget(self.log_view)

        for pane in (self.local, self.remote):
            pane.transfer.connect(self.transfer)
            pane.upload_paths.connect(lambda paths, target: self.upload(paths, target))
            pane.edit.connect(lambda e, p: win.open_editor(p.session, e))
            pane.message.connect(win.show_message)
        self.local.start()
        self.remote.start()

    def log(self, msg: str, error: bool = False) -> None:
        stamp = time.strftime("%H:%M:%S")
        on_ui(lambda: self.log_view.appendPlainText(f"{stamp}  {'✖ ' if error else ''}{msg}"))

    def transfer(self, entries: list[Entry], src: FilePane, target: str) -> None:
        if src is self.local:
            self.upload([e.path for e in entries], target or self.remote.path)
        else:
            dest = target or self.local.path
            for e in entries:
                self.win.engine.download(self.remote_session.site, e, dest)

    def upload(self, paths: list[str], target: str) -> None:
        if not target:
            return
        join = (self.remote_session.backend.join if self.remote_session.backend
                else (lambda a, b: a.rstrip("/") + "/" + b))
        for p in paths:
            self.win.engine.upload(self.remote_session.site, p, target, remote_join=join)

    def close(self) -> None:
        self.win.settings["last_local_dir"] = self.local.path
        self.local_session.close()
        self.remote_session.close()


class MainWindow(QMainWindow):
    def __init__(self, store: Store, settings: Settings):
        super().__init__()
        self.store = store
        self.settings = settings
        self.setWindowTitle("BlamixFiles")
        self.resize(1320, 820)
        self.queue_store = QueueStore(data_dir() / "queue.db", keep_site=lambda sid: sid in self.store.sites)
        self.engine = E.TransferEngine(self._connector, workers=int(settings["workers"]),
                                       on_change=lambda j: on_ui(lambda: self._job_changed(j)),
                                       policy=settings["policy"], ask=self._ask_overwrite,
                                       preserve_mtime=bool(settings["preserve_mtime"]),
                                       store=self.queue_store,
                                       limit_up=int(settings["limit_up_kb"]) * 1024,
                                       limit_down=int(settings["limit_down_kb"]) * 1024)
        self._refresh_timer = QTimer(singleShot=True, interval=400)
        self._refresh_timer.timeout.connect(self._refresh_targets)
        self._refresh_dirs: set[tuple[int, str]] = set()

        root = QWidget()
        rl = QHBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self._build_sidebar())

        right = QWidget()
        vl = QVBoxLayout(right)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        qc = QWidget(objectName="Toolbar")
        ql = QHBoxLayout(qc)
        ql.setContentsMargins(10, 8, 10, 8)
        self.quick = QLineEdit(placeholderText="Quick connect:  sftp://user@host/path   ftp://…   ftps://…   "
                                               "(Ctrl+L)")
        self.quick.returnPressed.connect(self.quick_connect)
        ql.addWidget(self.quick, 1)
        go = QPushButton("Connect", objectName="Primary")
        go.clicked.connect(self.quick_connect)
        ql.addWidget(go)
        vl.addWidget(qc)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.welcome = self._build_welcome()
        self.tabs.addTab(self.welcome, "Welcome")
        self.queue = QueueView(self.engine, settings)
        self.queue.summary.connect(self._queue_summary)
        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(self.tabs)
        vsplit.addWidget(self.queue)
        vsplit.setStretchFactor(0, 4)
        vsplit.setStretchFactor(1, 1)
        vsplit.setSizes([600, 200])
        self.vsplit = vsplit
        vl.addWidget(vsplit, 1)
        rl.addWidget(right, 1)
        self.setCentralWidget(root)

        self.status_label = QLabel("")
        self.statusBar().addWidget(self.status_label, 1)
        self.queue_label = QLabel("")
        self.statusBar().addPermanentWidget(self.queue_label)
        self._build_menu()
        self.reload_sites()
        restored = self.queue_store.load(self.store.sites)
        if restored:
            self.engine.restore(restored, paused=True)
            self.queue.offer_resume(len(restored))
        geo = settings["window_geometry"]
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))

    # ------------------------------------------------------------ layout
    def _build_sidebar(self) -> QWidget:
        side = QWidget(objectName="Sidebar")
        side.setFixedWidth(250)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(12, 14, 12, 12)
        sl.setSpacing(8)
        brand = QLabel("BlamixFiles", objectName="Brand")
        sl.addWidget(brand)
        sl.addWidget(QLabel(f"v{__version__} · SFTP · FTP · S3 · WebDAV", objectName="BrandSub"))
        self.search = QLineEdit(placeholderText="Search sites", objectName="Search")
        self.search.textChanged.connect(self.reload_sites)
        sl.addWidget(self.search)
        row = QHBoxLayout()
        row.addWidget(QLabel("SITES", objectName="SectionLabel"))
        row.addStretch(1)
        add = QToolButton()
        add.setIcon(icon("plus"))
        add.setToolTip("New site (Ctrl+N)")
        add.clicked.connect(self.new_site)
        row.addWidget(add)
        sl.addLayout(row)
        self.site_tree = QTreeWidget()
        self.site_tree.setHeaderHidden(True)
        self.site_tree.setIndentation(12)
        self.site_tree.itemActivated.connect(self._site_activated)
        self.site_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.site_tree.customContextMenuRequested.connect(self._site_menu)
        sl.addWidget(self.site_tree, 1)
        coffee = QPushButton(icon("coffee", C["muted"]), "  Buy me a coffee", objectName="Ghost")
        coffee.setToolTip("BlamixFiles is free. If it saves you time, a coffee keeps it going.")
        coffee.clicked.connect(lambda: webbrowser.open(KOFI))
        sl.addWidget(coffee)
        return side

    def _build_welcome(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(60, 50, 60, 50)
        lay.addWidget(QLabel("Welcome to BlamixFiles", objectName="H1"))
        sub = QLabel("Double-click a site on the left, add one with <b>+</b>, or type an address in "
                     "Quick connect.<br><br>"
                     "• Drag files between the two panes (or from Explorer/Finder) to transfer them.<br>"
                     "• Double-click a text file to edit it right here. <b>Ctrl+S</b> saves it back to the "
                     "server.<br>"
                     "• Moving from FileZilla? <b>File → Import from FileZilla</b>.")
        sub.setWordWrap(True)
        sub.setObjectName("Muted")
        lay.addWidget(sub)
        row = QHBoxLayout()
        b1 = QPushButton(icon("plus", "#0b0d12"), " New site", objectName="Primary")
        b1.clicked.connect(self.new_site)
        b2 = QPushButton(icon("import"), " Import from FileZilla")
        b2.clicked.connect(self.import_filezilla)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def _build_menu(self) -> None:
        mb = self.menuBar()
        f = mb.addMenu("&File")

        def act(menu, text, slot, shortcut=None, ic=None):
            a = QAction(icon(ic) if ic else None, text, self) if ic else QAction(text, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(slot)
            menu.addAction(a)
            return a
        act(f, "New site…", self.new_site, "Ctrl+N", "plus")
        act(f, "Quick connect", lambda: (self.quick.setFocus(), self.quick.selectAll()), "Ctrl+L", "bolt")
        f.addSeparator()
        act(f, "Import from FileZilla…", self.import_filezilla, None, "import")
        f.addSeparator()
        act(f, "Close tab", lambda: self.close_tab(self.tabs.currentIndex()), "Ctrl+W")
        act(f, "Quit", self.close, "Ctrl+Q")
        self.sync_menu = mb.addMenu("&Sync")
        self.reload_profiles()
        v = mb.addMenu("&View")
        act(v, "Show/hide transfer queue", lambda: self.queue.setVisible(not self.queue.isVisible()), "Ctrl+J")
        h = mb.addMenu("&Help")
        act(h, "BlamixFiles on GitHub", lambda: webbrowser.open(GITHUB), None, "github")
        act(h, "Report a problem", lambda: webbrowser.open(GITHUB + "/issues"), None, "help")
        act(h, "☕ Buy me a coffee", lambda: webbrowser.open(KOFI), None, "coffee")
        h.addSeparator()
        act(h, "About BlamixFiles", self.about)

    # ------------------------------------------------------------ sync
    def reload_profiles(self) -> None:
        m = self.sync_menu
        m.clear()
        a = m.addAction(icon("sync"), "Compare && sync current tab…", lambda: self.open_sync())
        a.setShortcut(QKeySequence("Ctrl+Shift+S"))
        profiles = self.store.sync_profiles
        if profiles:
            m.addSeparator()
            for p in profiles:
                site = self.store.sites.get(p["site_id"])
                label = f"{p['name']}   ({site.label if site else 'site deleted'})"
                act = m.addAction(icon("star"), label, lambda p=p: self.run_profile(p))
                act.setEnabled(site is not None)
            m.addSeparator()
            dm = m.addMenu(icon("trash"), "Delete profile")
            for p in profiles:
                dm.addAction(p["name"], lambda p=p: self._delete_profile(p["name"]))

    def _delete_profile(self, name: str) -> None:
        if QMessageBox.question(self, "Delete profile", f"Delete the sync profile “{name}”?") == QMessageBox.Yes:
            self.store.delete_profile(name)
            self.reload_profiles()

    def open_sync(self, tab=None, profile=None, auto: bool = False) -> None:
        from .sync_dialog import SyncDialog
        tab = tab or self.tabs.currentWidget()
        if not isinstance(tab, SiteTab):
            self.show_message("Open a site first: sync compares the two folders of a site tab.", True)
            return
        SyncDialog(self, tab, profile, auto_compare=auto).exec()

    def run_profile(self, p: dict) -> None:
        from ..core.sync import SyncProfile
        prof = SyncProfile.from_dict(p)
        site = self.store.sites.get(prof.site_id)
        if site is None:
            return
        tab = next((t for t in self.site_tabs() if t.remote_session.site.id == site.id), None)
        if tab is None:
            self.open_site(site, prof.remote_dir)
            tab = self.site_tabs()[-1]
        self.tabs.setCurrentWidget(tab)
        self.open_sync(tab, prof, auto=True)

    # ------------------------------------------------------------ sites
    def reload_sites(self, *_a) -> None:
        q = self.search.text() if hasattr(self, "search") else ""
        self.site_tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}

        def group_item(path: str) -> QTreeWidgetItem | None:
            if not path:
                return None
            if path in groups:
                return groups[path]
            parent_path, _, name = path.rpartition("/")
            parent = group_item(parent_path)
            it = QTreeWidgetItem([name])
            it.setIcon(0, icon("folder", C["faint"], 16))
            it.setFlags(it.flags() & ~Qt.ItemIsSelectable)
            (parent.addChild(it) if parent else self.site_tree.addTopLevelItem(it))
            it.setExpanded(True)
            groups[path] = it
            return it
        for s in sorted(self.store.sites.values(), key=lambda s: (s.group.lower(), s.label.lower())):
            if not s.matches(q):
                continue
            it = QTreeWidgetItem([s.label])
            it.setData(0, Qt.UserRole, s.id)
            it.setIcon(0, icon("server", s.color or (C["danger"] if s.production else C["muted"]), 16))
            it.setToolTip(0, s.address + (f"\n{s.notes}" if s.notes else ""))
            parent = group_item(s.group)
            (parent.addChild(it) if parent else self.site_tree.addTopLevelItem(it))

    def _site_activated(self, item, _col=0) -> None:
        sid = item.data(0, Qt.UserRole)
        if sid and sid in self.store.sites:
            self.open_site(self.store.sites[sid])

    def _site_menu(self, pos) -> None:
        it = self.site_tree.itemAt(pos)
        sid = it.data(0, Qt.UserRole) if it else None
        m = QMenu(self)
        if sid:
            s = self.store.sites[sid]
            m.addAction(icon("plug"), "Connect", lambda: self.open_site(s))
            m.addAction(icon("edit"), "Edit…", lambda: self.edit_site(s))
            m.addAction(icon("copy"), "Duplicate", lambda: self._duplicate(s))
            m.addSeparator()
            m.addAction(icon("trash", C["danger"]), "Delete", lambda: self.delete_site(s))
        else:
            m.addAction(icon("plus"), "New site…", self.new_site)
        m.exec(self.site_tree.viewport().mapToGlobal(pos))

    def new_site(self) -> None:
        dlg = SiteDialog(None, self.store.all_groups(), self)
        if dlg.exec():
            self.store.upsert(dlg.site)
            self.reload_sites()

    def edit_site(self, s: Site) -> None:
        dlg = SiteDialog(s, self.store.all_groups(), self)
        if dlg.exec():
            self.store.upsert(dlg.site)
            self.reload_sites()

    def _duplicate(self, s: Site) -> None:
        c = s.copy()
        from ..models import _id
        c.id = _id()
        c.name = (s.name or s.host) + " (copy)"
        self.store.upsert(c)
        self.reload_sites()

    def delete_site(self, s: Site) -> None:
        if QMessageBox.question(self, "Delete site", f"Delete “{s.label}” from your vault?") == QMessageBox.Yes:
            self.store.delete(s.id)
            self.reload_sites()
            self.reload_profiles()

    def site_changed(self, site: Site) -> None:
        """A session learned something worth keeping (a pinned certificate)."""
        def apply():
            s = self.store.sites.get(site.id)
            if s is not None and s.tls_pinned != site.tls_pinned:
                s.tls_pinned = site.tls_pinned
                self.store.save()
        on_ui(apply)

    def import_filezilla(self) -> None:
        from ..importers import filezilla_default_path, import_filezilla
        default = filezilla_default_path()
        path = str(default) if default.exists() else ""
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "FileZilla sitemanager.xml", str(default.parent),
                                                  "FileZilla sites (sitemanager.xml *.xml)")
        if not path:
            return
        try:
            sites = import_filezilla(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Import", f"Could not read {path}:\n{e}")
            return
        added = self.store.import_sites(sites)
        self.reload_sites()
        with_pw = sum(1 for s in sites if s.password)
        QMessageBox.information(
            self, "Imported from FileZilla",
            f"Imported {added} new site(s) from\n{path}\n\n"
            + (f"{with_pw} saved password(s) are now encrypted in your BlamixFiles vault. "
               "FileZilla keeps its own copy in that file (unencrypted unless you set a master "
               "password there), so consider removing it." if with_pw else ""))

    # ------------------------------------------------------------ tabs
    def open_site(self, site: Site, path: str = "") -> None:
        if site.id in self.store.sites:
            self.store.touch(site.id)
        tab = SiteTab(self, site, path)
        i = self.tabs.addTab(tab, icon("server", site.color or (C["danger"] if site.production else C["muted"]), 16),
                             site.label)
        self.tabs.setTabToolTip(i, site.address)
        self.tabs.setCurrentIndex(i)
        if self.tabs.indexOf(self.welcome) != -1:
            self.tabs.removeTab(self.tabs.indexOf(self.welcome))

    def quick_connect(self) -> None:
        text = self.quick.text().strip()
        if not text:
            return
        try:
            site, path = Site.from_url(text)
        except ValueError as e:
            self.show_message(str(e), True)
            return
        site.name = site.host
        self.open_site(site, path)

    def open_editor(self, session: Session, entry: Entry) -> None:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, EditorTab) and w.session is session and w.path == entry.path:
                self.tabs.setCurrentIndex(i)
                return
        ed = EditorTab(session, entry)
        i = self.tabs.addTab(ed, icon("code", C["accent"], 16), entry.name)
        self.tabs.setTabToolTip(i, f"{session.label}: {entry.path}")
        self.tabs.setCurrentIndex(i)
        ed.title_changed.connect(lambda t, w=ed: self._retitle(w, t))
        ed.message.connect(self.show_message)
        ed.saved.connect(lambda _p, s=session: self._refresh_panes_of(s))

    def _retitle(self, w: QWidget, title: str) -> None:
        i = self.tabs.indexOf(w)
        if i != -1:
            self.tabs.setTabText(i, title)

    def close_tab(self, index: int) -> None:
        w = self.tabs.widget(index)
        if w is None:
            return
        if isinstance(w, EditorTab) and w.dirty:
            r = QMessageBox.question(self, "Unsaved changes", f"Save changes to {w.name}?",
                                     QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Save:
                w.save()
                return
        if isinstance(w, SiteTab):
            editors = [self.tabs.widget(i) for i in range(self.tabs.count())
                       if isinstance(self.tabs.widget(i), EditorTab)
                       and self.tabs.widget(i).session is w.remote_session]
            if any(e.dirty for e in editors):
                QMessageBox.information(self, "Unsaved changes",
                                        "Save or close the files you're editing on this site first.")
                return
            busy = [j for j in self.engine.jobs
                    if j.site.id == w.remote_session.site.id and j.status in (E.QUEUED, E.RUNNING)]
            if busy and QMessageBox.question(
                    self, "Transfers running",
                    f"{len(busy)} transfer(s) for {w.site.label} haven't finished. "
                    "Close the tab and cancel them?") != QMessageBox.Yes:
                return
            for j in busy:
                self.engine.cancel(j.id)
            for e in editors:
                self.tabs.removeTab(self.tabs.indexOf(e))
            w.close()
        self.tabs.removeTab(index)
        if self.tabs.count() == 0:
            self.tabs.addTab(self.welcome, "Welcome")

    def site_tabs(self) -> list[SiteTab]:
        return [self.tabs.widget(i) for i in range(self.tabs.count()) if isinstance(self.tabs.widget(i), SiteTab)]

    # ------------------------------------------------------------ transfers
    def _connector(self, site: Site):
        """Called on transfer threads. Reuse the site's tab (its login, 2FA, pinned
        certificate); if no tab is open (e.g. a transfer restored from last time),
        open one, so every question (host key, password, code) can be asked."""
        def find_or_open():
            for t in self.site_tabs():
                if t.remote_session.site.id == site.id:
                    return t
            known = self.store.sites.get(site.id, site)
            self.open_site(known)
            return self.site_tabs()[-1]
        tab = ask_on_ui(find_or_open)
        tab.remote_session.ensure_connected()
        return tab.remote_session.transfer_connector(site)

    def _ask_overwrite(self, job: E.Job, existing) -> str:
        def ask():
            if job.kind == "upload":
                st = os.stat(job.src)
                src_size, src_m = st.st_size, st.st_mtime
            else:
                src_size, src_m = job.size, job.mtime
            dlg = OverwriteDialog(os.path.basename(job.dst.rstrip("/\\")), src_size, src_m,
                                  existing.size, existing.mtime, self)
            return dlg.result_policy if dlg.exec() else "cancel"
        return ask_on_ui(ask)

    def _job_changed(self, job: E.Job) -> None:
        self.queue.update_job(job)
        if job.status != E.DONE:
            return
        # refresh the destination pane if it shows the folder the file/folder landed in
        for t in self.site_tabs():
            if job.kind == "upload" and t.remote_session.site.id == job.site.id and t.remote_session.backend:
                self._refresh_dirs.add((id(t.remote), t.remote_session.backend.parent(job.dst)))
            elif job.kind == "download":
                self._refresh_dirs.add((id(t.local), os.path.dirname(job.dst)))
        self._refresh_timer.start()

    def _refresh_targets(self) -> None:
        dirs, self._refresh_dirs = self._refresh_dirs, set()
        for t in self.site_tabs():
            for pane in (t.local, t.remote):
                if (id(pane), pane.path) in dirs:
                    pane.refresh()

    def _refresh_panes_of(self, session: Session) -> None:
        for t in self.site_tabs():
            for pane in (t.local, t.remote):
                if pane.session is session:
                    pane.refresh()

    def _queue_summary(self, text: str) -> None:
        self.queue_label.setText(text)

    # ------------------------------------------------------------ misc
    def show_message(self, msg: str, error: bool = False) -> None:
        self.status_label.setText(("✖  " if error else "") + msg)
        self.status_label.setStyleSheet(f"color:{C['danger'] if error else C['muted']};")

    def about(self) -> None:
        QMessageBox.about(
            self, "About BlamixFiles",
            f"<h3>BlamixFiles {__version__}</h3>"
            "<p>A free, open-source file transfer client: SFTP, FTP and FTPS, a built-in editor "
            "and an encrypted password vault.</p>"
            f"<p><a href='{GITHUB}'>{GITHUB}</a><br>MIT license. No ads, no bundled installers, no telemetry.</p>"
            f"<p>If it saves you time: <a href='{KOFI}'>☕ buy me a coffee</a></p>")

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        style_window(self)

    def closeEvent(self, e):  # noqa: N802
        dirty = [self.tabs.widget(i) for i in range(self.tabs.count())
                 if isinstance(self.tabs.widget(i), EditorTab) and self.tabs.widget(i).dirty]
        busy = self.engine.pending()
        if dirty or busy:
            parts = []
            if dirty:
                parts.append(f"{len(dirty)} unsaved file(s)")
            if busy:
                parts.append(f"{busy} transfer(s) not finished (saved sites resume next time)")
            if QMessageBox.question(self, "Quit BlamixFiles?", " and ".join(parts).capitalize()
                                    + ". Quit anyway?") != QMessageBox.Yes:
                e.ignore()
                return
        self.settings["window_geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.settings["policy"] = self.engine.policy
        # unfinished transfers stay in the saved queue: offered again on the next start
        self.engine.store = None
        for t in self.site_tabs():
            t.close()
        self.settings.save()
        self.engine.shutdown()
        super().closeEvent(e)
