"""Main window: site list, quick connect, tabs (site = local | remote panes; editor tabs),
transfer queue."""
from __future__ import annotations

import os
import threading
import time
import webbrowser

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTabBar, QTabWidget,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import __version__, keychain, updater
from ..core import engine as E
from ..core.queue_store import QueueStore
from ..core.vfs import Entry
from ..models import Site, Store
from ..paths import data_dir, transfer_log_path
from ..settings import Settings
from ..vault import Vault, WrongPassword
from .bridge import ask_on_ui, on_ui
from .dialogs import OverwriteDialog, SiteDialog
from .editor import EditorTab
from .file_pane import FilePane
from .queue_view import QueueView
from .rowtree import RowTree
from .session import Session
from . import a11y, platform_ui, theme
from .theme import C, icon, style_window

from ..links import COMPANY, COMPANY_URL, KOFI_URL as KOFI, REPO_URL as GITHUB


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
        self.watch_btn = QToolButton(checkable=True)
        self.watch_btn.setIcon(icon("eye", C["accent"]))
        self.watch_btn.setToolTip("Watch the local folder and upload every file that changes")
        self.watch_btn.toggled.connect(self._toggle_watch)
        ml.addWidget(up)
        ml.addWidget(down)
        ml.addSpacing(14)
        ml.addWidget(sync)
        ml.addWidget(self.watch_btn)
        ml.addStretch(1)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.local)
        split.addWidget(mid)
        split.addWidget(self.remote)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(2, 1)
        split.setCollapsible(1, False)
        # "watching C:\site -> /var/www" bar (shown while a folder watch runs)
        self.watcher = None
        self.watch_bar = QWidget(objectName="Banner")
        wb = QHBoxLayout(self.watch_bar)
        wb.setContentsMargins(12, 5, 8, 5)
        dot = QLabel()
        dot.setPixmap(icon("eye", C["ok"], 16).pixmap(16, 16))
        wb.addWidget(dot)
        self.watch_text = QLabel("")
        wb.addWidget(self.watch_text, 1)
        self.watch_list_btn = QPushButton("Show files", checkable=True)
        self.watch_list_btn.toggled.connect(lambda on: (self.watch_list.setVisible(on),
                                                        self.watch_list_btn.setText("Hide files" if on else "Show files")))
        wb.addWidget(self.watch_list_btn)
        stop = QPushButton("Stop watching")
        stop.clicked.connect(lambda: self.watch_btn.setChecked(False))
        wb.addWidget(stop)
        self.watch_bar.hide()
        lay.addWidget(self.watch_bar)
        # what the watch uploaded, newest first (live)
        self.watch_list = QTreeWidget()
        self.watch_list.setObjectName("Files")
        self.watch_list.setRootIsDecorated(False)
        self.watch_list.setHeaderLabels(["Time", "File", "Status"])
        self.watch_list.setColumnWidth(0, 80)
        self.watch_list.setColumnWidth(1, 520)
        self.watch_list.setMaximumHeight(160)
        self.watch_list.setAccessibleName("Files uploaded by the folder watch")
        self.watch_list.hide()
        self.watch_items: dict[int, QTreeWidgetItem] = {}
        lay.addWidget(self.watch_list)
        lay.addWidget(split, 1)
        lay.addWidget(self.log_view)

        for pane in (self.local, self.remote):
            pane.transfer.connect(self.transfer)
            pane.upload_paths.connect(lambda paths, target: self.upload(paths, target))
            pane.edit.connect(lambda e, p: win.open_editor(p.session, e))
            pane.edit_external.connect(win.edit_external)
            pane.message.connect(win.show_message)
        self.local.bookmarks = self.remote.bookmarks = win.bookmarks
        tree = bool(win.settings["show_tree"])
        self.local.set_tree_visible(tree)
        self.remote.set_tree_visible(tree)
        self.local.start()
        self.remote.start()
        self._keepalive = QTimer(self, interval=60_000)
        self._keepalive.timeout.connect(self.remote_session.keepalive)
        self._keepalive.start()

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

    # ------------------------------------------------------------ folder watch
    def _toggle_watch(self, on: bool) -> None:
        if not on:
            self.stop_watch()
            return
        if self.watcher is not None:
            return
        from ..core import watch as W
        local, remote = self.local.path, self.remote.path
        site = self.remote_session.site
        backend = self.remote_session.backend
        if not local or not remote or backend is None:
            self.win.show_message("Connect to the server first, then open the folders to keep in step.", True)
            self.watch_btn.setChecked(False)
            return
        warn = ("<p style='color:%s'><b>%s is marked as production.</b></p>" % (C["danger"], site.label)
                if site.production else "")
        box = QMessageBox(QMessageBox.Question, "Watch folder",
                          f"{warn}<p>Upload every file that changes in<br><b>{local}</b><br>"
                          f"to <b>{site.label}:{remote}</b>?</p>"
                          "<p>New and changed files go up as soon as they're saved. Files you delete "
                          "locally are <b>not</b> deleted on the server. Already-different files aren't "
                          "touched: use Compare &amp; sync for those first.</p>"
                          f"<p style='color:{C['muted']}'>Skipped: .git, node_modules, editor temp files…</p>",
                          QMessageBox.Yes | QMessageBox.Cancel, self)
        box.button(QMessageBox.Yes).setText("Start watching")
        if box.exec() != QMessageBox.Yes:
            self.watch_btn.setChecked(False)
            return
        join = backend.join
        engine = self.win.engine

        def changed(rels: list[str]) -> None:
            jobs = W.queue_uploads(engine, site, local, remote, rels, join)
            if jobs:
                on_ui(lambda: self._watch_update(len(jobs), rels, jobs))
        self.watcher = W.FolderWatcher(local, changed, on_error=lambda m: self.log(f"watch: {m}", True))
        try:
            self.watcher.start()
        except OSError as e:
            self.watcher = None
            self.watch_btn.setChecked(False)
            self.win.show_message(f"Can't watch {local}: {e.strerror or e}", True)
            return
        self.watch_count = 0
        self.watch_target = f"{local}  →  {site.label}:{remote}"
        self.watch_text.setText(f"Watching  {self.watch_target}")
        self.watch_bar.show()
        self.log(f"Watching {local} → {remote}")
        self.win.tab_watch_changed(self)

    WATCH_LIST_MAX = 300

    def _watch_update(self, n: int, rels: list[str], jobs: list | None = None) -> None:
        for j in jobs or []:
            if j.is_dir:
                continue
            it = QTreeWidgetItem([time.strftime("%H:%M:%S"), os.path.relpath(j.src, self.local.path), "queued"])
            it.setToolTip(1, f"{j.src}  →  {j.dst}")
            self.watch_list.insertTopLevelItem(0, it)
            self.watch_items[j.id] = it
        while self.watch_list.topLevelItemCount() > self.WATCH_LIST_MAX:
            old = self.watch_list.takeTopLevelItem(self.watch_list.topLevelItemCount() - 1)
            self.watch_items = {k: v for k, v in self.watch_items.items() if v is not old}
        self.watch_count += n
        last = rels[-1] if rels else ""
        self.watch_text.setText(f"Watching  {self.watch_target}   ·   {self.watch_count} uploaded"
                                + (f"   ·   last: {last}" if last else ""))
        for r in rels:
            self.log(f"changed: {r}")

    def watch_job_changed(self, job) -> None:
        it = self.watch_items.get(job.id)
        if it is None:
            return
        text = {E.DONE: "uploaded", E.FAILED: f"failed: {job.error}", E.RUNNING: "uploading …",
                E.SKIPPED: "skipped", E.CANCELLED: "cancelled"}.get(job.status, job.status)
        it.setText(2, text)
        it.setForeground(2, QColor(C["danger"] if job.status == E.FAILED else
                                   C["ok"] if job.status == E.DONE else C["muted"]))
        if job.status in (E.DONE, E.FAILED, E.SKIPPED, E.CANCELLED):
            self.watch_items.pop(job.id, None)

    def stop_watch(self) -> None:
        if self.watcher is None:
            return
        self.watcher.stop()
        self.watcher = None
        self.watch_bar.hide()
        self.log("Stopped watching")
        if self.watch_btn.isChecked():
            self.watch_btn.setChecked(False)
        self.win.tab_watch_changed(self)

    def close(self) -> None:
        self.stop_watch()
        self._keepalive.stop()
        self.win.settings["last_local_dir"] = self.local.path
        self.local_session.close()
        self.remote_session.close()


