"""Dialogs: vault unlock, sign-in prompts, host keys / certificates, site editor,
"file exists" question, permissions."""
from __future__ import annotations

import stat as statmod
import threading
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..core.backends import DEFAULT_PORTS, PROTOCOLS
from ..models import COLORS, Site
from .theme import C, icon, style_window


class _Base(QDialog):
    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        style_window(self)


# ======================================================================= unlock
class UnlockDialog(_Base):
    def __init__(self, create: bool, attempt, parent=None):
        """attempt(password) -> str error or '' on success."""
        super().__init__(parent)
        self.create = create
        self._attempt = attempt
        self.setWindowTitle("BlamixFiles")
        self.setFixedWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 30, 32, 26)
        lay.setSpacing(10)

        badge = QLabel()
        badge.setPixmap(icon("lock", C["accent"], 34).pixmap(34, 34))
        lay.addWidget(badge)
        h = QLabel("Create your vault" if create else "Unlock BlamixFiles", objectName="H1")
        h.setStyleSheet("font-size:17pt; font-weight:600;")
        lay.addWidget(h)
        sub = QLabel(
            "Sites, passwords and keys are encrypted with AES-256-GCM using a key derived from "
            "this master password. There's no way to recover it if you forget it."
            if create else "Enter your master password to decrypt your sites.")
        sub.setWordWrap(True)
        sub.setObjectName("Muted")
        lay.addWidget(sub)
        lay.addSpacing(8)

        self.pw = QLineEdit(echoMode=QLineEdit.Password, placeholderText="Master password")
        lay.addWidget(self.pw)
        self.pw2 = QLineEdit(echoMode=QLineEdit.Password, placeholderText="Confirm password")
        self.pw2.setVisible(create)
        lay.addWidget(self.pw2)
        self.err = QLabel()
        self.err.setStyleSheet(f"color:{C['danger']};")
        self.err.setWordWrap(True)
        self.err.hide()
        lay.addWidget(self.err)

        self.btn = QPushButton("Create vault" if create else "Unlock", objectName="Primary")
        self.btn.setDefault(True)
        self.btn.clicked.connect(self._go)
        lay.addSpacing(6)
        lay.addWidget(self.btn)
        self.pw.returnPressed.connect(self._go if not create else self.pw2.setFocus)
        self.pw2.returnPressed.connect(self._go)

    def _go(self) -> None:
        p = self.pw.text()
        if self.create:
            if len(p) < 8:
                return self._fail("Use at least 8 characters.")
            if p != self.pw2.text():
                return self._fail("Passwords don't match.")
        self.btn.setEnabled(False)
        self.btn.setText("Deriving key …")
        self.repaint()
        err = self._attempt(p)
        self.btn.setEnabled(True)
        self.btn.setText("Create vault" if self.create else "Unlock")
        if err:
            self._fail(err)
            self.pw.selectAll()
            self.pw.setFocus()
        else:
            self.accept()

    def _fail(self, msg: str) -> None:
        self.err.setText(msg)
        self.err.show()




