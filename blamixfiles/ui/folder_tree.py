"""Folder tree above a file list (like FileZilla's). Loaded lazily: a folder's subfolders
are listed when you expand it; the path you're in is filled in from the listing the
pane already did, so following the pane costs no extra requests."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTreeWidgetItem

from .rowtree import RowTree
from .theme import C, icon

PATH = Qt.UserRole
LOADED = Qt.UserRole + 1


class FolderTree(RowTree):
    navigate = Signal(str)

    def __init__(self, pane):
        super().__init__()
        self.pane = pane
        self.setObjectName("Folders")
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setIndentation(14)
        self.setAnimated(False)
        self.itemExpanded.connect(self._expanded)
        self.itemClicked.connect(lambda it, _c: self.navigate.emit(it.data(0, PATH)))
        self._roots_loaded = False

    # ------------------------------------------------------------ helpers
    def _new_item(self, parent, name: str, path: str) -> QTreeWidgetItem:
        it = QTreeWidgetItem([name])
        it.setData(0, PATH, path)
        it.setData(0, LOADED, False)
        it.setIcon(0, icon("folder", C["accent"], 16))
        QTreeWidgetItem(it, ["…"])                     # placeholder: shows the expand arrow
        (parent.addChild(it) if parent is not None else self.addTopLevelItem(it))
        return it

    @staticmethod
    def _drop_placeholder(parent) -> None:
        if parent is not None:
            for i in reversed(range(parent.childCount())):
                if parent.child(i).data(0, PATH) is None:
                    parent.takeChild(i)

    def _child(self, parent, path: str):
        n = parent.childCount() if parent is not None else self.topLevelItemCount()
        for i in range(n):
            it = parent.child(i) if parent is not None else self.topLevelItem(i)
            if it.data(0, PATH) == path:
                return it
        return None

    def _set_children(self, item, folders: list) -> None:
        item.takeChildren()
        for e in sorted(folders, key=lambda e: e.name.lower()):
            self._new_item(item, e.name, e.path)
        item.setData(0, LOADED, True)

    def _chain(self, path: str) -> list[str]:
        b = self.pane.session.backend
        chain = [path]
        for _ in range(64):
            parent = b.parent(chain[-1])
            if parent == chain[-1] or parent == "":
                break
            chain.append(parent)
        return list(reversed(chain))

    # ------------------------------------------------------------ following the pane
    def show_path(self, path: str, entries: list) -> None:
        b = self.pane.session.backend
        if b is None or not path:
            return
        if not self._roots_loaded and self.pane.session.is_local and sys.platform == "win32":
            self._roots_loaded = True
            for d in b.list(""):                     # drives: instant, no network
                if self._child(None, d.path) is None:
                    self._new_item(None, d.name.rstrip("\\"), d.path)
        self.blockSignals(True)            # expanding ancestors here mustn't trigger listings
        parent = None
        item = None
        for p in self._chain(path):
            item = self._child(parent, p)
            if item is None:
                self._drop_placeholder(parent)
                name = b.basename(p) or p
                item = self._new_item(parent, name, p)
            if parent is not None and not parent.isExpanded():
                parent.setExpanded(True)
            parent = item
        self._set_children(item, [e for e in entries if e.is_dir])
        item.setExpanded(True)
        self.blockSignals(False)
        self.setCurrentItem(item)
        self.scrollToItem(item)

    def _expanded(self, item) -> None:
        if item.data(0, LOADED):
            return
        path = item.data(0, PATH)

        def load(b):
            return [e for e in b.list(path) if e.is_dir]
        self.pane.session.run(load, lambda folders: self._set_children(item, folders),
                              lambda msg: item.takeChildren())
