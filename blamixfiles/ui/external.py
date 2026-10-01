"""'Edit in another app': download, open in VS Code / Notepad++ / the default app,
upload again on every save (with a check that nobody changed the server copy)."""
from __future__ import annotations

import os
import subprocess
import sys

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QFileDialog, QMessageBox

from ..core.external_edit import ExternalEdit, ExternalEdits
from ..paths import data_dir
from .bridge import on_ui


def launch(program: str, path: str) -> None:
    """Open `path` in `program` ("" = whatever the system opens that file type with)."""
    if not program:
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            raise OSError(f"No app is set up to open {os.path.basename(path)}")
        return
    if sys.platform == "darwin" and program.endswith(".app"):
        subprocess.Popen(["open", "-a", program, path])
    else:
        subprocess.Popen([program, path], close_fds=True)


class ExternalEditor(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.edits = ExternalEdits(data_dir() / "external-edit")
        self.edits.clean_old()
        self.timer = QTimer(self, interval=1000)
        self.timer.timeout.connect(self._poll)
        self._busy: set[int] = set()           # edits with a question or an upload in progress

    # ------------------------------------------------------------ settings
    @property
    def program(self) -> str:
        return self.win.settings["external_editor"] or ""

    def choose_program(self) -> bool:
        start = os.environ.get("ProgramFiles", "/Applications" if sys.platform == "darwin" else "/usr/bin")
        flt = "Programs (*.exe)" if sys.platform == "win32" else "All files (*)"
        path, _ = QFileDialog.getOpenFileName(self.win, "Program to edit files with", start, flt)
        if not path:
            return False
        self.win.settings["external_editor"] = path
        self.win.settings.save()
        self.win.show_message(f"Files will open in {os.path.basename(path)}")
        return True

    def use_default_app(self) -> None:
        self.win.settings["external_editor"] = ""
        self.win.settings.save()
        self.win.show_message("Files will open in the system's default app")

    # ------------------------------------------------------------ open
    def open(self, session, entry) -> None:
        if session.is_local:
            try:
                launch(self.program, entry.path)
            except OSError as e:
                self.win.show_message(str(e), True)
            return
        site = session.site
        existing = self.edits.find(site.id, entry.path)
        if existing is not None and os.path.exists(existing.local_path):
            self._launch(existing)                # already open: don't overwrite their edits
            return
        local = self.edits.local_path(site.id, entry.path)

        def fetch(b):
            st = b.stat(entry.path)
            if st is None:
                raise FileNotFoundError(2, "File not found", entry.path)
            tmp = local + ".part"
            with open(tmp, "wb") as f:
                b.download(entry.path, f)
            os.replace(tmp, local)
            if st.mtime:
                os.utime(local, (st.mtime, st.mtime))
            return st
        session.log(f"Downloading {entry.name} to edit it in another app …")

        def done(st):
            edit = self.edits.add(ExternalEdit(site=site, session=session, remote_path=entry.path,
                                               local_path=local, remote_mtime=st.mtime, remote_size=st.size))
            self._launch(edit)
            self.timer.start()
        session.run(fetch, done, lambda msg: self.win.show_message(f"Couldn't open {entry.name}: {msg}", True))

    def _launch(self, edit: ExternalEdit) -> None:
        try:
            launch(self.program, edit.local_path)
        except OSError as e:
            self.win.show_message(f"{e}. Choose a program: right-click → Edit in another app → Choose program…", True)
            return
        who = os.path.basename(self.program) if self.program else "the default app"
        edit.session.log(f"Editing {edit.remote_path} in {who}: saves there are uploaded back here")
        self.win.show_message(f"{edit.name} is open in {who}. Save it there to upload it again.")
        self._update_status()

    # ------------------------------------------------------------ watching
    def _poll(self) -> None:
        if not self.edits.edits:
            self.timer.stop()
            return
        for edit in self.edits.poll():
            if id(edit) in self._busy:
                continue
            self._offer(edit)

    def _offer(self, edit: ExternalEdit) -> None:
        site = edit.site
        must_ask = edit.ask or getattr(site, "production", False)
        if must_ask:
            self._busy.add(id(edit))
            box = QMessageBox(QMessageBox.Question, "Upload changes?",
                              f"<b>{edit.name}</b> was saved in the other app.<br>"
                              f"Upload it to <b>{site.label}</b>:{edit.remote_path}?"
                              + ("<br><br><b style='color:#ff6b6b'>This is a production site.</b>"
                                 if getattr(site, "production", False) else ""),
                              QMessageBox.Yes | QMessageBox.No, self.win)
            box.button(QMessageBox.Yes).setText("Upload")
            box.button(QMessageBox.No).setText("Not now")
            cb = None
            if not getattr(site, "production", False):
                cb = QCheckBox("Upload this file's next saves without asking")
                box.setCheckBox(cb)
            answer = box.exec()
            self._busy.discard(id(edit))
            if edit not in self.edits.edits:
                return
            if answer != QMessageBox.Yes:
                self.edits.accepted(edit)           # until the next save
                return
            if cb is not None and cb.isChecked():
                edit.ask = False
        self._upload(edit)

    def _upload(self, edit: ExternalEdit, force: bool = False) -> None:
        self._busy.add(id(edit))
        local, remote = edit.local_path, edit.remote_path
        try:
            data = open(local, "rb").read()
        except OSError as e:
            self._busy.discard(id(edit))
            self.win.show_message(f"Can't read {local}: {e}", True)
            return
        self.edits.accepted(edit)                    # this version; a new save is offered again

        def push(b):
            st = b.stat(remote)
            if st is not None and not force and self.edits.remote_changed(edit, st.mtime, st.size):
                return "conflict", st
            b.write_bytes(remote, data)
            return "ok", b.stat(remote)

        def done(result):
            self._busy.discard(id(edit))
            what, st = result
            if what == "conflict":
                self._conflict(edit, st)
                return
            self.edits.uploaded(edit, st.mtime if st else 0.0, st.size if st else len(data))
            edit.session.log(f"Uploaded {remote} (edited in another app)")
            self.win.show_message(f"Uploaded {edit.name} to {edit.site.label}")
            self.win._refresh_panes_of(edit.session)
            self._update_status()

        def failed(msg):
            self._busy.discard(id(edit))
            edit._sig = (0, 0)                        # offer it again on the next poll
            self.win.show_message(f"Upload of {edit.name} failed: {msg}", True)
        edit.session.run(push, done, failed)

    def _conflict(self, edit: ExternalEdit, st) -> None:
        box = QMessageBox(QMessageBox.Warning, "Changed on the server",
                          f"<b>{edit.remote_path}</b> was changed on {edit.site.label} after you opened it "
                          "(by someone else, or another app).<br><br>"
                          "Overwrite it with your version, or keep the server's version?",
                          QMessageBox.Yes | QMessageBox.Cancel, self.win)
        box.button(QMessageBox.Yes).setText("Overwrite")
        box.button(QMessageBox.Cancel).setText("Keep server version")
        if box.exec() == QMessageBox.Yes:
            self._upload(edit, force=True)
        else:
            edit.session.log(f"Not uploaded: {edit.remote_path} changed on the server", True)
            self.edits.uploaded(edit, st.mtime, st.size)   # their version is now the base

    # ------------------------------------------------------------ bookkeeping
    def stop(self, edit: ExternalEdit) -> None:
        # the file stays (the other app may still have it open); old copies are cleaned after a week
        self.edits.remove(edit, delete_file=False)
        self._update_status()

    def forget_session(self, session) -> None:
        gone = self.edits.forget_session(session)
        if gone:
            self.win.show_message(f"{len(gone)} file(s) open in other apps won't be uploaded any more "
                                  "(the site tab was closed)", True)
        self._update_status()

    def _update_status(self) -> None:
        n = len(self.edits.edits)
        on_ui(lambda: self.win.external_status(n))
