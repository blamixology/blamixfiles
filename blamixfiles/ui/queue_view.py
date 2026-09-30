"""The transfer queue panel at the bottom of the window."""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QLabel, QMenu, QStyledItemDelegate,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

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

    def __init__(self, engine: E.TransferEngine, parent=None):
        super().__init__(parent)
        self.engine = engine
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
        self.pause_btn = tb("pause", "Pause the queue", self._toggle_pause)
        tb("retry", "Retry failed", lambda: engine.retry())
        tb("x", "Cancel all", lambda: engine.cancel())
        tb("clear", "Clear finished", self.clear_finished)
        lay.addWidget(bar)

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

    def _toggle_pause(self) -> None:
        p = not self.engine.paused
        self.engine.set_paused(p)
        self.pause_btn.setIcon(icon("play" if p else "pause"))
        self.pause_btn.setToolTip("Resume the queue" if p else "Pause the queue")
        self._update_summary()

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