class AuthPromptDialog(_Base):
    """Keyboard-interactive prompts from the server (verification code, OTP, …)."""

    def __init__(self, server_label: str, title: str, instructions: str,
                 prompts: list[tuple[str, bool]], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Sign in to {server_label}")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)
        head = QHBoxLayout()
        badge = QLabel()
        badge.setPixmap(icon("shield", C["accent"], 28).pixmap(28, 28))
        head.addWidget(badge)
        h = QLabel(title.strip() or "Verification required", objectName="H2")
        head.addWidget(h, 1)
        lay.addLayout(head)
        sub = QLabel(instructions.strip() or f"{server_label} asks for more information to sign you in.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        lay.addWidget(sub)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.fields: list[QLineEdit] = []
        for text, echo in prompts:
            ed = QLineEdit(echoMode=QLineEdit.Normal if echo else QLineEdit.Password)
            low = text.lower()
            if any(w in low for w in ("code", "otp", "token", "verification", "passcode")):
                ed.setPlaceholderText("123456")
                ed.setInputMethodHints(Qt.ImhDigitsOnly)
            form.addRow(text.strip().rstrip(":") or "Answer", ed)
            self.fields.append(ed)
            ed.returnPressed.connect(self._next)
        lay.addLayout(form)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Sign in", objectName="Primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        if self.fields:
            self.fields[0].setFocus()

    def _next(self) -> None:
        idx = self.fields.index(self.sender())
        if idx + 1 < len(self.fields):
            self.fields[idx + 1].setFocus()
        else:
            self.accept()

    def answers(self) -> list[str]:
        return [f.text() for f in self.fields]




# ======================================================================= trust questions
def ask_host_key(parent, host_id: str, key_type: str, fp: str, changed: bool) -> bool:
    if changed:
        box = QMessageBox(QMessageBox.Warning, "Host key CHANGED",
                          f"<b>The host key for {host_id} has changed.</b><br><br>"
                          "This happens after a server reinstall, but it can also mean someone is "
                          "intercepting the connection. Only continue if you know why it changed."
                          f"<br><br>New {key_type} key:<br><code>{fp}</code>", parent=parent)
        box.addButton("Cancel", QMessageBox.RejectRole)
        yes = box.addButton("Replace key and connect", QMessageBox.DestructiveRole)
    else:
        box = QMessageBox(QMessageBox.Question, "New server",
                          f"First connection to <b>{host_id}</b>.<br><br>"
                          f"Its {key_type} key fingerprint is:<br><code>{fp}</code><br><br>"
                          "Trust this server?", parent=parent)
        box.addButton("Cancel", QMessageBox.RejectRole)
        yes = box.addButton("Trust and connect", QMessageBox.AcceptRole)
    box.exec()
    return box.clickedButton() is yes


def ask_certificate(parent, host: str, fp: str, reason: str) -> bool:
    box = QMessageBox(QMessageBox.Warning, "Untrusted certificate",
                      f"The TLS certificate of <b>{host}</b> isn't trusted:<br><i>{reason}</i><br><br>"
                      "Self-signed certificates are common on small servers. If you trust this one, "
                      "BlamixFiles remembers its fingerprint and warns you if it ever changes."
                      f"<br><br>SHA-256:<br><code>{fp}</code>", parent=parent)
    box.addButton("Cancel", QMessageBox.RejectRole)
    yes = box.addButton("Trust this certificate", QMessageBox.AcceptRole)
    box.exec()
    return box.clickedButton() is yes


def ask_password(parent, site: Site) -> str | None:
    dlg = _Base(parent)
    dlg.setWindowTitle(f"Sign in to {site.label}")
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(24, 20, 24, 18)
    lay.addWidget(QLabel(f"Password for <b>{site.username or 'anonymous'}</b> at {site.host}"))
    ed = QLineEdit(echoMode=QLineEdit.Password)
    lay.addWidget(ed)
    row = QHBoxLayout()
    row.addStretch(1)
    cancel = QPushButton("Cancel")
    ok = QPushButton("Connect", objectName="Primary")
    ok.setDefault(True)
    cancel.clicked.connect(dlg.reject)
    ok.clicked.connect(dlg.accept)
    ed.returnPressed.connect(dlg.accept)
    row.addWidget(cancel)
    row.addWidget(ok)
    lay.addLayout(row)
    return ed.text() if dlg.exec() == QDialog.Accepted else None


# ======================================================================= file exists
def _fmt_when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "?"


class OverwriteDialog(_Base):
    """Target exists. Returns a policy ("overwrite", "resume", "skip", "newer"),
    optionally with "-all", or "cancel"."""

    def __init__(self, name: str, src_size: int, src_mtime: float, dst_size: int, dst_mtime: float,
                 parent=None):
        super().__init__(parent)
        from .fmt import human_size
        self.setWindowTitle("File exists")
        self.result_policy = "cancel"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)
        lay.addWidget(QLabel(f"<b>{name}</b> already exists.", objectName="H2"))
        info = QLabel(f"Source: {human_size(src_size)}, modified {_fmt_when(src_mtime)}<br>"
                      f"Target: {human_size(dst_size)}, modified {_fmt_when(dst_mtime)}")
        info.setObjectName("Muted")
        lay.addWidget(info)
        self.all = QCheckBox("Do this for all remaining files")
        lay.addWidget(self.all)
        row = QHBoxLayout()
        for label, policy in (("Overwrite", "overwrite"), ("If newer", "newer"),
                              ("Resume", "resume"), ("Skip", "skip")):
            b = QPushButton(label, objectName="Primary" if policy == "overwrite" else "")
            b.clicked.connect(lambda _=False, p=policy: self._pick(p))
            row.addWidget(b)
        if not (0 < dst_size < src_size):
            row.itemAt(2).widget().setEnabled(False)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _pick(self, policy: str) -> None:
        self.result_policy = policy + ("-all" if self.all.isChecked() else "")
        self.accept()


# ======================================================================= permissions
class ChmodDialog(_Base):
    def __init__(self, name: str, mode: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Permissions of {name}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        self.boxes: dict[int, QCheckBox] = {}
        grid = QFormLayout()
        bits = statmod.S_IMODE(mode or 0)
        for who, shift in (("Owner", 6), ("Group", 3), ("Others", 0)):
            w = QWidget()
            r = QHBoxLayout(w)
            r.setContentsMargins(0, 0, 0, 0)
            for label, b in (("read", 4), ("write", 2), ("execute", 1)):
                cb = QCheckBox(label)
                cb.setChecked(bool(bits & (b << shift)))
                cb.toggled.connect(self._sync_octal)
                self.boxes[b << shift] = cb
                r.addWidget(cb)
            grid.addRow(who, w)
        self.octal = QLineEdit(f"{bits:03o}")
        self.octal.setMaxLength(4)
        self.octal.textEdited.connect(self._sync_boxes)
        grid.addRow("Numeric", self.octal)
        self.recursive = QCheckBox("Apply to everything inside (folders)")
        lay.addLayout(grid)
        lay.addWidget(self.recursive)
        self._special = bits & 0o7000
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        ok = QPushButton("Apply", objectName="Primary")
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def mode(self) -> int:
        return self._special | sum(bit for bit, cb in self.boxes.items() if cb.isChecked())

    def _sync_octal(self) -> None:
        self.octal.setText(f"{self.mode():03o}")

    def _sync_boxes(self, text: str) -> None:
        try:
            v = int(text, 8)
        except ValueError:
            return
        self._special = v & 0o7000
        for bit, cb in self.boxes.items():
            cb.blockSignals(True)
            cb.setChecked(bool(v & bit))
            cb.blockSignals(False)


# ======================================================================= site editor
class _Tester(QObject):
    done = Signal(str)


class SiteDialog(_Base):
    def __init__(self, site: Site | None, groups: list[str], parent=None, sites: list | None = None):
        super().__init__(parent)
        self.site = site.copy() if site else Site()
        self.all_sites = sites or []
        self.setWindowTitle("Edit site" if site else "New site")
        self.setMinimumWidth(600)
        s = self.site
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        form = QFormLayout()
        form.setVerticalSpacing(9)
        self.name = QLineEdit(s.name, placeholderText="My web server")
        self.proto = QComboBox()
        for k, v in PROTOCOLS.items():
            self.proto.addItem(v, k)
        self.proto.setCurrentIndex(max(0, self.proto.findData(s.protocol)))
        self.host = QLineEdit(s.host, placeholderText="example.com or 10.0.0.5")
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText("default")
        self.port.setValue(s.port)
        self.port.setFixedWidth(120)
        self.hp_widget = QWidget()
        hp = QHBoxLayout(self.hp_widget)
        hp.setContentsMargins(0, 0, 0, 0)
        hp.addWidget(self.host, 1)
        hp.addWidget(QLabel("Port"))
        hp.addWidget(self.port)
        self.user = QLineEdit(s.username)
        self.auth = QComboBox()
        self.password = QLineEdit(s.password, echoMode=QLineEdit.Password)
        self.key_path = QLineEdit(s.key_path, placeholderText="~/.ssh/id_ed25519")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_key)
        kp = QHBoxLayout()
        kp.addWidget(self.key_path, 1)
        kp.addWidget(browse)
        self.key_row = QWidget()
        self.key_row.setLayout(kp)
        kp.setContentsMargins(0, 0, 0, 0)
        self.passphrase = QLineEdit(s.passphrase, echoMode=QLineEdit.Password,
                                    placeholderText="only if the key is encrypted")
        self.remote_dir = QLineEdit(s.remote_dir, placeholderText="login folder")
        self.local_dir = QLineEdit(s.local_dir, placeholderText="home folder")
        self.group = QComboBox(editable=True)
        self.group.addItems([""] + groups)
        self.group.setCurrentText(s.group)
        self.color = QComboBox()
        for col in COLORS:
            self.color.addItem("none" if not col else col, col)
        self.color.setCurrentIndex(max(0, self.color.findData(s.color)))
        self.production = QCheckBox("Production: red tab, extra confirmation before deleting")
        self.production.setChecked(s.production)
        self.passive = QCheckBox("Passive mode (recommended)")
        self.passive.setChecked(s.ftp_passive)
        self.region = QLineEdit(s.s3_region, placeholderText="e.g. eu-central-1 (empty = provider default)")
        self.jump = QComboBox()
        self.jump.addItem("none (connect directly)", "")
        for other in sorted(self.all_sites, key=lambda x: x.label.lower()):
            if other.id != s.id and other.is_ssh:
                self.jump.addItem(f"{other.label}  ({other.username + '@' if other.username else ''}{other.host})",
                                  other.id)
        self.jump.setCurrentIndex(max(0, self.jump.findData(s.jump_id)))
        self.jump.setToolTip("Reach this server through another SSH site (a bastion / ProxyJump).")
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 10)
        self.parallel.setValue(s.parallel)
        self.notes = QPlainTextEdit(s.notes)
        self.notes.setFixedHeight(60)

        form.addRow("Name", self.name)
        form.addRow("Protocol", self.proto)
        form.addRow("Host", self.hp_widget)
        form.addRow("Region", self.region)
        form.addRow("Jump host", self.jump)
        form.addRow("Username", self.user)
        form.addRow("Login", self.auth)
        form.addRow("Password", self.password)
        form.addRow("Private key", self.key_row)
        form.addRow("Key passphrase", self.passphrase)
        form.addRow("Remote folder", self.remote_dir)
        form.addRow("Local folder", self.local_dir)
        form.addRow("Group", self.group)
        form.addRow("Color", self.color)
        form.addRow("", self.production)
        form.addRow("FTP", self.passive)
        form.addRow("Parallel transfers", self.parallel)
        form.addRow("Notes", self.notes)
        self.form = form
        lay.addLayout(form)

        self.test_msg = QLabel()
        self.test_msg.setWordWrap(True)
        self.test_msg.setObjectName("Muted")
        lay.addWidget(self.test_msg)
        row = QHBoxLayout()
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test)
        row.addWidget(self.test_btn)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        save = QPushButton("Save", objectName="Primary")
        save.setDefault(True)
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._save)
        row.addWidget(cancel)
        row.addWidget(save)
        lay.addLayout(row)

        self.proto.currentIndexChanged.connect(self._proto_changed)
        self.auth.currentIndexChanged.connect(self._auth_changed)
        self._proto_changed(keep_auth=s.auth)
        self._tester = _Tester()
        self._tester.done.connect(self._tested)

    def _label(self, field, text: str) -> None:
        lbl = self.form.labelForField(field)
        if lbl is not None:
            lbl.setText(text)

    def _proto_changed(self, *_a, keep_auth: str | None = None) -> None:
        proto = self.proto.currentData()
        ssh_ = proto in ("sftp", "scp")
        s3 = proto == "s3"
        dav = proto in ("webdav", "webdavs")
        cur = keep_auth or self.auth.currentData()
        self.auth.blockSignals(True)
        self.auth.clear()
        if ssh_:
            opts = [("Password", "password"), ("Private key", "key"), ("SSH agent / default keys", "agent"),
                    ("Ask for the password each time", "ask")]
        elif s3 or dav:
            opts = [("Keys / password", "password"), ("Ask each time", "ask")]
        else:
            opts = [("Password", "password"), ("Anonymous", "anonymous"), ("Ask for the password each time", "ask")]
        for label, val in opts:
            self.auth.addItem(label, val)
        self.auth.setCurrentIndex(max(0, self.auth.findData(cur)))
        self.auth.blockSignals(False)
        self.port.setSpecialValueText(f"default ({DEFAULT_PORTS.get(proto, 22)})")
        self.form.setRowVisible(self.passive, proto.startswith("ftp"))
        self.form.setRowVisible(self.region, s3)
        self.form.setRowVisible(self.jump, ssh_ and self.jump.count() > 1)
        self._label(self.hp_widget, "Endpoint" if s3 else "Host")
        self._label(self.user, "Access key" if s3 else "Username")
        self._label(self.password, "Secret key" if s3 else ("App password" if dav else "Password"))
        self._label(self.remote_dir, "Bucket / folder" if s3 else ("WebDAV path" if dav else "Remote folder"))
        self.host.setPlaceholderText(
            "s3.amazonaws.com, s3.eu-central-003.backblazeb2.com, http://minio.lan:9000 …" if s3 else
            "cloud.example.com" if dav else "example.com or 10.0.0.5")
        self.remote_dir.setPlaceholderText(
            "my-bucket/backups (empty = list all buckets)" if s3 else
            "Nextcloud: /remote.php/dav/files/USERNAME" if dav else "login folder")
        self._auth_changed()

    def _auth_changed(self, *_a) -> None:
        a = self.auth.currentData()
        self.form.setRowVisible(self.password, a == "password")
        self.form.setRowVisible(self.key_row, a == "key")
        self.form.setRowVisible(self.passphrase, a == "key")
        self.form.setRowVisible(self.user, a != "anonymous")

    def _browse_key(self) -> None:
        import os
        path, _ = QFileDialog.getOpenFileName(self, "Private key", os.path.expanduser("~/.ssh"))
        if path:
            self.key_path.setText(path)

    def _collect(self) -> Site:
        s = self.site.copy()
        s.name = self.name.text().strip()
        s.protocol = self.proto.currentData()
        s.host = self.host.text().strip()
        s.port = self.port.value()
        s.username = self.user.text().strip()
        s.auth = self.auth.currentData()
        s.password = self.password.text() if s.auth == "password" else ""
        s.key_path = self.key_path.text().strip()
        s.passphrase = self.passphrase.text()
        s.remote_dir = self.remote_dir.text().strip()
        s.local_dir = self.local_dir.text().strip()
        s.group = self.group.currentText().strip("/ ")
        s.color = self.color.currentData() or ""
        s.production = self.production.isChecked()
        s.ftp_passive = self.passive.isChecked()
        s.s3_region = self.region.text().strip()
        s.jump_id = (self.jump.currentData() or "") if s.is_ssh else ""
        s.parallel = self.parallel.value()
        s.notes = self.notes.toPlainText()
        return s

    def _save(self) -> None:
        s = self._collect()
        if not s.host:
            self.test_msg.setText("Enter a host name.")
            return
        self.site = s
        self.accept()

    def _test(self) -> None:
        s = self._collect()
        if not s.host:
            self.test_msg.setText("Enter a host name.")
            return
        self.test_btn.setEnabled(False)
        self.test_msg.setText("Connecting …")

        def run():
            from ..core import ssh
            from ..core.backends import make_backend
            from ..core.backends.ftp import UntrustedCertificate
            from ..core.errors import friendly
            try:
                if s.is_ssh and s.protocol == "sftp":
                    msg = ssh.test_connection(s)
                else:
                    b = make_backend(s)
                    try:
                        b.connect()
                        b.close()
                        msg = ""
                    except UntrustedCertificate as e:
                        msg = f"OK. Self-signed certificate ({e.reason}); you'll be asked to trust it."
                    except ssh.UnknownHostKey as e:
                        msg = (f"OK. Host key not yet trusted ({e.key.get_name()} {ssh.fingerprint(e.key)}); "
                               "you'll be asked on first connect.")
            except Exception as e:  # noqa: BLE001
                msg = friendly(e)
            self._tester.done.emit(msg)
        threading.Thread(target=run, daemon=True).start()

    def _tested(self, msg: str) -> None:
        self.test_btn.setEnabled(True)
        ok = not msg or msg.startswith("OK")
        self.test_msg.setText(("✔ " + (msg or "Connected successfully.")) if ok else "✖ " + msg)
        self.test_msg.setStyleSheet(f"color:{C['ok'] if ok else C['danger']};")


