"""Compare & sync: pick folders and rules, see every change before it happens, apply."""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..core import sync as S
from ..core.backends.local import LocalBackend
from .bridge import on_ui
from .fmt import human_size, human_time
from .theme import C, icon, style_window

COLORS = {S.UPLOAD: C["accent"], S.DOWNLOAD: C["accent2"], S.MKDIR_REMOTE: C["muted"],
          S.MKDIR_LOCAL: C["muted"], S.DELETE_REMOTE: C["danger"], S.DELETE_LOCAL: C["danger"],
          S.CONFLICT: C["warn"]}
ICONS = {S.UPLOAD: "upload", S.DOWNLOAD: "download", S.MKDIR_REMOTE: "folder-plus",
         S.MKDIR_LOCAL: "folder-plus", S.DELETE_REMOTE: "trash", S.DELETE_LOCAL: "trash", S.CONFLICT: "help"}


class SyncDialog(QDialog):
    def __init__(self, win, tab, profile: S.SyncProfile | None = None, auto_compare: bool = False):
        super().__init__(win)
        self.win, self.tab = win, tab
        self.site = tab.remote_session.site
        self.plan: S.Plan | None = None
        self._cancel = False
        self._busy = False
        self.setWindowTitle(f"Compare & sync · {self.site.label}")
        self.resize(1080, 680)
        opt = profile.options if profile else S.SyncOptions(
            tolerance=2.0 if self.site.is_ssh else 60.0)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        paths = QHBoxLayout()
        self.local_dir = QLineEdit(profile.local_dir if profile else tab.local.path)
        self.remote_dir = QLineEdit(profile.remote_dir if profile else tab.remote.path)
        paths.addWidget(QLabel("This computer"))
        paths.addWidget(self.local_dir, 1)
        paths.addWidget(QLabel("⇄"))
        paths.addWidget(QLabel(self.site.label + ("  ·  PRODUCTION" if self.site.production else ""),
                               objectName="Prod" if self.site.production else ""))
        paths.addWidget(self.remote_dir, 1)
        lay.addLayout(paths)

        opts = QHBoxLayout()
        self.dir_group = QButtonGroup(self)
        for i, (key, label) in enumerate(((S.UPLOAD, "Upload  →"), (S.DOWNLOAD, "←  Download"),
                                          ("both", "⇄  Both ways"))):
            rb = QRadioButton(label)
            rb.setProperty("key", key)
            rb.setChecked(opt.direction == key)
            self.dir_group.addButton(rb, i)
            opts.addWidget(rb)
        self.mirror = QCheckBox("Mirror (delete extra files)")
        self.mirror.setChecked(opt.mirror)
        self.mirror.setToolTip("Also delete files that don't exist on the source side. "
                               "Every delete is listed below before anything happens.")
        opts.addSpacing(12)
        opts.addWidget(self.mirror)
        opts.addStretch(1)
        opts.addWidget(QLabel("Compare by"))
        self.compare_by = QComboBox()
        self.compare_by.addItem("size + modified time", "mtime")
        self.compare_by.addItem("size only", "size")
        self.compare_by.addItem("content checksum (slow)", "checksum")
        self.compare_by.setCurrentIndex(self.compare_by.findData(opt.compare))
        opts.addWidget(self.compare_by)
        lay.addLayout(opts)

        ex = QHBoxLayout()
        ex.addWidget(QLabel("Skip"))
        self.excludes = QLineEdit(", ".join(opt.excludes))
        self.excludes.setToolTip("Names or paths to leave out, comma separated. Wildcards work: *.log, cache/*")
        ex.addWidget(self.excludes, 1)
        self.tolerance = opt.tolerance
        self.compare_btn = QPushButton(icon("refresh", C["on_accent"]), " Compare", objectName="Primary")
        self.compare_btn.clicked.connect(self.run_compare)
        ex.addWidget(self.compare_btn)
        lay.addLayout(ex)

        self.dir_group.buttonToggled.connect(lambda *_: self._direction_changed())
        self._direction_changed()
        for w in (self.local_dir, self.remote_dir, self.excludes):
            w.textEdited.connect(lambda *_: self._invalidate())
        self.mirror.toggled.connect(lambda *_: self._invalidate())
        self.compare_by.currentIndexChanged.connect(lambda *_: self._invalidate())

        self.tree = QTreeWidget()
        self.tree.setObjectName("Files")
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setHeaderLabels(["Action", "Path", "Here", "On the server", "Why"])
        h = self.tree.header()
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        for i, w in ((0, 225), (2, 165), (3, 165), (4, 220)):
            h.setSectionResizeMode(i, QHeaderView.Interactive)
            self.tree.setColumnWidth(i, w)
        self.tree.itemChanged.connect(lambda *_: self._update_summary())
        lay.addWidget(self.tree, 1)

        bottom = QHBoxLayout()
        self.summary = QLabel("Press Compare to see what would change. Nothing is changed until you press Apply.")
        self.summary.setObjectName("Muted")
        self.summary.setWordWrap(True)
        bottom.addWidget(self.summary, 1)
        self.save_btn = QPushButton(icon("star"), " Save as profile")
        self.save_btn.clicked.connect(self.save_profile)
        bottom.addWidget(self.save_btn)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        bottom.addWidget(close)
        self.apply_btn = QPushButton(icon("play", C["on_accent"]), " Apply", objectName="Primary")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.apply)
        bottom.addWidget(self.apply_btn)
        lay.addLayout(bottom)
        self.profile_name = profile.name if profile else ""
        if auto_compare:
            self.run_compare()

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        style_window(self)

    # ------------------------------------------------------------ options
    def options(self) -> S.SyncOptions:
        direction = self.dir_group.checkedButton().property("key")
        return S.SyncOptions(direction=direction, mirror=self.mirror.isChecked() and direction != "both",
                             compare=self.compare_by.currentData(), tolerance=self.tolerance,
                             excludes=[x.strip() for x in self.excludes.text().split(",") if x.strip()])

    def _direction_changed(self) -> None:
        both = self.dir_group.checkedButton().property("key") == "both"
        self.mirror.setEnabled(not both)
        if both:
            self.mirror.setToolTip("Two-way sync never deletes: without a sync history a missing "
                                   "file could mean 'new' or 'deleted'.")
        self._invalidate()

    def _invalidate(self) -> None:
        if self.plan is not None:
            self.plan = None
            self.tree.clear()
            self.apply_btn.setEnabled(False)
            self.summary.setText("Options changed: press Compare again.")

    # ------------------------------------------------------------ compare
    def run_compare(self) -> None:
        if self._busy:
            self._cancel = True
            return
        opt = self.options()
        local_root, remote_root = self.local_dir.text().strip(), self.remote_dir.text().strip()
        if not local_root or not remote_root:
            return
        self._busy, self._cancel = True, False
        self.compare_btn.setText(" Stop")
        self.apply_btn.setEnabled(False)
        self.tree.clear()
        self.summary.setText("Scanning …")
        stop = lambda: self._cancel  # noqa: E731

        def progress(side):
            def cb(folder):
                on_ui(lambda: self.summary.setText(f"Scanning {side}: {folder}"))
            return cb

        # local and server are scanned at the same time
        local_result: dict = {}

        def scan_local():
            try:
                local_result["v"] = S.scan(LocalBackend(), local_root, opt.excludes, progress("here"), stop)
            except BaseException as e:  # noqa: BLE001
                local_result["e"] = e
        t = threading.Thread(target=scan_local, daemon=True)
        t.start()

        def scan_remote(b):
            remote = S.scan(b, remote_root, opt.excludes, progress("server"), stop)
            t.join()
            if "e" in local_result:
                raise local_result["e"]
            plan = S.compare(local_result["v"], remote, local_root, remote_root, opt)
            if plan.to_check:
                S.resolve_checksums(plan, LocalBackend(), b, progress("both sides"), stop)
            return plan
        self.tab.remote_session.run(scan_remote, self._compared, self._failed)

    def _failed(self, msg: str) -> None:
        self._busy = False
        self.compare_btn.setText(" Compare")
        self.summary.setText("Stopped." if msg == "Cancelled" else f"✖ {msg}")

    def _compared(self, plan: S.Plan) -> None:
        self._busy = False
        self.compare_btn.setText(" Compare")
        self.plan = plan
        self.tree.blockSignals(True)
        for a in plan.actions:
            it = QTreeWidgetItem([S.ACTION_LABELS[a.kind], a.rel + ("/" if a.is_dir else ""),
                                  self._describe(a.local), self._describe(a.remote), a.reason])
            it.setData(0, Qt.UserRole, a)
            it.setIcon(0, icon(ICONS[a.kind], COLORS[a.kind], 16))
            it.setForeground(0, QColor(COLORS[a.kind]))
            if a.kind == S.CONFLICT:
                a.enabled = False
                it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable)
            else:
                it.setCheckState(0, Qt.Checked if a.enabled else Qt.Unchecked)
            self.tree.addTopLevelItem(it)
        self.tree.blockSignals(False)
        self._update_summary()

    @staticmethod
    def _describe(e) -> str:
        if e is None:
            return "—"
        if e.is_dir:
            return "folder"
        return f"{human_size(e.size)} · {human_time(e.mtime)}"

    def _update_summary(self) -> None:
        if self.plan is None:
            return
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            a = it.data(0, Qt.UserRole)
            if a.kind != S.CONFLICT:
                a.enabled = it.checkState(0) == Qt.Checked
        p = self.plan
        text = p.summary()
        size = p.transfer_bytes()
        if size:
            text += f" · {human_size(size)} to transfer"
        self.summary.setText(text)
        self.apply_btn.setEnabled(any(a.enabled for a in p.actions))

    # ------------------------------------------------------------ apply
    def apply(self) -> None:
        p = self.plan
        if p is None:
            return
        deletes = [a for a in p.actions if a.enabled and a.kind in S.DELETES]
        if deletes:
            where = "on the server" if deletes[0].kind == S.DELETE_REMOTE else "on this computer"
            sample = "\n".join("  " + a.rel for a in deletes[:8]) + ("\n  …" if len(deletes) > 8 else "")
            warn = (f"\n\n{self.site.label} is a PRODUCTION server." if self.site.production
                    and deletes[0].kind == S.DELETE_REMOTE else "")
            if QMessageBox.warning(self, "Delete files?",
                                   f"This deletes {len(deletes)} item(s) {where}:\n\n{sample}{warn}\n\n"
                                   "This can't be undone. Continue?",
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        self.apply_btn.setEnabled(False)
        self.summary.setText("Applying …")
        site = self.site

        def run(b):
            return len(S.apply(p, site, self.win.engine, b, LocalBackend()))
        self.tab.remote_session.run(run, self._applied, self._failed)

    def _applied(self, n: int) -> None:
        self.win.show_message(f"Sync: {n} transfer(s) queued" if n else "Sync: done", False)
        self.tab.local.refresh()
        self.tab.remote.refresh()
        self.accept()

    # ------------------------------------------------------------ profiles
    def save_profile(self) -> None:
        if self.site.id not in self.win.store.sites:
            QMessageBox.information(self, "Save profile", "Save this connection as a site first "
                                    "(profiles belong to a saved site).")
            return
        name, ok = QInputDialog.getText(self, "Save sync profile", "Profile name:",
                                        text=self.profile_name or f"{self.site.label} sync")
        name = name.strip()
        if not (ok and name):
            return
        prof = S.SyncProfile(name, self.site.id, self.local_dir.text().strip(),
                             self.remote_dir.text().strip(), self.options())
        self.win.store.save_profile(prof.to_dict())
        self.profile_name = name
        self.win.reload_profiles()
        self.win.show_message(f"Saved sync profile “{name}”. Run it from the Sync menu or: "
                              f"blamixfiles sync \"{name}\"", False)

