"""Sidebar markers for related and active conversations."""

from PySide6.QtCore import QEvent, QRect, Qt, QVariantAnimation
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from .. import theme, widgets


class RelationshipDelegate(QStyledItemDelegate):
    ROLE = Qt.UserRole + 1
    ACTIVITY = Qt.UserRole + 2
    ACTIVITY_WIDTH = 22

    def __init__(self, table):
        super().__init__(table)
        self._table = table
        self._active = False
        self._pulse = QVariantAnimation(self)
        self._pulse.setDuration(2200)
        self._pulse.setLoopCount(-1)
        self._pulse.setKeyValues([(0.0, 1.0), (0.5, 0.2), (1.0, 1.0)])
        self._pulse.valueChanged.connect(lambda _value: table.viewport().update())
        table.installEventFilter(self)

    def set_activity(self, active):
        self._active = active
        if active and self._table.isVisible():
            self._pulse.start()
        else:
            self._pulse.stop()

    def eventFilter(self, watched, event):
        if watched is self._table:
            if event.type() == QEvent.Show:
                self.set_activity(self._active)
            elif event.type() == QEvent.Hide:
                self._pulse.stop()
        return super().eventFilter(watched, event)

    def paint(self, painter, option, index):
        groups = index.data(self.ROLE) or []
        content = QStyleOptionViewItem(option)
        right = self.ACTIVITY_WIDTH if index.data(self.ACTIVITY) else 0
        content.rect.adjust(4 * len(groups), 0, -right, 0)
        if right and option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(theme.SELECT_BG))
        super().paint(painter, content, index)
        if right:
            rect = QRect(option.rect.right() - 15, option.rect.center().y() - 4, 10, 10)
            widgets.PulseDot.paint_dot(painter, rect, self._pulse.currentValue())
        left = option.rect.left()
        for kind in groups:
            color = theme.ACCENT if kind == "continuation" else theme.WARN
            painter.fillRect(left, option.rect.top(), 3, option.rect.height(), QColor(color))
            left += 4
