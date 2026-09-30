"""A file browser pane (local or remote): path bar, filter, sortable list,
drag & drop, context menu. All I/O runs on the pane's Session thread."""
from __future__ import annotations

import json
import os

from PySide6.QtCore import QMimeData, Qt, QUrl, Signal
from PySide6.QtGui import QDrag, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..core.vfs import Entry
from .fmt import human_size, human_time
from .session import Session
from .theme import C, icon

MIME = "application/x-blamixfiles-entries"
COLS = ["Name", "Size", "Modified", "Permissions", "Owner"]


class _Item(QTreeWidgetItem):
    def __init__(self, e: Entry):
        super().__init__([e.name + ("/" if e.is_dir else ""), "" if e.is_dir else human_size(e.size),
                          human_time(e.mtime), e.perms, e.owner])
        self.entry = e
        self.setIcon(0, icon("folder" if e.is_dir else "file", C["accent"] if e.is_dir else C["muted"], 16))
        self.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        if e.is_link:
            self.setToolTip(0, f"{e.name} → {e.link_target}")
        if e.name.startswith("."):
            self.setForeground(0, self.foreground(0).color().darker(135))

    def __lt__(self, other: "_Item") -> bool:  # folders first, then by column
        tw = self.treeWidget()
        col = tw.sortColumn() if tw else 0
        asc = (tw.header().sortIndicatorOrder() == Qt.AscendingOrder) if tw else True
        a, b = self.entry, other.entry
        if a.is_dir != b.is_dir:
            return a.is_dir if asc else b.is_dir
        if col == 1:
            return a.size < b.size
        if col == 2:
            return a.mtime < b.mtime
        if col == 3:
            return a.perms < b.perms
        if col == 4:
            return a.owner < b.owner
        return a.name.lower() < b.name.lower()


class FileTree(QTreeWidget):
    dropped = Signal(object, str)          # payload dict, target dir ("" = current)

    def __init__(self, pane: "FilePane"):
        super().__init__()
        self.pane = pane
        self.setObjectName("Files")
        self.setColumnCount(len(COLS))
        self.setHeaderLabels(COLS)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.AscendingOrder)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        h = self.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for i, w in ((1, 80), (2, 110), (3, 92), (4, 90)):
            h.setSectionResizeMode(i, QHeaderView.Interactive)
            self.setColumnWidth(i, w)

    # ---- drag out
    def startDrag(self, actions):  # noqa: N802
        entries = self.pane.selected()
        if not entries:
            return
        md = QMimeData()
        md.setData(MIME, json.dumps({"pane": id(self.pane),
                                     "paths": [e.path for e in entries]}).encode())
        if self.pane.session.is_local:   # local files can also go to Explorer/Finder
            md.setUrls([QUrl.fromLocalFile(e.path) for e in entries])
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.exec(Qt.CopyAction)

    # ---- drop in
    def _accepts(self, md) -> bool:
        if md.hasFormat(MIME):
            payload = json.loads(bytes(md.data(MIME)).decode())
            return payload["pane"] != id(self.pane)
        return md.hasUrls() and not self.pane.session.is_local

    def dragEnterEvent(self, e):  # noqa: N802
        if self._accepts(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):  # noqa: N802
        if self._accepts(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):  # noqa: N802
        md = e.mimeData()
        if not self._accepts(md):
            return e.ignore()
        item = self.itemAt(e.position().toPoint())
        target = item.entry.path if isinstance(item, _Item) and item.entry.is_dir else ""
        if md.hasFormat(MIME):
            payload = json.loads(bytes(md.data(MIME)).decode())
        else:
            payload = {"pane": None, "paths": [u.toLocalFile() for u in md.urls() if u.isLocalFile()]}
        e.acceptProposedAction()
        self.dropped.emit(payload, target)


