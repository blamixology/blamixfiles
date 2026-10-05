"""A tree whose selected / hovered row is ONE rounded block across the whole row.

Qt paints an item and its expand-arrow (branch) area as two separate pieces, which leaves a seam
(and two rounded ends) in the middle of the highlight. Here the row is painted in drawRow(), and the
style sheet (see theme.py) makes the item and branch backgrounds transparent."""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QStyle, QTreeWidget

from .theme import C


class RowTree(QTreeWidget):
    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)

    def drawRow(self, painter, option, index) -> None:  # noqa: N802
        color = None
        if self.selectionModel().isSelected(index):
            color = C["selected"]
        elif option.state & QStyle.State_MouseOver:
            color = C["hover"]
        if color:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(color))
            row = QRect(0, option.rect.y(), self.viewport().width(), option.rect.height())
            painter.drawRoundedRect(row.adjusted(2, 0, -2, 0), 6, 6)
            painter.restore()
        super().drawRow(painter, option, index)
