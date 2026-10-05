"""The transfer queue panel at the bottom of the window."""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QActionGroup, QColor, QPainter
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QLabel, QMenu, QPushButton,
                               QStyledItemDelegate, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ..core import engine as E
from .fmt import human_size, human_speed
from .theme import C, icon

STATUS_TEXT = {E.QUEUED: "Queued", E.RUNNING: "Transferring", E.DONE: "Done", E.FAILED: "Failed",
               E.SKIPPED: "Skipped (exists)", E.CANCELLED: "Cancelled"}
STATUS_COLOR = {E.DONE: C["ok"], E.FAILED: C["danger"], E.CANCELLED: C["faint"], E.SKIPPED: C["muted"]}


class _BarDelegate(QStyledItemDelegate):
    def paint(self, p: QPainter, opt, index):
        frac = index.data(Qt.UserRole)
        if frac is None:
            return super().paint(p, opt, index)
        r = opt.rect.adjusted(6, opt.rect.height() // 2 - 3, -6, -(opt.rect.height() // 2 - 3))
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(C["surface2"]))
        p.drawRoundedRect(r, 3, 3)
        if frac > 0:
            p.setBrush(QColor(index.data(Qt.UserRole + 1) or C["accent"]))
            p.drawRoundedRect(QRect(r.x(), r.y(), max(6, int(r.width() * min(1.0, frac))), r.height()), 3, 3)
        p.restore()