class FilePane(QWidget):
    transfer = Signal(object, object, str)     # entries (list[Entry]), source pane, target dir on the other side
    upload_paths = Signal(object, str)         # local paths from the OS, target dir here
    edit = Signal(object, object)              # Entry, this pane
    message = Signal(str, bool)

    def __init__(self, session: Session, title: str, start_dir: str = "", parent=None):
        super().__init__(parent)
        self.session = session
        self.path = ""
        self.entries: list[Entry] = []
        self.other: FilePane | None = None
        self._pending_select: str = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QWidget(objectName="Toolbar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(8, 6, 8, 6)
        bl.setSpacing(4)
        self.title = QLabel(title, objectName="H2")
        self.title.setStyleSheet("font-size:10pt;")
        bl.addWidget(self.title)

        def tb(name, tip, slot):
            b = QToolButton()
            b.setIcon(icon(name))
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bl.addWidget(b)
            return b
        tb("up", "Parent folder (Backspace)", self.go_up)
        tb("home", "Home folder", self.go_home)
        tb("refresh", "Refresh (F5)", self.refresh)
        tb("folder-plus", "New folder", self.new_folder)
        self.path_edit = QLineEdit()
        self.path_edit.returnPressed.connect(lambda: self.open_dir(self.path_edit.text().strip()))
        bl.addWidget(self.path_edit, 1)
        self.filter = QLineEdit(placeholderText="Filter")
        self.filter.setFixedWidth(130)
        self.filter.textChanged.connect(self._apply_filter)
        bl.addWidget(self.filter)
        lay.addWidget(bar)

        self.tree = FileTree(self)
        self.tree.itemActivated.connect(self._activated)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.dropped.connect(self._dropped)
        lay.addWidget(self.tree, 1)
        self.status = QLabel("", objectName="Hint")
        self.status.setContentsMargins(10, 4, 10, 4)
        lay.addWidget(self.status)

        for key, fn in ((Qt.Key_Backspace, self.go_up), (Qt.Key_F5, self.refresh),
                        (Qt.Key_Delete, self.delete_selected), (Qt.Key_F2, self.rename_selected),
                        (Qt.Key_F4, self._edit_selected)):
            sc = QShortcut(QKeySequence(key), self.tree)
            sc.setContext(Qt.WidgetShortcut)
            sc.activated.connect(fn)
        self._start_dir = start_dir

    # ------------------------------------------------------------ navigation
    def start(self) -> None:
        start = self._start_dir
        self.status.setText("Connecting …" if not self.session.is_local else "")

        def first(b):
            return b.normalize(start) if start and self.session.is_local else (start or b.home())
        self.session.run(first, self.open_dir, self._error)

    def open_dir(self, path: str, select: str = "") -> None:
        if self.session.is_local and path:
            path = os.path.expanduser(path)
        self._pending_select = select
        self.status.setText("Loading …")

        def load(b):
            p = b.normalize(path) if self.session.is_local else (path or "/")
            return p, b.list(p)
        self.session.run(load, self._loaded, self._error)

    def _loaded(self, result) -> None:
        path, entries = result
        self.path = path
        self.entries = entries
        self.path_edit.setText(path)
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        items = [_Item(e) for e in entries]
        self.tree.addTopLevelItems(items)
        self.tree.setSortingEnabled(True)
        self._apply_filter(self.filter.text())
        n_dirs = sum(1 for e in entries if e.is_dir)
        total = sum(e.size for e in entries if not e.is_dir)
        self.status.setText(f"{len(entries) - n_dirs} files, {n_dirs} folders, {human_size(total)}")
        if self._pending_select:
            for it in items:
                if it.entry.name == self._pending_select:
                    self.tree.setCurrentItem(it)
                    break
        elif items:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _error(self, msg: str) -> None:
        self.status.setText(msg)
        self.message.emit(msg, True)

    def refresh(self) -> None:
        cur = self.tree.currentItem()
        self.open_dir(self.path, cur.entry.name if isinstance(cur, _Item) else "")

    def go_up(self) -> None:
        b = self.session.backend
        if b is None or not self.path:
            return
        parent = b.parent(self.path)
        if parent != self.path:
            self.open_dir(parent, b.basename(self.path))

    def go_home(self) -> None:
        self.session.run(lambda b: b.home(), self.open_dir, self._error)

    def _apply_filter(self, text: str) -> None:
        t = text.lower().strip()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            it.setHidden(bool(t) and t not in it.entry.name.lower())

    # ------------------------------------------------------------ selection
    def selected(self) -> list[Entry]:
        return [it.entry for it in self.tree.selectedItems() if isinstance(it, _Item)]

    def _activated(self, item, _col) -> None:
        if not isinstance(item, _Item):
            return
        if item.entry.is_dir:
            self.open_dir(item.entry.path)
        else:
            self.edit.emit(item.entry, self)

    def _edit_selected(self) -> None:
        files = [e for e in self.selected() if not e.is_dir]
        for e in files[:5]:
            self.edit.emit(e, self)

    # ------------------------------------------------------------ transfers
    def send_selected(self) -> None:
        entries = self.selected()
        if entries and self.other:
            self.transfer.emit(entries, self, "")

    def _dropped(self, payload: dict, target: str) -> None:
        if payload.get("pane") is None:            # from Explorer/Finder
            self.upload_paths.emit(payload["paths"], target or self.path)
            return
        src = self.other
        if src is None or id(src) != payload["pane"]:
            return
        wanted = set(payload["paths"])
        entries = [e for e in src.entries if e.path in wanted]
        if entries:
            src.transfer.emit(entries, src, target or self.path)

    # ------------------------------------------------------------ file operations
    def _confirm_prod(self, what: str) -> bool:
        site = self.session.site
        if site is not None and site.production:
            return QMessageBox.warning(
                self, "Production server",
                f"{site.label} is marked as <b>production</b>.<br><br>{what}?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes
        return True

    def new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        name = name.strip()
        if not (ok and name) or self.session.backend is None:
            return
        path = self.session.backend.join(self.path, name)
        self.session.run(lambda b: b.mkdir(path), lambda _: self.open_dir(self.path, name), self._error)

    def rename_selected(self) -> None:
        sel = self.selected()
        if len(sel) != 1:
            return
        e = sel[0]
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=e.name)
        name = name.strip()
        if not (ok and name and name != e.name) or self.session.backend is None:
            return
        dst = self.session.backend.join(self.session.backend.parent(e.path), name)
        self.session.run(lambda b: b.rename(e.path, dst), lambda _: self.open_dir(self.path, name), self._error)

    def delete_selected(self) -> None:
        sel = self.selected()
        if not sel:
            return
        what = sel[0].name if len(sel) == 1 else f"{len(sel)} items"
        if QMessageBox.question(self, "Delete", f"Delete {what}? This can't be undone.") != QMessageBox.Yes:
            return
        if not self._confirm_prod(f"Really delete {what} on the server"):
            return

        def rm(b):
            for e in sel:
                if e.is_dir and not e.is_link:
                    b.remove_tree(e.path)
                else:
                    b.remove(e.path)
        self.session.run(rm, lambda _: self.refresh(), self._error)

    def chmod_selected(self) -> None:
        sel = self.selected()
        if not sel:
            return
        from .dialogs import ChmodDialog
        dlg = ChmodDialog(sel[0].name if len(sel) == 1 else f"{len(sel)} items", sel[0].mode or 0o644, self)
        if not dlg.exec():
            return
        mode, rec = dlg.mode(), dlg.recursive.isChecked()

        def apply(b):
            def one(path, is_dir):
                b.chmod(path, mode)
                if rec and is_dir:
                    for c in b.list(path):
                        one(c.path, c.is_dir and not c.is_link)
            for e in sel:
                one(e.path, e.is_dir and not e.is_link)
        self.session.run(apply, lambda _: self.refresh(), self._error)

    def _menu(self, pos) -> None:
        sel = self.selected()
        m = QMenu(self)
        b = self.session.backend
        remote = not self.session.is_local
        if sel:
            files = [e for e in sel if not e.is_dir]
            if len(sel) == 1 and sel[0].is_dir:
                m.addAction(icon("folder-open"), "Open", lambda: self.open_dir(sel[0].path))
            if files:
                m.addAction(icon("edit"), "Edit (F4)", self._edit_selected)
            if self.other:
                m.addAction(icon("download" if remote else "upload"),
                            "Download" if remote else "Upload", self.send_selected)
            m.addSeparator()
            m.addAction(icon("edit"), "Rename (F2)", self.rename_selected).setEnabled(len(sel) == 1)
            if b is not None and b.caps.chmod:
                m.addAction(icon("lock"), "Permissions…", self.chmod_selected)
            m.addAction(icon("copy"), "Copy path",
                        lambda: QApplication.clipboard().setText("\n".join(e.path for e in sel)))
            m.addSeparator()
            m.addAction(icon("trash", C["danger"]), "Delete", self.delete_selected)
            m.addSeparator()
        m.addAction(icon("folder-plus"), "New folder", self.new_folder)
        m.addAction(icon("refresh"), "Refresh", self.refresh)
        m.exec(self.tree.viewport().mapToGlobal(pos))
