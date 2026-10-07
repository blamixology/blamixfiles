"""Dialogs for the tools in the file lists and menus: search, folder size, bulk rename, compare two files,
copy to another server, SSH key setup, scheduled syncs, after-sync hooks and diagnostics.

Long jobs (search, folder size) use their own connection on a plain thread, so the file list stays usable
while they run: SFTP opens another channel on the login that is already there (no new password or 2FA
code), other protocols log in again with the same details.
"""
from __future__ import annotations

import os
import sys
import threading
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QRadioButton, QSpinBox, QTimeEdit, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from ..core import rename as R
from ..core import search as F
from ..core.backends.local import LocalBackend
from ..core.errors import friendly
from .bridge import on_ui
from .fmt import human_size, human_time
from .theme import C, icon, style_window


class _Dialog(QDialog):
    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        style_window(self)


def own_connection(pane):
    """A connection of its own for a long job started from a file pane (see the module docstring)."""
    if pane.session.is_local:
        return LocalBackend()
    return pane.session.transfer_connector(pane.session.site)


def _close(b) -> None:
    try:
        b.close()
    except Exception:  # noqa: BLE001
        pass


# ======================================================================= search
class SearchDialog(_Dialog):
    def __init__(self, pane, parent=None):
        super().__init__(parent)
        self.pane = pane
        self._stop = threading.Event()
        self._running = False
        self.setWindowTitle(f"Search · {pane.title.text()}")
        self.resize(900, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        form = QFormLayout()
        self.root = QLineEdit(pane.path)
        form.addRow("Look in", self.root)
        self.name = QLineEdit(placeholderText="*.log, report, IMG_2024* … (empty = everything)")
        self.name.returnPressed.connect(self.start)
        form.addRow("Name", self.name)
        row = QHBoxLayout()
        self.kind = QComboBox()
        for label, key in (("Files and folders", "any"), ("Files", "file"), ("Folders", "folder")):
            self.kind.addItem(label, key)
        self.larger = QLineEdit(placeholderText="at least, e.g. 10M")
        self.smaller = QLineEdit(placeholderText="at most, e.g. 1G")
        self.newer = QLineEdit(placeholderText="changed within, e.g. 7d")
        self.older = QLineEdit(placeholderText="not changed for, e.g. 30d")
        for w in (self.kind, self.larger, self.smaller, self.newer, self.older):
            row.addWidget(w)
        form.addRow("Only", row)
        self.case = QCheckBox("Match case")
        form.addRow("", self.case)
        lay.addLayout(form)

        self.results = QTreeWidget()
        self.results.setObjectName("Files")
        self.results.setRootIsDecorated(False)
        self.results.setHeaderLabels(["Path", "Size", "Modified"])
        self.results.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.results.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.results.itemDoubleClicked.connect(self._reveal)
        self.results.setAccessibleName("Search results")
        lay.addWidget(self.results, 1)

        bottom = QHBoxLayout()
        self.status = QLabel("Enter what to look for and press Search.", objectName="Muted")
        bottom.addWidget(self.status, 1)
        self.reveal_btn = QPushButton(icon("folder-open"), " Show in folder")
        self.reveal_btn.clicked.connect(lambda: self._reveal(self.results.currentItem()))
        bottom.addWidget(self.reveal_btn)
        self.send_btn = QPushButton(icon("download" if not pane.session.is_local else "upload"),
                                    " Download" if not pane.session.is_local else " Upload")
        self.send_btn.setToolTip("Copy the selected results to the folder open in the other list")
        self.send_btn.clicked.connect(self._send)
        bottom.addWidget(self.send_btn)
        self.go = QPushButton(icon("search", C["on_accent"]), " Search", objectName="Primary")
        self.go.clicked.connect(self.start)
        bottom.addWidget(self.go)
        lay.addLayout(bottom)

    def query(self) -> F.Query:
        now = time.time()
        return F.Query(name=self.name.text().strip(), case_sensitive=self.case.isChecked(),
                       min_size=F.parse_size(self.larger.text()) if self.larger.text().strip() else 0,
                       max_size=F.parse_size(self.smaller.text()) if self.smaller.text().strip() else 0,
                       newer_than=now - F.parse_age(self.newer.text()) if self.newer.text().strip() else 0,
                       older_than=now - F.parse_age(self.older.text()) if self.older.text().strip() else 0,
                       kind=self.kind.currentData())

    def start(self) -> None:
        if self._running:
            self._stop.set()
            return
        try:
            q = self.query()
        except ValueError as e:
            self.status.setText(str(e))
            return
        root = self.root.text().strip() or self.pane.path
        self.results.clear()
        self._stop.clear()
        self._running = True
        self.go.setText(" Stop")
        self.status.setText("Searching …")
        found = [0]
        last = [0.0]

        def folder(path: str) -> None:
            now = time.monotonic()
            if now - last[0] > 0.2:
                last[0] = now
                on_ui(lambda: self.status.setText(f"Searching … {found[0]} found · {path}"))

        def work() -> None:
            b = None
            try:
                b = own_connection(self.pane)
                for e in F.find(b, root, q, self._stop.is_set, folder):
                    found[0] += 1
                    on_ui(lambda e=e: self._add(e))
                msg = f"{found[0]} found" + (" (stopped)" if self._stop.is_set() else "")
            except Exception as ex:  # noqa: BLE001
                msg = friendly(ex)
            finally:
                if b is not None and not self.pane.session.is_local:
                    _close(b)
            on_ui(lambda: self._done(msg))
        threading.Thread(target=work, daemon=True, name="search").start()

    def _add(self, e) -> None:
        it = QTreeWidgetItem([e.path + ("/" if e.is_dir else ""), "" if e.is_dir else human_size(e.size),
                              human_time(e.mtime)])
        it.setIcon(0, icon("folder" if e.is_dir else "file", C["accent"] if e.is_dir else C["muted"], 16))
        it.setData(0, Qt.UserRole, e)
        it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        self.results.addTopLevelItem(it)

    def _done(self, msg: str) -> None:
        self._running = False
        self.go.setText(" Search")
        self.status.setText(msg)

    def _reveal(self, it) -> None:
        if it is None:
            return
        e = it.data(0, Qt.UserRole)
        b = self.pane.session.backend
        folder = e.path if e.is_dir else (b.parent(e.path) if b else os.path.dirname(e.path))
        self.pane.open_dir(folder, select=e.path)

    def _send(self) -> None:
        entries = [it.data(0, Qt.UserRole) for it in self.results.selectedItems()]
        other = self.pane.other
        if not entries or other is None or not other.path:
            self.status.setText("Select results, and open the target folder in the other list.")
            return
        self.pane.transfer.emit(entries, self.pane, other.path)
        self.status.setText(f"Queued {len(entries)} item(s) to {other.path}")

    def reject(self) -> None:
        self._stop.set()
        super().reject()


# ======================================================================= folder size
def calculate_size(pane, entry, on_result) -> None:
    """Add up a folder on a thread; on_result(text) on the UI thread."""
    stop = threading.Event()

    def work() -> None:
        b = None
        try:
            b = own_connection(pane)
            u = F.folder_size(b, entry.path, stop.is_set)
            text = (f"{entry.name}: {human_size(u.bytes)} in {u.files} file(s), {u.folders} folder(s)"
                    + (f"; {u.unreadable} folder(s) couldn't be read" if u.unreadable else ""))
            on_ui(lambda: on_result(entry, u, text))
        except Exception as ex:  # noqa: BLE001
            on_ui(lambda m=friendly(ex): on_result(entry, None, m))
        finally:
            if b is not None and not pane.session.is_local:
                _close(b)
    threading.Thread(target=work, daemon=True, name="folder-size").start()
    return stop


# ======================================================================= bulk rename
class RenameDialog(_Dialog):
    def __init__(self, pane, entries: list, parent=None):
        super().__init__(parent)
        self.pane = pane
        self.entries = entries
        self.setWindowTitle(f"Rename {len(entries)} items")
        self.resize(820, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        form = QFormLayout()
        fr = QHBoxLayout()
        self.find = QLineEdit(placeholderText="Find")
        self.repl = QLineEdit(placeholderText="Replace with")
        self.regex = QCheckBox("Regular expression")
        self.match_case = QCheckBox("Match case")
        self.match_case.setChecked(True)
        for w in (self.find, self.repl, self.regex, self.match_case):
            fr.addWidget(w)
        form.addRow("Replace", fr)
        cr = QHBoxLayout()
        self.case = QComboBox()
        for label, key in (("Keep", ""), ("lower case", "lower"), ("UPPER CASE", "upper"), ("Title Case", "title")):
            self.case.addItem(label, key)
        self.keep_ext = QCheckBox("Leave extensions alone")
        self.keep_ext.setChecked(True)
        cr.addWidget(self.case)
        cr.addWidget(self.keep_ext)
        cr.addStretch(1)
        form.addRow("Case", cr)
        nr = QHBoxLayout()
        self.template = QLineEdit(placeholderText="e.g. photo-{n:03}{ext}   ({name} = the name, {ext} = .jpg …)")
        self.start = QSpinBox()
        self.start.setRange(0, 1_000_000)
        self.start.setValue(1)
        self.start.setPrefix("from ")
        nr.addWidget(self.template, 1)
        nr.addWidget(self.start)
        form.addRow("Numbering", nr)
        lay.addLayout(form)
        self.preview = QTreeWidget()
        self.preview.setObjectName("Files")
        self.preview.setRootIsDecorated(False)
        self.preview.setHeaderLabels(["Now", "After", ""])
        self.preview.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.preview.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.preview.setAccessibleName("Rename preview")
        lay.addWidget(self.preview, 1)
        bottom = QHBoxLayout()
        self.summary = QLabel("", objectName="Muted")
        bottom.addWidget(self.summary, 1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        self.apply_btn = QPushButton(icon("edit", C["on_accent"]), " Rename", objectName="Primary")
        self.apply_btn.clicked.connect(self.apply)
        bottom.addWidget(self.apply_btn)
        lay.addLayout(bottom)
        for w in (self.find, self.repl, self.template):
            w.textChanged.connect(self.update_preview)
        for w in (self.regex, self.match_case, self.keep_ext):
            w.toggled.connect(self.update_preview)
        self.case.currentIndexChanged.connect(self.update_preview)
        self.start.valueChanged.connect(self.update_preview)
        self.update_preview()

    def rule(self) -> R.Rule:
        return R.Rule(find=self.find.text(), replace=self.repl.text(), regex=self.regex.isChecked(),
                      match_case=self.match_case.isChecked(), case=self.case.currentData(),
                      template=self.template.text().strip(), start=self.start.value(),
                      keep_extension=self.keep_ext.isChecked())

    def update_preview(self) -> None:
        names = [e.name for e in self.entries]
        existing = {e.name for e in self.pane.entries}
        self.items = R.plan(names, self.rule(), existing, windows=self.pane.session.is_local and sys.platform == "win32")
        self.preview.clear()
        ok = 0
        for it in self.items:
            row = QTreeWidgetItem([it.old, it.new, it.problem])
            if it.problem:
                row.setForeground(1, QColor(C["danger"]))
                row.setForeground(2, QColor(C["danger"]))
            elif it.changes:
                ok += 1
            else:
                row.setForeground(1, QColor(C["faint"]))                 # unchanged
            self.preview.addTopLevelItem(row)
        bad = sum(1 for it in self.items if it.problem)
        self.summary.setText(f"{ok} will be renamed" + (f", {bad} can't be (see the reason)" if bad else ""))
        self.apply_btn.setEnabled(ok > 0)

    def apply(self) -> None:
        items = [it for it in self.items if it.changes]
        folder = self.pane.path
        self.apply_btn.setEnabled(False)

        def done(failed) -> None:
            if failed:
                QMessageBox.warning(self, "Rename", "Some names couldn't be changed:\n\n"
                                    + "\n".join(f"{it.old}: {err}" for it, err in failed[:12]))
            self.pane.refresh()
            self.accept()
        self.pane.session.run(lambda b: R.apply(b, folder, items), done,
                              lambda msg: (QMessageBox.warning(self, "Rename", msg), self.apply_btn.setEnabled(True)))


# ======================================================================= compare two files
COMPARE_LIMIT = 5_000_000


def compare_files(win, left_pane, left_entry, right_pane, right_entry) -> None:
    """Fetch both files (each on its own connection) and show the differences."""
    from .editor import DiffDialog
    if max(left_entry.size, right_entry.size) > COMPARE_LIMIT:
        win.show_message(f"Files larger than {human_size(COMPARE_LIMIT)} can't be compared here.", True)
        return
    win.show_message(f"Comparing {left_entry.name} with {right_entry.name} …")

    def fetch(pane, entry):
        b = own_connection(pane)
        try:
            return b.read_bytes(entry.path)
        finally:
            if not pane.session.is_local:
                _close(b)

    def work() -> None:
        try:
            a = fetch(left_pane, left_entry)
            b = fetch(right_pane, right_entry)
        except Exception as ex:  # noqa: BLE001
            on_ui(lambda m=friendly(ex): win.show_message(m, True))
            return
        if b"\0" in a[:8192] or b"\0" in b[:8192]:
            text = "The files are identical." if a == b else "Binary files: they differ."
            on_ui(lambda: (win.show_message(""), QMessageBox.information(win, "Compare", text)))
            return

        def show() -> None:
            win.show_message("")
            DiffDialog(f"{left_entry.name} ↔ {right_entry.name}", b.decode("utf-8", "replace"),
                       a.decode("utf-8", "replace"), win,
                       left=f"{left_pane.title.text()}: {left_entry.path}",
                       right=f"{right_pane.title.text()}: {right_entry.path}").exec()
        on_ui(show)
    threading.Thread(target=work, daemon=True, name="compare").start()


# ======================================================================= copy to another server
class RelayDialog(_Dialog):
    """Pick the destination (a saved site, or another folder on this one) for a server-to-server copy."""

    def __init__(self, win, src_site, entries: list, parent=None):
        super().__init__(parent)
        self.win = win
        self.setWindowTitle("Copy to another server")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        what = entries[0].name if len(entries) == 1 else f"{len(entries)} items"
        lay.addWidget(QLabel(f"Copy <b>{what}</b> from <b>{src_site.label}</b> to:"))
        form = QFormLayout()
        self.site = QComboBox()
        sites = sorted(win.store.sites.values(), key=lambda s: s.label.lower())
        for s in sites:
            self.site.addItem(icon("server", s.color or C["muted"], 16), s.label, s.id)
        if src_site.id in win.store.sites:
            self.site.setCurrentIndex(max(0, self.site.findData(src_site.id)))
        form.addRow("Server", self.site)
        self.path = QLineEdit()
        form.addRow("Folder", self.path)
        lay.addLayout(form)
        hint = QLabel("The data passes through this computer (in a temporary file that is deleted right after).",
                      objectName="Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton(icon("copy", C["on_accent"]), " Copy", objectName="Primary")
        ok.clicked.connect(self._ok)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        self.site.currentIndexChanged.connect(self._site_changed)
        self._site_changed()

    def _site_changed(self) -> None:
        s = self.win.store.sites.get(self.site.currentData())
        tab = next((t for t in self.win.site_tabs() if s and t.remote_session.site.id == s.id), None)
        self.path.setText(tab.remote.path if tab and tab.remote.path else (s.remote_dir if s else "") or "/")

    def _ok(self) -> None:
        if not self.path.text().strip().startswith("/"):
            QMessageBox.warning(self, "Copy", "Enter a folder on the server, starting with /")
            return
        self.accept()

    def destination(self):
        return self.win.store.sites.get(self.site.currentData()), self.path.text().strip()


# ======================================================================= SSH key setup
class KeySetupDialog(_Dialog):
    """Make (or pick) an SSH key, put it on the server, and switch the site to it."""

    def __init__(self, win, site, parent=None):
        super().__init__(parent)
        from ..core import keys as K
        self.win, self.site, self.K = win, site, K
        self.setWindowTitle(f"Key login · {site.label}")
        self.setMinimumWidth(600)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.addWidget(QLabel("Log in with an SSH key instead of a password", objectName="H2"))
        info = QLabel("The public half goes into ~/.ssh/authorized_keys on the server; the private half stays "
                      "on this computer. You need to be able to log in now (password or 2FA) to do this.",
                      objectName="Muted")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.new = QRadioButton("Make a new key")
        self.existing = QRadioButton("Use a key I already have")
        has_key = bool(site.key_path)
        self.existing.setChecked(has_key)
        self.new.setChecked(not has_key)
        lay.addWidget(self.new)
        nf = QFormLayout()
        self.path = QLineEdit(str(K.default_key_path()))
        self.passphrase = QLineEdit(echoMode=QLineEdit.Password, placeholderText="optional; asked when the key is used")
        nf.addRow("Save as", self.path)
        nf.addRow("Passphrase", self.passphrase)
        lay.addLayout(nf)
        lay.addWidget(self.existing)
        ef = QHBoxLayout()
        self.pub = QLineEdit((site.key_path + ".pub") if site.key_path else "", placeholderText="the .pub file")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        ef.addWidget(self.pub, 1)
        ef.addWidget(browse)
        lay.addLayout(ef)
        self.switch = QCheckBox("Use this key for the site from now on")
        self.switch.setChecked(True)
        lay.addWidget(self.switch)
        self.status = QLabel("", objectName="Muted")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        self.go = QPushButton(icon("lock", C["on_accent"]), " Set up", objectName="Primary")
        self.go.clicked.connect(self.run)
        row.addWidget(close)
        row.addWidget(self.go)
        lay.addLayout(row)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Public key", os.path.expanduser("~/.ssh"),
                                              "Public keys (*.pub);;All files (*)")
        if path:
            self.pub.setText(path)
            self.existing.setChecked(True)

    def run(self) -> None:
        K = self.K
        try:
            if self.new.isChecked():
                priv = os.path.expanduser(self.path.text().strip())
                line = K.generate(priv, self.passphrase.text())
                pub_path = priv + ".pub"
            else:
                pub_path = os.path.expanduser(self.pub.text().strip())
                line = K.read_public_key(pub_path)
                priv = pub_path[:-4] if pub_path.endswith(".pub") else ""
        except (OSError, K.KeyError_) as e:
            self.status.setText(f"<span style='color:{C['danger']}'>{e}</span>")
            return
        self.go.setEnabled(False)
        self.status.setText("Adding the key on the server …")
        site = self.site

        def work() -> None:
            b = None
            try:
                tab = next((t for t in self.win.site_tabs() if t.remote_session.site.id == site.id), None)
                b = tab.remote_session.transfer_connector(site) if tab else self.win._connector(site)
                added = K.install(b, line)
                msg = "Added to the server." if added else "That key was already on the server."
                on_ui(lambda: self._done(True, msg, priv))
            except Exception as ex:  # noqa: BLE001
                on_ui(lambda m=friendly(ex): self._done(False, m, priv))
            finally:
                if b is not None:
                    _close(b)
        threading.Thread(target=work, daemon=True, name="copy-id").start()

    def _done(self, ok: bool, msg: str, priv: str) -> None:
        self.go.setEnabled(True)
        color = C["ok"] if ok else C["danger"]
        if ok and self.switch.isChecked() and priv:
            s = self.site.copy()
            s.auth, s.key_path = "key", priv
            if self.new.isChecked():
                s.passphrase = self.passphrase.text()
            self.win.store.upsert(s)
            self.win.reload_sites()
            msg += " The site now logs in with the key."
        self.status.setText(f"<span style='color:{color}'>{msg}</span>")


# ======================================================================= scheduled sync
class ScheduleDialog(_Dialog):
    def __init__(self, win, profile_name: str, current: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Schedule · {profile_name}")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.addWidget(QLabel(f"Run <b>{profile_name}</b> automatically, even with BlamixFiles closed"))
        if current:
            lay.addWidget(QLabel(f"Now: {current}", objectName="Muted"))
        self.off = QRadioButton("Don't run it on a schedule")
        self.daily = QRadioButton("Every day at")
        self.every = QRadioButton("Every")
        self.time = QTimeEdit()
        self.time.setDisplayFormat("HH:mm")
        self.hours = QSpinBox()
        self.hours.setRange(1, 23)
        self.hours.setValue(6)
        self.hours.setSuffix(" hour(s)")
        self.daily.setChecked(True)
        lay.addWidget(self.off)
        r1 = QHBoxLayout()
        r1.addWidget(self.daily)
        r1.addWidget(self.time)
        r1.addStretch(1)
        lay.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(self.every)
        r2.addWidget(self.hours)
        r2.addStretch(1)
        lay.addLayout(r2)
        from .. import keychain
        from ..paths import vault_path
        if not keychain.load(keychain.account_for(vault_path())):
            warn = QLabel("A scheduled sync opens the vault with the password saved in the OS keychain. "
                          "Switch on File → Unlock with … first, or it can't start.")
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color:{C['warn']};")
            lay.addWidget(warn)
        hint = QLabel("Uses Windows Task Scheduler / cron. Results show up in View → Transfer log.",
                      objectName="Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Save", objectName="Primary")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def choice(self):
        """None = off, else a schedule.When."""
        from ..core.schedule import When
        if self.off.isChecked():
            return None
        if self.daily.isChecked():
            return When(daily=self.time.time().toString("HH:mm"))
        return When(every_hours=self.hours.value())


# ======================================================================= after-sync hooks
class HooksDialog(_Dialog):
    def __init__(self, command: str, webhook: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("After sync")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.addWidget(QLabel("When a sync of this profile has finished its transfers", objectName="H2"))
        form = QFormLayout()
        self.command = QLineEdit(command, placeholderText='e.g. ssh web "sudo systemctl reload nginx"')
        self.webhook = QLineEdit(webhook, placeholderText="https://… (gets a JSON summary by POST)")
        form.addRow("Run command", self.command)
        form.addRow("Call webhook", self.webhook)
        lay.addLayout(form)
        hint = QLabel("The command runs on this computer, through the system shell, with BLAMIXFILES_PROFILE, "
                      "BLAMIXFILES_STATUS (ok / failed), BLAMIXFILES_FILES, BLAMIXFILES_BYTES and BLAMIXFILES_FAILED "
                      "set. Both run after scheduled syncs and `blamixfiles sync` too. Saved with the profile.",
                      objectName="Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("OK", objectName="Primary")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)


# ======================================================================= diagnostics
class DiagnosticsDialog(_Dialog):
    def __init__(self, settings: dict, crash_text: str = "", parent=None):
        super().__init__(parent)
        from .. import diagnostics
        self.setWindowTitle("Diagnostics")
        self.resize(860, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        head = ("BlamixFiles closed unexpectedly. This is the report it wrote:" if crash_text
                else "For a problem report: copy this and paste it into the issue.")
        lay.addWidget(QLabel(head, objectName="H2"))
        note = QLabel("Nothing is sent anywhere. It has no passwords or keys, but it does show folder paths "
                      "and site names: look it over before you share it.", objectName="Muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.view = QPlainTextEdit(readOnly=True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setPlainText(crash_text or diagnostics.report(settings))
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        copy = QPushButton(icon("copy"), " Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.view.toPlainText()))
        save = QPushButton(icon("save"), " Save as…")
        save.clicked.connect(self._save)
        folder = QPushButton(icon("folder-open"), " Crash reports folder")
        folder.clicked.connect(self._open_folder)
        issue = QPushButton(icon("github"), " Report a problem")
        issue.clicked.connect(self._issue)
        close = QPushButton("Close", objectName="Primary")
        close.clicked.connect(self.accept)
        for w in (copy, save, folder, issue):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save diagnostics", os.path.expanduser("~/blamixfiles-diagnostics.txt"),
                                              "Text (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.view.toPlainText())

    def _open_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from .. import diagnostics
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(diagnostics.crash_dir())))

    def _issue(self) -> None:
        import webbrowser

        from ..links import ISSUES_URL
        QApplication.clipboard().setText(self.view.toPlainText())
        webbrowser.open(ISSUES_URL + "/new")