class TabIcon(QLabel):
    """A tab's icon, drawn by us (see _Tabs). Keeps its QIcon so a theme change can redraw it."""

    def __init__(self, ico: QIcon):
        super().__init__()
        self.set_qicon(ico)

    def set_qicon(self, ico: QIcon) -> None:
        self.qicon = ico
        self.setPixmap(ico.pixmap(16, 16))


class _Tabs(QTabWidget):
    """Tabs whose icon and close button we place ourselves. The style-sheet tab keeps 6 px of margin
    above the label, and Qt centers its own icon/close button on the whole tab, so both land a few
    pixels above the text; each sits in a small holder that parks it on the label's line."""

    OFFSET = 3          # px the icon / button is pushed down from the middle of the tab

    def _holder(self, child, size: int, left: int = 0) -> QWidget:
        holder = QWidget()
        holder.setFixedSize(size + 4 + left, 24)
        child.setParent(holder)
        child.move(left, 12 + self.OFFSET - child.height() // 2)
        return holder

    def addTab(self, widget, *args):  # noqa: N802
        if len(args) == 2:                          # (widget, icon, label)
            ico, label = args
            index = super().addTab(widget, label)
            self.setTabIcon(index, ico)
            return index
        return super().addTab(widget, *args)

    def setTabIcon(self, index: int, ico) -> None:  # noqa: N802
        bar = self.tabBar()
        old = bar.tabButton(index, QTabBar.LeftSide)
        if old is not None and getattr(old, "icon_label", None) is not None and not ico.isNull():
            old.icon_label.set_qicon(ico)
            return
        if ico.isNull():
            bar.setTabButton(index, QTabBar.LeftSide, None)
            return
        label = TabIcon(ico)
        label.setFixedSize(16, 16)
        holder = self._holder(label, 16, left=8)
        holder.icon_label = label
        bar.setTabButton(index, QTabBar.LeftSide, holder)

    def tabInserted(self, index: int) -> None:  # noqa: N802
        super().tabInserted(index)
        btn = QToolButton(objectName="TabClose", toolTip="Close tab")
        btn.setIcon(icon("x", C["muted"], 14))
        btn.setIconSize(QSize(14, 14))
        btn.setFixedSize(20, 20)
        btn.setCursor(Qt.ArrowCursor)
        btn.clicked.connect(lambda _=False, b=btn: self._close_for(b))
        holder = self._holder(btn, 20)
        holder.btn = btn
        self.tabBar().setTabButton(index, QTabBar.RightSide, holder)

    def _close_for(self, btn) -> None:
        bar = self.tabBar()
        for i in range(bar.count()):
            holder = bar.tabButton(i, QTabBar.RightSide)
            if holder is not None and getattr(holder, "btn", None) is btn:
                self.tabCloseRequested.emit(i)
                return


class MainWindow(QMainWindow):
    def __init__(self, store: Store, settings: Settings):
        super().__init__()
        self.store = store
        self.settings = settings
        store.keep_tokens()
        self.keychain_account = ""          # set by main(); empty in tests
        self.setWindowTitle("BlamixFiles")
        self.resize(1320, 820)
        from .palette import Bookmarks
        self.bookmarks = Bookmarks(store)
        from ..core.ssh import set_site_resolver
        set_site_resolver(lambda sid: self.store.sites.get(sid))      # jump hosts
        self.queue_store = QueueStore(data_dir() / "queue.db", keep_site=lambda sid: sid in self.store.sites)
        self.engine = E.TransferEngine(self._connector, workers=int(settings["workers"]),
                                       on_change=lambda j: on_ui(lambda: self._job_changed(j)),
                                       policy=settings["policy"], ask=self._ask_overwrite,
                                       preserve_mtime=bool(settings["preserve_mtime"]),
                                       store=self.queue_store,
                                       limit_up=int(settings["limit_up_kb"]) * 1024,
                                       limit_down=int(settings["limit_down_kb"]) * 1024,
                                       verify=bool(settings["verify_checksums"]),
                                       log_path=transfer_log_path())
        self._refresh_timer = QTimer(singleShot=True, interval=400)
        self._refresh_timer.timeout.connect(self._refresh_targets)
        self._refresh_dirs: set[tuple[int, str]] = set()

        root = QWidget()
        rl = QHBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        self.sidebar = self._build_sidebar()
        rl.addWidget(self.sidebar)

        right = QWidget()
        vl = QVBoxLayout(right)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        qc = QWidget(objectName="Toolbar")
        ql = QHBoxLayout(qc)
        ql.setContentsMargins(10, 8, 10, 8)
        self.sidebar_btn = QToolButton(toolTip="Show/hide the server list (Ctrl+B)")
        self.sidebar_btn.setIcon(icon("sidebar"))
        self.sidebar_btn.clicked.connect(self.toggle_sidebar)
        ql.addWidget(self.sidebar_btn)
        self.quick = QLineEdit(placeholderText="Quick connect:  sftp://user@host/path   ftp://…   ftps://…   "
                                               "(Ctrl+L)")
        self.quick.returnPressed.connect(self.quick_connect)
        ql.addWidget(self.quick, 1)
        go = QPushButton("Connect", objectName="Primary")
        go.clicked.connect(self.quick_connect)
        ql.addWidget(go)
        vl.addWidget(qc)

        self.tabs = _Tabs()
        self.tabs.tabBar().setDrawBase(False)       # the 1 px light line the style draws under the tabs
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
        self.external_btn = QToolButton(objectName="Ghost")
        self.external_btn.setPopupMode(QToolButton.InstantPopup)
        self.external_btn.hide()
        self.statusBar().addPermanentWidget(self.external_btn)
        self.queue_label = QLabel("")
        self.statusBar().addPermanentWidget(self.queue_label)
        self._build_menu()
        self.reload_sites()
        restored = self.queue_store.load(self.store.sites)
        if restored:
            self.engine.restore(restored, paused=True)
            self.queue.offer_resume(len(restored))
        self.sidebar.setVisible(bool(settings["show_sidebar"]))
        try:                                   # "Follow the system" also follows the OS while the app runs
            QApplication.instance().styleHints().colorSchemeChanged.connect(self._system_scheme_changed)
        except AttributeError:
            pass
        from .update import Updates
        self.updates = Updates(self)
        self.force_quit = False             # set by the updater: quit without the "are you sure" questions
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
        from .dialogs import _logo
        brand = QHBoxLayout()
        brand.setSpacing(10)
        logo = _logo("app.png", 30, self)
        if logo:
            brand.addWidget(logo)
        bt = QVBoxLayout()
        bt.setSpacing(0)
        bt.addWidget(QLabel("BlamixFiles", objectName="Brand", toolTip=f"Version {__version__}"))
        bt.addWidget(QLabel("SFTP · FTP · S3 · SMB · Cloud", objectName="BrandSub", toolTip=f"Version {__version__}"))
        brand.addLayout(bt, 1)
        sl.addLayout(brand)
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
        self.site_tree = RowTree()
        self.site_tree.setObjectName("ServerTree")
        self.site_tree.setHeaderHidden(True)
        self.site_tree.setIndentation(12)
        self.site_tree.itemActivated.connect(self._site_activated)
        self.site_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.site_tree.customContextMenuRequested.connect(self._site_menu)
        sl.addWidget(self.site_tree, 1)
        coffee = QPushButton(icon("coffee", C["muted"]), "  Buy me a coffee", objectName="Ghost")
        coffee.setToolTip("BlamixFiles is free. If it saves you time, a coffee keeps it going.")
        coffee.clicked.connect(lambda: webbrowser.open(KOFI))
        ver = QPushButton(f"v{__version__}", objectName="Ghost")
        ver.setToolTip("About BlamixFiles")
        ver.setStyleSheet(f"color:{C['faint']}; font-size:8.5pt; padding:6px 8px;")
        ver.clicked.connect(self.about)
        bottom = QHBoxLayout()
        bottom.setSpacing(0)
        bottom.addWidget(coffee, 1)
        bottom.addWidget(ver)
        sl.addLayout(bottom)
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
                     "• Moving from FileZilla or WinSCP? <b>File → Import</b> brings your sites over "
                     "(BlamixShell servers too).")
        sub.setWordWrap(True)
        sub.setObjectName("Muted")
        lay.addWidget(sub)
        row = QHBoxLayout()
        b1 = QPushButton(icon("plus", C["on_accent"]), " New site", objectName="Primary")
        b1.clicked.connect(self.new_site)
        b2 = QPushButton(icon("import"), " Import sites")
        im = QMenu(b2)
        im.addAction("From FileZilla…", self.import_filezilla)
        im.addAction("From WinSCP…", self.import_winscp)
        im.addAction("From BlamixShell…", self.import_blamixshell)
        b2.setMenu(im)
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
        act(f, "Command palette…", self.show_palette, "Ctrl+K", "search")
        f.addSeparator()
        act(f, "Import from FileZilla…", self.import_filezilla, None, "import")
        act(f, "Import from WinSCP…", self.import_winscp, None, "import")
        act(f, "Import from BlamixShell…", self.import_blamixshell, None, "import")
        f.addSeparator()
        if keychain.available():
            self.keychain_act = act(f, f"Unlock with {keychain.backend_name()}", self.toggle_keychain)
            self.keychain_act.setCheckable(True)
            self.keychain_act.setToolTip("Remember the master password in the OS secure store")
            f.aboutToShow.connect(lambda: self.keychain_act.setChecked(self._keychain_saved()))
            f.addSeparator()
        if platform_ui.can_make_desktop_shortcut():
            act(f, "Create desktop shortcut", self.make_desktop_shortcut)
            f.addSeparator()
        act(f, "Close tab", lambda: self.close_tab(self.tabs.currentIndex()), "Ctrl+W")
        act(f, "Quit", self.close, "Ctrl+Q")
        self.sync_menu = mb.addMenu("&Sync")
        self.reload_profiles()
        v = mb.addMenu("&View")
        act(v, "Show/hide transfer queue", lambda: self.queue.setVisible(not self.queue.isVisible()), "Ctrl+J")
        act(v, "Switch between the two file lists", self.switch_pane, "F6")
        act(v, "Transfer log…", self.show_transfer_log)
        act(v, "Show/hide server list", self.toggle_sidebar, "Ctrl+B")
        act(v, "Show/hide folder trees", self.toggle_trees, "Ctrl+T")
        tm = v.addMenu("Theme")
        grp = QActionGroup(self)
        current = theme.canonical(self.settings["theme"])
        for name in theme.theme_names():
            a = tm.addAction("Follow the system" if name == theme.SYSTEM else name)
            a.setCheckable(True)
            a.setChecked(name == current)
            a.triggered.connect(lambda _=False, n=name: self.set_theme(n))
            grp.addAction(a)
        h = mb.addMenu("&Help")
        act(h, "Check for updates", lambda: self.updates.check(manual=True), None, "refresh")
        act(h, "Install update from file…", lambda: self.updates.from_file(), None, "import")
        auto = act(h, "Check for updates automatically", self._set_auto_updates)
        auto.setCheckable(True)
        auto.setChecked(updater.updates_allowed(self.settings))
        auto.setEnabled(updater.update_check_policy() is None)       # an admin policy can lock it
        h.addSeparator()
        act(h, "BlamixFiles on GitHub", lambda: webbrowser.open(GITHUB), None, "github")
        act(h, "Report a problem", lambda: webbrowser.open(GITHUB + "/issues"), None, "help")
        act(h, "☕ Buy me a coffee", lambda: webbrowser.open(KOFI), None, "coffee")
        act(h, f"Made by {COMPANY}", lambda: webbrowser.open(COMPANY_URL), None, "link")
        act(h, "Diagnostics…", self.show_diagnostics, None, "help")
        h.addSeparator()
        act(h, "About BlamixFiles", self.about)

    def make_desktop_shortcut(self) -> None:
        err = platform_ui.create_desktop_shortcut()
        if err:
            QMessageBox.warning(self, "Desktop shortcut", err)
        else:
            self.show_message("Desktop shortcut created")

    def _set_auto_updates(self, on: bool) -> None:
        self.settings["check_updates"] = bool(on)
        self.settings.save()

    def unsaved_editors(self) -> list:
        return [self.tabs.widget(i) for i in range(self.tabs.count())
                if isinstance(self.tabs.widget(i), EditorTab) and self.tabs.widget(i).dirty]

    def switch_pane(self) -> None:
        """F6: move the keyboard focus between this computer's list and the server's list."""
        tab = self.tabs.currentWidget()
        if isinstance(tab, SiteTab):
            on_local = tab.local.isAncestorOf(QApplication.focusWidget() or tab)
            (tab.remote if on_local else tab.local).tree.setFocus()

    def show_diagnostics(self, crash_text: str = "") -> None:
        from .tools import DiagnosticsDialog
        DiagnosticsDialog(self.settings.data, crash_text, self).exec()

    def check_crashes(self) -> None:
        """Offer the newest crash report once, the first time the app starts after a crash."""
        from .. import diagnostics
        seen = float(self.settings["crash_seen"] or 0)
        new = diagnostics.new_crashes(seen)
        self.settings["crash_seen"] = time.time()
        self.settings.save()
        if not new or not seen:                       # (first run ever: nothing to compare against)
            return
        box = QMessageBox(QMessageBox.Warning, "BlamixFiles", "BlamixFiles closed unexpectedly last time. "
                          "Do you want to see the report (to send it with a problem report)?",
                          QMessageBox.Yes | QMessageBox.No, self)
        box.button(QMessageBox.Yes).setText("Show the report")
        box.button(QMessageBox.No).setText("Not now")
        if box.exec() == QMessageBox.Yes:
            self.show_diagnostics(new[0].read_text(encoding="utf-8", errors="replace"))

    def show_transfer_log(self) -> None:
        from .dialogs import TransferLogDialog
        TransferLogDialog(transfer_log_path(), self).exec()

    def toggle_sidebar(self) -> None:
        show = not self.sidebar.isVisible()
        self.sidebar.setVisible(show)
        self.settings["show_sidebar"] = show
        self.settings.save()

    def set_theme(self, name: str) -> None:
        """Switch the running app to a theme and remember the choice."""
        self.settings["theme"] = name
        self.settings.save()
        self.apply_theme(name)

    def apply_theme(self, name: str) -> None:
        theme.switch_theme(QApplication.instance(), name)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, EditorTab):
                w.restyle()
        self.show_message(f"Theme: {theme.CURRENT}")

    def _system_scheme_changed(self, *_args) -> None:
        if theme.canonical(self.settings["theme"]) == theme.SYSTEM:
            self.apply_theme(theme.SYSTEM)

    # ---- unlock with the OS keychain
    def _keychain_saved(self) -> bool:
        return bool(self.keychain_account and keychain.load(self.keychain_account))

    def toggle_keychain(self) -> None:
        name = keychain.backend_name()
        if not self.keychain_account or self._keychain_saved():
            keychain.delete(self.keychain_account)
            return
        pw, ok = QInputDialog.getText(self, "Unlock automatically", f"Master password to store in {name}:",
                                      QLineEdit.Password)
        if not ok:
            return
        try:
            Vault.open(self.store.vault.path, pw)
        except WrongPassword:
            QMessageBox.warning(self, "Unlock automatically", "Wrong master password.")
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Unlock automatically", str(e))
            return
        if not keychain.save(self.keychain_account, pw):
            QMessageBox.warning(self, "Unlock automatically", f"Couldn't store the password in {name}.")

    def show_palette(self) -> None:
        from .palette import CommandPalette
        entries = []
        for site in sorted(self.store.sites.values(), key=lambda x: x.label.lower()):
            entries.append((site.label, site.address + (f"  ·  {site.group}" if site.group else ""),
                            lambda s=site: self.open_site(s), "server"))
        for b in self.store.bookmarks:
            site = self.store.sites.get(b["site_id"]) if b["site_id"] else None
            if b["site_id"] and site is None:
                continue
            where = site.label if site else "This computer"
            entries.append((b["path"], f"bookmark · {where}", lambda b=b: self.open_bookmark(b), "star"))
        for p in self.store.sync_profiles:
            if p["site_id"] in self.store.sites:
                entries.append((p["name"], "sync profile", lambda p=p: self.run_profile(p), "sync"))
        actions = [("New site", self.new_site, "plus"), ("Compare & sync current tab", self.open_sync, "sync"),
                   ("Import from FileZilla", self.import_filezilla, "import"),
                   ("Import from WinSCP", self.import_winscp, "import"),
                   ("Import from BlamixShell", self.import_blamixshell, "import"),
                   ("Watch local folder & upload changes (current tab)", self.toggle_watch, "eye"),
                   ("Transfer log", self.show_transfer_log, "download"),
                   ("Show/hide server list", self.toggle_sidebar, "sidebar"),
                   ("Show/hide folder trees", self.toggle_trees, "folder"),
                   ("Show/hide transfer queue", lambda: self.queue.setVisible(not self.queue.isVisible()), "download"),
                   ("Pause/resume transfers", self.queue._toggle_pause, "pause"),
                   ("Retry failed transfers", lambda: self.engine.retry(), "retry"),
                   ("Check for updates", lambda: self.updates.check(manual=True), "refresh"),
                   ("Install update from file", lambda: self.updates.from_file(), "import"),
                   ("Buy me a coffee", lambda: webbrowser.open(KOFI), "coffee")]
        entries += [(t, "action", cb, ic) for t, cb, ic in actions]
        pal = CommandPalette(entries, self, anchor=self.quick)
        pal.quick_connect = lambda text: (self.quick.setText(text), self.quick_connect())
        pal.exec()

    def open_bookmark(self, b: dict) -> None:
        if not b["site_id"]:                      # a local folder: current site tab's left pane
            tab = self.tabs.currentWidget()
            if isinstance(tab, SiteTab):
                tab.local.open_dir(b["path"])
            else:
                self.show_message("Open a site tab first: local bookmarks open in its left pane.", True)
            return
        site = self.store.sites.get(b["site_id"])
        tab = next((t for t in self.site_tabs() if t.remote_session.site.id == b["site_id"]), None)
        if tab is None:
            self.open_site(site, b["path"])
        else:
            self.tabs.setCurrentWidget(tab)
            tab.remote.open_dir(b["path"])

    def toggle_trees(self) -> None:
        show = not bool(self.settings["show_tree"])
        self.settings["show_tree"] = show
        self.settings.save()
        for t in self.site_tabs():
            t.local.set_tree_visible(show)
            t.remote.set_tree_visible(show)

    # ------------------------------------------------------------ restore tabs
    def _save_open_tabs(self) -> None:
        tabs = []
        for t in self.site_tabs():
            sid = t.remote_session.site.id
            if sid in self.store.sites:                # quick-connect tabs aren't saved
                tabs.append({"site_id": sid, "remote": t.remote.path, "local": t.local.path})
        self.settings["open_tabs"] = tabs

    def restore_tabs(self) -> None:
        if not self.settings["restore_tabs"]:
            return
        for t in self.settings["open_tabs"] or []:
            site = self.store.sites.get(t.get("site_id"))
            if site is None:
                continue
            s = site.copy()
            if t.get("local"):
                s.local_dir = t["local"]
            self.open_site(s, t.get("remote", ""))

    # ------------------------------------------------------------ sync
    def reload_profiles(self) -> None:
        m = self.sync_menu
        m.clear()
        a = m.addAction(icon("sync"), "Compare && sync current tab…", lambda: self.open_sync())
        a.setShortcut(QKeySequence("Ctrl+Shift+S"))
        w = m.addAction(icon("eye"), "Watch local folder && upload changes", self.toggle_watch)
        w.setShortcut(QKeySequence("Ctrl+Shift+W"))
        profiles = self.store.sync_profiles
        if profiles:
            m.addSeparator()
            for p in profiles:
                site = self.store.sites.get(p["site_id"])
                label = f"{p['name']}   ({site.label if site else 'site deleted'})"
                act = m.addAction(icon("star"), label, lambda p=p: self.run_profile(p))
                act.setEnabled(site is not None)
            m.addSeparator()
            from ..core import schedule as SC
            if SC.available():
                sm = m.addMenu(icon("bolt"), "Schedule profile")
                planned = SC.listing()
                for p in profiles:
                    when = planned.get(p["name"], "")
                    sm.addAction(p["name"] + (f"   ({when})" if when else ""),
                                 lambda p=p, when=when: self.schedule_profile(p["name"], when))
            dm = m.addMenu(icon("trash"), "Delete profile")
            for p in profiles:
                dm.addAction(p["name"], lambda p=p: self._delete_profile(p["name"]))

    def after_transfers(self, site, jobs: list, name: str, command: str, webhook: str, info: dict) -> None:
        """Run a sync profile's hooks once its transfers (and the files in its folders) are finished."""
        from ..core import sync as S
        started = time.time() - 1
        ids = {j.id for j in jobs}
        timer = QTimer(self, interval=1000)

        def mine(j) -> bool:
            return j.id in ids or (j.site.id == site.id and j.kind in ("upload", "download")
                                   and (j.started or 0) >= started)

        def check() -> None:
            ours = [j for j in self.engine.jobs if mine(j)]
            if any(j.status in (E.QUEUED, E.RUNNING) for j in ours):
                return
            timer.stop()
            timer.deleteLater()
            files = [j for j in ours if not j.is_dir]
            failed = sum(j.status == E.FAILED for j in files)
            result = dict(info, status="failed" if failed else "ok", files=sum(j.status == E.DONE for j in files),
                          bytes=sum(j.size for j in files if j.status == E.DONE), failed=failed)

            def work() -> None:
                notes = S.run_hooks(name, command, webhook, result)
                on_ui(lambda: self.show_message(f"After sync “{name}”: " + "; ".join(notes)))
            threading.Thread(target=work, daemon=True, name="sync-hooks").start()
        timer.timeout.connect(check)
        timer.start()

    def schedule_profile(self, name: str, current: str = "") -> None:
        from ..core import schedule as SC
        from .tools import ScheduleDialog
        dlg = ScheduleDialog(self, name, current, self)
        if dlg.exec() != QDialog.Accepted:
            return
        when = dlg.choice()
        try:
            if when is None:
                SC.remove(name)
                self.show_message(f"“{name}” no longer runs on a schedule")
            else:
                SC.install(name, when)
                self.show_message(f"“{name}” will sync {when.describe()}")
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Schedule", f"Couldn't set up the schedule:\n{e}")
        self.reload_profiles()

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
            if s.is_ssh:
                m.addAction(icon("lock"), "Set up key login…", lambda: self.setup_key(s))
                from .terminal import find_blamixshell
                if find_blamixshell(self.settings["blamixshell_path"]):
                    m.addAction(icon("terminal"), "Open in BlamixShell", lambda: self.open_in_blamixshell(s))
            m.addSeparator()
            m.addAction(icon("trash", C["danger"]), "Delete", lambda: self.delete_site(s))
        else:
            m.addAction(icon("plus"), "New site…", self.new_site)
        m.exec(self.site_tree.viewport().mapToGlobal(pos))

    def open_in_blamixshell(self, site) -> None:
        from .terminal import open_in_blamixshell
        err = open_in_blamixshell(site, lambda sid: self.store.sites.get(sid), self.settings["blamixshell_path"])
        self.show_message(err or f"Opening {site.label} in BlamixShell …", bool(err))

    def setup_key(self, site) -> None:
        from .tools import KeySetupDialog
        KeySetupDialog(self, site, self).exec()

    def new_site(self) -> None:
        dlg = SiteDialog(None, self.store.all_groups(), self, sites=list(self.store.sites.values()))
        if dlg.exec():
            self.store.upsert(dlg.site)
            self.reload_sites()

    def edit_site(self, s: Site) -> None:
        dlg = SiteDialog(s, self.store.all_groups(), self, sites=list(self.store.sites.values()))
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

    def _imported(self, app: str, where: str, sites: list, notes: list[str]) -> None:
        added = self.store.import_sites(sites)
        self.reload_sites()
        with_pw = sum(1 for x in sites if x.password)
        msg = f"Imported {added} new site(s) from {where}"
        if len(sites) - added:
            msg += f" ({len(sites) - added} already here)"
        msg += "."
        if with_pw:
            msg += f"\n\n{with_pw} saved password(s) are now encrypted in your BlamixFiles vault."
        if notes:
            msg += "\n\n" + "\n".join("• " + n for n in notes[:12])
            if len(notes) > 12:
                msg += f"\n… and {len(notes) - 12} more"
        QMessageBox.information(self, f"Imported from {app}", msg)

    def import_winscp(self) -> None:
        from .. import importers as I
        src = I.winscp_default_source()
        if src == "registry":
            r = QMessageBox.question(
                self, "Import from WinSCP",
                "Read the sessions WinSCP keeps in the Windows registry?\n\n"
                "Choose No to pick a WinSCP.ini file instead.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.No:
                src = ""
        if not src:
            src, _ = QFileDialog.getOpenFileName(self, "WinSCP.ini", os.path.expanduser("~"),
                                                 "WinSCP settings (WinSCP.ini *.ini)")
            if not src:
                return
        try:
            sites, notes = I.import_winscp(src)
        except Exception as e:  # noqa: BLE001 (shown to the user)
            QMessageBox.warning(self, "Import from WinSCP", f"Couldn't read the WinSCP sessions:\n{e}")
            return
        self._imported("WinSCP", "the Windows registry" if src == "registry" else src, sites, notes)

    def import_blamixshell(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        from .. import importers as I
        found = I.blamixshell_default_path()
        path = str(found) if found else ""
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "BlamixShell vault", os.path.expanduser("~"),
                                                  "BlamixShell vault (vault.sdv *.sdv)")
            if not path:
                return
        while True:
            pw, ok = QInputDialog.getText(self, "Import from BlamixShell",
                                          f"BlamixShell master password for\n{path}:", QLineEdit.Password)
            if not ok:
                return
            try:
                sites, notes = I.import_blamixshell(path, pw)
                break
            except I.ImportError_ as e:
                if "password" not in str(e).lower():
                    QMessageBox.warning(self, "Import from BlamixShell", str(e))
                    return
                QMessageBox.warning(self, "Import from BlamixShell", str(e))
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "Import from BlamixShell", f"Couldn't read the vault:\n{e}")
                return
        self._imported("BlamixShell", path, sites, notes)

    # ------------------------------------------------------------ tabs
    def open_site(self, site: Site, path: str = "") -> None:
        if site.id in self.store.sites:
            self.store.touch(site.id)
        tab = SiteTab(self, site, path)
        i = self.tabs.addTab(tab, icon("server", site.color or (C["danger"] if site.production else C["muted"]), 16),
                             site.label)
        self.tabs.setTabToolTip(i, site.address)
        self.tabs.setCurrentIndex(i)
        a11y.apply(tab)
        tab.local.tree.setFocus()
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
        ed.open_external.connect(lambda s=session, e=entry: self.external.open(s, e))

    # ------------------------------------------------------------ edit in another app
    @property
    def external(self):
        if getattr(self, "_external", None) is None:
            from .external import ExternalEditor
            self._external = ExternalEditor(self)
        return self._external

    def edit_external(self, entry, pane_or_cmd) -> None:
        if pane_or_cmd == "choose":
            self.external.choose_program()
            return
        if pane_or_cmd == "default":
            self.external.use_default_app()
            return
        self.external.open(pane_or_cmd.session, entry)

    def external_status(self, n: int) -> None:
        """Status bar: 'N files open in other apps', with a menu to stop watching them."""
        self.external_btn.setVisible(n > 0)
        if not n:
            return
        self.external_btn.setText(f"✎ {n} file{'s' if n != 1 else ''} open in other apps")
        m = QMenu(self.external_btn)
        for e in list(self.external.edits.edits):
            sub = m.addMenu(f"{e.name}   ({e.site.label})")
            sub.addAction("Open again", lambda e=e: self.external._launch(e))
            sub.addAction("Upload now", lambda e=e: self.external._upload(e))
            sub.addAction("Stop watching", lambda e=e: self.external.stop(e))
        self.external_btn.setMenu(m)

    def toggle_watch(self) -> None:
        tab = self.tabs.currentWidget()
        if isinstance(tab, SiteTab):
            tab.watch_btn.toggle()
        else:
            self.show_message("Open a site tab first.", True)

    def tab_watch_changed(self, tab: "SiteTab") -> None:
        """A watching tab gets an eye icon, so it's visible from any tab."""
        i = self.tabs.indexOf(tab)
        if i == -1:
            return
        site = tab.site
        if tab.watcher is not None:
            self.tabs.setTabIcon(i, icon("eye", C["ok"], 16))
            self.tabs.setTabToolTip(i, f"{site.address}\nWatching {tab.local.path}")
        else:
            self.tabs.setTabIcon(i, icon("server", site.color or (C["danger"] if site.production else C["muted"]), 16))
            self.tabs.setTabToolTip(i, site.address)

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
            if getattr(self, "_external", None) is not None:
                self._external.forget_session(w.remote_session)
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
        for t in self.site_tabs():
            if t.watch_items:
                t.watch_job_changed(job)
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
        from .dialogs import AboutDialog
        AboutDialog(self).exec()

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        style_window(self)

    def closeEvent(self, e):  # noqa: N802
        dirty = self.unsaved_editors()
        busy = self.engine.pending()
        if (dirty or busy) and not self.force_quit:
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
        self._save_open_tabs()
        # unfinished transfers stay in the saved queue: offered again on the next start
        self.engine.store = None
        for t in self.site_tabs():
            t.close()
        self.settings.save()
        self.engine.shutdown()
        super().closeEvent(e)
