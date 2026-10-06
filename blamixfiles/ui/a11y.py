"""Accessibility pass: give every control a name a screen reader can say, and make the main views
identifiable. Icon-only buttons take their tooltip as the name (without the "(Ctrl+N)" hint), search
boxes their placeholder."""
from __future__ import annotations

import re

from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QPlainTextEdit,
                               QTreeWidget)

VIEW_NAMES = {"ServerTree": "Servers", "Folders": "Folder tree", "Files": "File list", "Code": "Editor"}


def _clean(text: str) -> str:
    text = re.sub(r"\s*\(.*?\)\s*$", "", text or "")       # "New site (Ctrl+N)" -> "New site"
    return text.replace("&", "").strip(" .:…")


def apply(root) -> None:
    for w in root.findChildren(QAbstractButton):
        if not w.accessibleName():
            name = _clean(w.text()) or _clean(w.toolTip()) or w.objectName()
            if name:
                w.setAccessibleName(name)
    for w in root.findChildren(QLineEdit):
        if not w.accessibleName():
            name = _clean(w.placeholderText()) or _clean(w.toolTip())
            if name:
                w.setAccessibleName(name)
    for kind in (QComboBox, QAbstractSpinBox):           # (findChildren takes one type at a time)
        for w in root.findChildren(kind):
            if not w.accessibleName() and w.toolTip():
                w.setAccessibleName(_clean(w.toolTip()))
    for w in root.findChildren(QTreeWidget):
        if not w.accessibleName():
            name = VIEW_NAMES.get(w.objectName())
            if name:
                w.setAccessibleName(name)
    for w in root.findChildren(QPlainTextEdit):
        if not w.accessibleName():
            name = VIEW_NAMES.get(w.objectName())
            if name:
                w.setAccessibleName(name)