class QueueView(QWidget):
    summary = Signal(str)

    def __init__(self, engine: E.TransferEngine, settings=None, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self.items: dict[int, QTreeWidgetItem] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget(objectName="Toolbar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 4, 10, 4)
        self.title = QLabel("Transfers", objectName="SectionLabel")
        bl.addWidget(self.title)
        bl.addStretch(1)
        bl.addWidget(QLabel("If the file exists:", objectName="Hint"))
        self.policy = QComboBox()
        for k, v in E.POLICIES.items():
            self.policy.addItem(v, k)
        self.policy.setCurrentIndex(self.policy.findData(engine.policy))
        self.policy.currentIndexChanged.connect(lambda: setattr(engine, "policy", self.policy.currentData()))
        bl.addWidget(self.policy)

        def tb(name, tip, slot):
            b = QToolButton()
            b.setIcon(icon(name))
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bl.addWidget(b)
            return b
        self.speed_btn = QToolButton()
        self.speed_btn.setIcon(icon("gauge"))
        self.speed_btn.setPopupMode(QToolButton.InstantPopup)
        self.speed_btn.setMenu(self._speed_menu())
        bl.addWidget(self.speed_btn)
        self._update_speed_tip()
        self.pause_btn = tb("pause", "Pause the queue", self._toggle_pause)
        tb("retry", "Retry failed", lambda: engine.retry())
        tb("x", "Cancel all", lambda: engine.cancel())
        tb("clear", "Clear finished", self.clear_finished)
        lay.addWidget(bar)

        # "you have unfinished transfers from last time" banner
        self.banner = QWidget(objectName="Banner")
        bn = QHBoxLayout(self.banner)
        bn.setContentsMargins(12, 6, 12, 6)
        self.banner_text = QLabel("")
        bn.addWidget(self.banner_text, 1)
        resume = QPushButton(icon("play", C["on_accent"]), " Resume", objectName="Primary")
        resume.clicked.connect(self._resume_restored)
        discard = QPushButton("Discard")
        discard.clicked.connect(self._discard_restored)
        bn.addWidget(resume)
        bn.addWidget(discard)
        self.banner.hide()
        lay.addWidget(self.banner)

        self.tree = QTreeWidget()
        self.tree.setObjectName("Files")
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setHeaderLabels(["File", "Direction", "Size", "Progress", "Speed", "Status"])
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for i, w in ((1, 260), (2, 80), (3, 140), (4, 90), (5, 220)):
            h.setSectionResizeMode(i, QHeaderView.Interactive)
            self.tree.setColumnWidth(i, w)
        self.tree.setItemDelegateForColumn(3, _BarDelegate(self.tree))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        lay.addWidget(self.tree, 1)

    # ------------------------------------------------------------ saved queue
    def offer_resume(self, n: int) -> None:
        self.banner_text.setText(f"{n} transfer(s) didn't finish last time. Resume them?")
        self.banner.show()
        self._sync_pause_button()

    def _resume_restored(self) -> None:
        self.banner.hide()
        self.engine.set_paused(False)
        self._sync_pause_button()

    def _discard_restored(self) -> None:
        self.banner.hide()
        self.engine.discard_unfinished()
        self.engine.set_paused(False)
        self._sync_pause_button()
        self.clear_finished()

    # ------------------------------------------------------------ speed limits
    PRESETS = [0, 128, 512, 1024, 2048, 5120, 10240]        # KB/s

    @staticmethod
    def _fmt_limit(kb: int) -> str:
        if not kb:
            return "Unlimited"
        return f"{kb // 1024} MB/s" if kb >= 1024 and kb % 1024 == 0 else f"{kb} KB/s"

    def _speed_menu(self) -> QMenu:
        m = QMenu(self)
        for direction, title in (("upload", "Upload limit"), ("download", "Download limit")):
            sub = m.addMenu(icon("upload" if direction == "upload" else "download"), title)
            group = QActionGroup(sub)
            current = self.engine.limits[direction].rate // 1024
            for kb in self.PRESETS:
                a = sub.addAction(self._fmt_limit(kb))
                a.setCheckable(True)
                a.setChecked(kb == current)
                group.addAction(a)
                a.triggered.connect(lambda _=False, d=direction, v=kb: self.set_limit(d, v))
            sub.addSeparator()
            custom = sub.addAction("Custom…")
            custom.triggered.connect(lambda _=False, d=direction: self._custom_limit(d))
            sub.aboutToShow.connect(lambda sub=sub, d=direction: self._check_current(sub, d))
        m.addSeparator()
        self.verify_action = m.addAction(icon("shield"), "Verify checksums after each transfer")
        self.verify_action.setCheckable(True)
        self.verify_action.setChecked(self.engine.verify)
        self.verify_action.setToolTip("Compare SHA-256/MD5 on both sides (needs a shell, HASH command or S3 ETag)")
        self.verify_action.toggled.connect(self.set_verify)
        return m

    def set_verify(self, on: bool) -> None:
        self.engine.verify = on
        if self.settings is not None:
            self.settings["verify_checksums"] = on
            self.settings.save()

    def _check_current(self, sub: QMenu, direction: str) -> None:
        current = self.engine.limits[direction].rate // 1024
        for a in sub.actions():
            if a.isCheckable():
                a.setChecked(a.text() == self._fmt_limit(current))

    def _custom_limit(self, direction: str) -> None:
        from PySide6.QtWidgets import QInputDialog
        kb, ok = QInputDialog.getInt(self, "Speed limit", f"{direction.capitalize()} limit in KB/s (0 = unlimited):",
                                     self.engine.limits[direction].rate // 1024, 0, 10_000_000, 64)
        if ok:
            self.set_limit(direction, kb)

    def set_limit(self, direction: str, kb: int) -> None:
        self.engine.set_limit(direction, kb * 1024)
        if self.settings is not None:
            self.settings["limit_up_kb" if direction == "upload" else "limit_down_kb"] = kb
            self.settings.save()
        self._update_speed_tip()
        self._update_summary()

    def _update_speed_tip(self) -> None:
        up = self.engine.limits["upload"].rate // 1024
        down = self.engine.limits["download"].rate // 1024
        self.speed_btn.setToolTip(f"Speed limits: ↑ {self._fmt_limit(up)} · ↓ {self._fmt_limit(down)}")
        limited = bool(up or down)
        self.speed_btn.setIcon(icon("gauge", C["warn"] if limited else None))

    def _sync_pause_button(self) -> None:
        p = self.engine.paused
        self.pause_btn.setIcon(icon("play" if p else "pause"))
        self.pause_btn.setToolTip("Resume the queue" if p else "Pause the queue")
        self._update_summary()

    def _toggle_pause(self) -> None:
        self.engine.set_paused(not self.engine.paused)
        self.banner.hide()
        self._sync_pause_button()

    def update_job(self, job: E.Job) -> None:
        it = self.items.get(job.id)
        if it is None:
            it = QTreeWidgetItem()
            it.setData(0, Qt.UserRole, job.id)
            self.items[job.id] = it
            self.tree.addTopLevelItem(it)
            site = job.site.label
            arrow = f"→ {site}: {job.dst}" if job.kind == "upload" else f"← {site}: {job.src}"
            it.setText(1, arrow)
            it.setToolTip(1, arrow)
            it.setIcon(0, icon("folder" if job.is_dir else ("upload" if job.kind == "upload" else "download"),
                               C["accent"] if job.is_dir else C["muted"], 16))
            it.setText(0, job.name)
            it.setToolTip(0, job.src)
        it.setText(2, "" if job.is_dir else human_size(job.size))
        frac = 1.0 if job.status == E.DONE else (job.done / job.size if job.size else 0.0)
        it.setData(3, Qt.UserRole, None if job.is_dir else frac)
        it.setData(3, Qt.UserRole + 1, STATUS_COLOR.get(job.status, C["accent"]))
        it.setText(4, human_speed(job.speed) if job.status == E.RUNNING else "")
        status = job.error or STATUS_TEXT.get(job.status, job.status)
        if job.status == E.DONE and job.verified:
            status = (f"Done ✓ {job.verified.upper()} verified" if job.verified not in ("unsupported",)
                      else "Done (server can't checksum)")
        if job.status == E.RUNNING and job.error:
            status = job.error
        it.setText(5, status)
        it.setToolTip(5, status)
        it.setForeground(5, QColor(STATUS_COLOR.get(job.status, C["text"])))
        self.tree.viewport().update()
        self._update_summary()

    def _update_summary(self) -> None:
        jobs = self.engine.jobs
        active = sum(1 for j in jobs if j.status in (E.QUEUED, E.RUNNING))
        failed = sum(1 for j in jobs if j.status == E.FAILED)
        speed = sum(j.speed for j in jobs if j.status == E.RUNNING)
        parts = []
        if active:
            parts.append(f"{active} in queue")
        if speed:
            parts.append(human_speed(speed))
        if failed:
            parts.append(f"{failed} failed")
        if self.engine.paused:
            parts.append("paused")
        up, down = self.engine.limits["upload"].rate, self.engine.limits["download"].rate
        if up or down:
            parts.append("limited " + " ".join(x for x in (
                f"↑{self._fmt_limit(up // 1024)}" if up else "", f"↓{self._fmt_limit(down // 1024)}" if down else "") if x))
        text = " · ".join(parts)
        self.title.setText("TRANSFERS" + (f"  ·  {text}" if text else ""))
        self.summary.emit(text)

    def clear_finished(self) -> None:
        for jid in self.engine.clear_finished():
            it = self.items.pop(jid, None)
            if it is not None:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(it))
        self._update_summary()

    def _menu(self, pos) -> None:
        it = self.tree.itemAt(pos)
        if it is None:
            return
        jid = it.data(0, Qt.UserRole)
        m = QMenu(self)
        m.addAction(icon("x"), "Cancel", lambda: self.engine.cancel(jid))
        m.addAction(icon("retry"), "Retry", lambda: self.engine.retry(jid))
        m.exec(self.tree.viewport().mapToGlobal(pos))