# ======================================================================= about
def open_url(url: str) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl(url))


def _logo(name: str, height: int, widget) -> QLabel | None:
    """A crisp (HiDPI-aware) image label from assets/, or None if the file is missing."""
    from PySide6.QtGui import QPixmap
    from ..paths import assets_dir
    pm = QPixmap(str(assets_dir() / name))
    if pm.isNull():
        return None
    dpr = widget.devicePixelRatioF()
    pm = pm.scaledToHeight(int(height * dpr), Qt.SmoothTransformation)
    pm.setDevicePixelRatio(dpr)
    lbl = QLabel()
    lbl.setPixmap(pm)
    return lbl


class AboutDialog(_Base):
    def __init__(self, parent=None):
        super().__init__(parent)
        from .. import __version__, links
        self.setWindowTitle("About BlamixFiles")
        self.setFixedWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(12)
        logo = _logo("app.png", 44, self)
        if logo:
            head.addWidget(logo)
        name = QVBoxLayout()
        name.setSpacing(0)
        name.addWidget(QLabel("BlamixFiles", objectName="H1"))
        name.addWidget(QLabel(f"Version {__version__}", objectName="Muted"))
        head.addLayout(name, 1)
        lay.addLayout(head)
        lay.addWidget(QLabel("File transfer client for SFTP, SCP, FTP/FTPS, WebDAV and S3, with a "
                             "built-in editor, compare & sync and an encrypted vault.", wordWrap=True))
        lay.addWidget(QLabel("Free and open source (MIT). No ads, no bundled installers, no tracking, "
                             "no paid tier.", objectName="Muted", wordWrap=True))

        a = C["accent"]
        lk = QLabel(f"<a style='color:{a}' href='{links.REPO_URL}'>GitHub</a> &nbsp;·&nbsp; "
                    f"<a style='color:{a}' href='{links.RELEASES_URL}'>Release notes</a> &nbsp;·&nbsp; "
                    f"<a style='color:{a}' href='{links.ISSUES_URL}'>Report a problem</a>")
        lk.setOpenExternalLinks(True)
        lk.setWordWrap(True)
        lay.addWidget(lk)

        lay.addSpacing(6)
        box = QFrame(objectName="SupportBox")
        box.setStyleSheet(f"#SupportBox {{ background:{C['surface']}; border:1px solid {C['border']};"
                          " border-radius:12px; }")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(16, 12, 16, 14)
        bl.setSpacing(10)
        bl.addWidget(QLabel("If BlamixFiles saves you time, you can buy me a coffee. Thanks!", wordWrap=True))
        row = QHBoxLayout()
        coffee = QPushButton(icon("coffee", "#0b0d12"), " Buy me a coffee", objectName="Primary")
        coffee.clicked.connect(lambda: open_url(links.KOFI_URL))
        sponsor = QPushButton(icon("heart", C["muted"]), " Sponsor on GitHub")
        sponsor.clicked.connect(lambda: open_url(links.SPONSOR_URL))
        row.addWidget(coffee)
        row.addWidget(sponsor)
        row.addStretch(1)
        bl.addLayout(row)
        lay.addWidget(box)

        # made by: a quiet credit - the logo, and under it the link (both open the company site)
        lay.addSpacing(6)
        brand = _logo("blamixology.png", 34, self)
        if brand:
            brand.setCursor(Qt.PointingHandCursor)
            brand.setToolTip(links.COMPANY_URL)
            brand.mousePressEvent = lambda _e: open_url(links.COMPANY_URL)
            lay.addWidget(brand)
        made = QLabel(f"<span style='color:{C['muted']}'>Made by:</span> "
                      f"<a style='color:{a}' href='{links.COMPANY_URL}'>blamixology.ro</a>")
        made.setOpenExternalLinks(True)
        lay.addWidget(made)

        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addSpacing(4)
        lay.addLayout(row)
