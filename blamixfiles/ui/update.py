"""Updates in the app: a quiet daily check, an "Update 1.2.3" button in the status bar,
release notes with Install / Release page / Skip, and "Install update from file…" for
computers without internet. The work itself is in blamixfiles/updater.py."""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
                               QPushButton, QTextBrowser, QVBoxLayout)

from .. import __version__, updater
from .bridge import on_ui
from .theme import C, icon, style_window

CHECK_EVERY = 20 * 3600          # seconds between automatic checks


class UpdateDialog(QDialog):
    """Release notes; Install (download + apply), release page, skip, later."""

    def __init__(self, release, current: str, can_install: bool, parent=None):
        super().__init__(parent)
        self.choice = "later"
        self.setWindowTitle("Update available")
        self.setMinimumSize(640, 440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        lay.addWidget(QLabel(f"BlamixFiles {release.version} is available", objectName="H2"))
        lay.addWidget(QLabel(f"You have {current}.", objectName="Muted"))
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(release.notes or "_No release notes._")
        lay.addWidget(notes, 1)
        if not can_install:
            hint = QLabel("This copy can't update itself: use the release page to download the new version.",
                          objectName="Hint")
            hint.setWordWrap(True)
            lay.addWidget(hint)
        row = QHBoxLayout()
        skip = QPushButton("Skip this version")
        skip.clicked.connect(lambda: self._done("skip"))
        page = QPushButton(icon("link"), " Release page")
        page.clicked.connect(lambda: self._done("page"))
        later = QPushButton("Later")
        later.clicked.connect(lambda: self._done("later"))
        row.addWidget(skip)
        row.addStretch(1)
        row.addWidget(later)
        row.addWidget(page)
        if can_install:
            install = QPushButton(icon("download", C["on_accent"]), " Install && restart", objectName="Primary")
            install.setDefault(True)
            install.clicked.connect(lambda: self._done("install"))
            row.addWidget(install)
        lay.addLayout(row)

    def _done(self, choice: str) -> None:
        self.choice = choice
        self.accept()

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        style_window(self)


class Updates(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.release = None
        self.button = QPushButton(objectName="Primary")
        self.button.setStyleSheet("padding: 1px 10px; border-radius: 7px; font-size: 8.5pt;")
        self.button.hide()
        self.button.clicked.connect(lambda: self.show_update(self.release))
        win.statusBar().addPermanentWidget(self.button)

    def start(self) -> None:
        """Quiet background check a few seconds after start (at most daily)."""
        QTimer.singleShot(4000, lambda: self.check(manual=False))

    # ------------------------------------------------------------ check
    def check(self, manual: bool = False) -> None:
        s = self.win.settings
        if updater.update_check_policy() is False:
            if manual:
                QMessageBox.information(self.win, "Updates", "Update checks are turned off by your administrator. "
                                        "To update, use Help → Install update from file.")
            return
        if not manual:
            if not updater.updates_allowed(s):
                return
            if time.time() - float(s["last_update_check"] or 0) < CHECK_EVERY:
                return
        else:
            self.win.show_message("Checking for updates …")

        def run():
            try:
                rel = updater.check()
            except updater.UpdateError as e:
                on_ui(lambda m=str(e): self._checked(manual, None, m))
                return
            on_ui(lambda: self._checked(manual, rel, ""))
        threading.Thread(target=run, daemon=True).start()

    def _checked(self, manual: bool, rel, err: str) -> None:
        s = self.win.settings
        if manual:
            self.win.show_message("")
        if err:
            if manual:
                QMessageBox.warning(self.win, "Updates", err)
            return
        s["last_update_check"] = time.time()
        s.save()
        if not rel:
            self.button.hide()
            if manual:
                QMessageBox.information(self.win, "Updates", f"You're up to date (BlamixFiles {__version__}).")
            return
        self.release = rel
        self.button.setText(f"⬆  Update {rel.version}")
        if manual:
            self.button.show()
            self.show_update(rel)
        elif rel.version != s["skip_version"]:
            self.button.show()
            self.win.show_message(f"BlamixFiles {rel.version} is available: click the button on the right.")

    # ------------------------------------------------------------ install
    def show_update(self, rel) -> None:
        if not rel:
            return
        asset = updater.pick_asset(rel)
        dlg = UpdateDialog(rel, __version__, can_install=asset is not None, parent=self.win)
        dlg.exec()
        if dlg.choice == "skip":
            self.win.settings["skip_version"] = rel.version
            self.win.settings.save()
            self.button.hide()
        elif dlg.choice == "page":
            QDesktopServices.openUrl(QUrl(rel.page))
        elif dlg.choice == "install" and asset and self._confirm_quit("install the update"):
            self._download(asset)

    def _confirm_quit(self, what: str) -> bool:
        win = self.win
        parts = []
        dirty = win.unsaved_editors()
        if dirty:
            parts.append(f"{len(dirty)} unsaved file(s)")
        busy = win.engine.pending()
        if busy:
            parts.append(f"{busy} transfer(s) not finished (saved sites resume next time)")
        msg = f"BlamixFiles will close to {what} and start again."
        if parts:
            msg += "\n\n" + " and ".join(parts).capitalize() + " will be lost or paused."
        return QMessageBox.question(win, "Install update?", msg + "\n\nContinue?") == QMessageBox.Yes

    def _download(self, asset) -> None:
        self.button.setEnabled(False)
        self.button.show()
        self.button.setText("Downloading update …")

        def progress(done: int, total: int) -> None:
            if total:
                on_ui(lambda: self.button.setText(f"Downloading update … {done * 100 // total}%"))

        def run():
            try:
                path = updater.download(asset, Path(tempfile.gettempdir()) / "blamixfiles-update", progress)
                on_ui(lambda: self._downloaded(str(path), ""))
            except updater.UpdateError as e:
                on_ui(lambda m=str(e): self._downloaded("", m))
        threading.Thread(target=run, daemon=True).start()

    def _downloaded(self, path: str, err: str) -> None:
        self.button.setEnabled(True)
        if err:
            self.button.setText(f"⬆  Update {self.release.version}")
            QMessageBox.warning(self.win, "Update failed", err)
            return
        self._apply(Path(path))

    def _apply(self, path: Path, kind: str | None = None) -> None:
        try:
            updater.apply_update(path, kind)
        except updater.UpdateError as e:
            QMessageBox.warning(self.win, "Update failed", str(e))
            return
        self.win.force_quit = True
        self.win.close()          # saves settings, tabs and the queue (closeEvent)
        # The update script waits for this process to end. Don't rely on a clean Qt
        # shutdown (an open dialog or a hung thread can keep the process alive).
        os._exit(0)

    # ------------------------------------------------------------ from a file
    def from_file(self) -> None:
        """Offline update: install an MSI / portable zip that was copied to this computer."""
        win = self.win
        kind = updater.install_kind()
        if kind not in ("msi", "portable"):
            QMessageBox.information(win, "Install update from file", updater.check_update_file(Path("x"), kind))
            return
        filt = "BlamixFiles installer (*.msi)" if kind == "msi" else "BlamixFiles portable (*.zip)"
        path, _ = QFileDialog.getOpenFileName(win, "Install update from file", str(Path.home() / "Downloads"), filt)
        if not path:
            return
        problem = updater.check_update_file(Path(path), kind)
        if problem:
            QMessageBox.warning(win, "Install update from file", problem)
            return
        digest = updater.sha256_of(Path(path))
        ver = updater.file_version(Path(path))
        if ver and not updater.is_newer(ver, __version__) and QMessageBox.question(
                win, "Install update from file",
                f"{Path(path).name} is version {ver}; you have {__version__}. Install it anyway?") != QMessageBox.Yes:
            return
        expected, ok = QInputDialog.getText(
            win, "Check the file",
            f"<b>{Path(path).name}</b>{f' (version {ver})' if ver else ''}<br><br>"
            f"SHA-256: <code>{digest}</code><br><br>"
            "Compare it with the checksum on the release page (each download lists its sha256). "
            "Paste the expected value to check automatically, or leave empty:")
        if not ok:
            return
        problem = updater.check_update_file(Path(path), kind, expected)
        if problem:
            QMessageBox.warning(win, "Install update from file", problem)
            return
        if self._confirm_quit("install the update"):
            self._apply(Path(path), kind)
