"""Sidebar markers for related conversations."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem

from .. import theme


class RelationshipDelegate(QStyledItemDelegate):
    ROLE = Qt.UserRole + 1

    def paint(self, painter, option, index):
        groups = index.data(self.ROLE) or []
        content = QStyleOptionViewItem(option)
        content.rect.adjust(4 * len(groups), 0, 0, 0)
        super().paint(painter, content, index)
        left = option.rect.left()
        for kind in groups:
            color = theme.ACCENT if kind == "continuation" else theme.WARN
            painter.fillRect(left, option.rect.top(), 3, option.rect.height(), QColor(color))
            left += 4
