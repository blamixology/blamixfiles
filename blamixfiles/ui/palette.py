"""Ctrl+K command palette (ported from BlamixShell) and folder bookmarks."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFrame, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout

from .theme import C, icon


class Bookmarks:
    """Folder bookmarks, stored in the vault: {"site_id": "" (this computer) or a site id,
    "path": "...", "name": "..."}."""

    def __init__(self, store):
        self.store = store

    def for_site(self, site_id: str) -> list[dict]:
        return [b for b in self.store.bookmarks if b.get("site_id", "") == site_id]

    def add(self, site_id: str, path: str, name: str = "") -> None:
        if any(b["site_id"] == site_id and b["path"] == path for b in self.store.bookmarks):
            return
        self.store.bookmarks.append({"site_id": site_id, "path": path, "name": name or path})
        self.store.save()

    def remove(self, site_id: str, path: str) -> None:
        self.store.bookmarks = [b for b in self.store.bookmarks
                                if not (b["site_id"] == site_id and b["path"] == path)]
        self.store.save()


class CommandPalette(QDialog):
    """Ctrl+K: fuzzy search sites, bookmarks, sync profiles and actions; also accepts an address."""

    MAX_ROWS = 10

    def __init__(self, entries: list[tuple[str, str, object, str]], parent=None, placeholder="",
                 anchor=None):
        """entries: (title, subtitle, callback, icon_name). anchor: the toolbar search
        field - the palette drops down from it (like a browser address bar)."""
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Popup)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.entries = entries
        self.anchor = anchor
        self.quick_connect = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = self.frame = QFrame(objectName="Palette")
        outer.addWidget(frame)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(6, 6, 6, 6)
        self.q = QLineEdit(placeholderText=placeholder or "Search sites, bookmarks, sync profiles and actions, or type an address")
        lay.addWidget(self.q)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{C['border']};")
        lay.addWidget(line)
        self.list = QListWidget()
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        lay.addWidget(self.list)
        self.empty = QLabel(objectName="Hint")
        self.empty.setContentsMargins(14, 10, 14, 12)
        self.empty.setWordWrap(True)
        lay.addWidget(self.empty)
        self.q.textChanged.connect(self._filter)
        self.q.returnPressed.connect(self._run)
        self.list.itemActivated.connect(lambda _i: self._run())
        self.q.installEventFilter(self)
        self.setFixedWidth(620)
        self._filter("")

    def eventFilter(self, obj, ev):  # noqa: N802
        from PySide6.QtCore import QEvent
        if obj is self.q and ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k in (Qt.Key_Down, Qt.Key_Up):
                r = self.list.currentRow() + (1 if k == Qt.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(r, self.list.count() - 1)))
                return True
            if k == Qt.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(obj, ev)

    @staticmethod
    def _score(q: str, text: str) -> int:
        q, text = q.lower(), text.lower()
        if not q:
            return 1
        if q in text:
            return 100 - text.index(q)
        i = 0
        for ch in text:
            if i < len(q) and ch == q[i]:
                i += 1
        return 10 if i == len(q) else 0

    def _filter(self, q: str) -> None:
        self.list.clear()
        scored = []
        for e in self.entries:
            sc = max(self._score(q, e[0]), self._score(q, e[1]) - 5)
            if sc > 0:
                scored.append((sc, e))
        scored.sort(key=lambda x: -x[0])
        qs = q.strip()
        if qs and ("@" in qs or "." in qs or "://" in qs) and " " not in qs:
            it = QListWidgetItem(icon("bolt", C["accent"]), f"Quick connect  ›  {qs}")
            it.setData(Qt.UserRole, ("quick", qs))
            self.list.addItem(it)
        for _sc, (title, sub, cb, ico) in scored[:60]:
            it = QListWidgetItem(icon(ico), f"{title}   ·   {sub}" if sub else title)
            it.setData(Qt.UserRole, ("cb", cb))
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)
        self._fit()

    def _fit(self) -> None:
        """Size the list to its rows (max MAX_ROWS) and show an empty state instead of
        a big blank box when nothing matches."""
        n = self.list.count()
        self.list.setVisible(n > 0)
        self.empty.setVisible(n == 0)
        if n:
            row = max(self.list.sizeHintForRow(0), 28)
            self.list.setFixedHeight(row * min(n, self.MAX_ROWS) + 2 * self.list.frameWidth() + 4)
        else:
            q = self.q.text().strip()
            self.empty.setText(f"Nothing matches “{q}”.\n"
                               "Tip: type an address (sftp://user@host, ftp://…, s3://…) to quick-connect.")
        # re-measure now (hidden/shown children otherwise keep the old height)
        for lay in (self.frame.layout(), self.layout()):
            lay.invalidate()
            lay.activate()
        self.resize(self.width(), self.sizeHint().height())

    def _run(self) -> None:
        it = self.list.currentItem()
        if not it:
            return
        kind, val = it.data(Qt.UserRole)
        self.accept()
        if kind == "quick" and self.quick_connect:
            self.quick_connect(val)
        elif kind == "cb":
            val()

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        a = self.anchor
        if a is not None and a.isVisible():
            # open over the search field, as its drop-down (same left edge, at least as wide)
            from PySide6.QtCore import QPoint
            self.setFixedWidth(max(a.width(), 560))
            top_left = a.mapToGlobal(QPoint(0, 0))
            self.move(top_left.x(), top_left.y() - 4)
        else:
            p = self.parentWidget()
            if p:
                g = p.geometry()
                self.move(g.x() + (g.width() - self.width()) // 2, g.y() + 90)
        self.q.setFocus()
