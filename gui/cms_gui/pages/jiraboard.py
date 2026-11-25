"""Jira's Board tab. Every network request originates in an interaction."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from html import escape

from PySide6.QtCore import QMimeData, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QDrag, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QScrollArea, QStyle,
    QStyledItemDelegate, QVBoxLayout, QWidget,
)

from .. import theme, widgets
from .jiradialogs import INPUT_HINTS, TransitionFieldsDialog, input_scalar

MIME = "application/x-qavector-jira-card"
EMPTY = {"value": None, "label": "No value"}
SAVING_ROLE = Qt.UserRole + 1
DETAILS_ROLE = Qt.UserRole + 2
SELECTED_ROLE = Qt.UserRole + 3
MOVE_ATTEMPTS = 3
MOVE_RETRY_DELAY_MS = 750
STATUS_CATEGORY_ORDER = {"new": 15, "indeterminate": 55, "done": 110}
WORKFLOW_STAGES = (
    (0, ("draft",)),
    (10, ("to do", "todo", "open", "backlog")),
    (20, ("reopen", "reopened")),
    (30, ("in progress", "in development", "working")),
    (40, ("ready for test", "ready for testing", "ready for qa")),
    (45, ("in test", "in testing", "review", "in review", "in qa")),
    (60, ("ready for stage", "ready for staging", "ready for uat")),
    (70, ("on stage", "on stage uat", "on staging", "uat", "in uat")),
    (80, ("ready for prod", "ready for prod release", "ready for release", "ready for production")),
    (90, ("on prod", "on production", "in production", "released", "deployed")),
    (100, ("done", "resolved", "closed")),
)
STATUS_NAMES = {name: rank for rank, names in WORKFLOW_STAGES for name in names}
PRIORITY_MARKS = {
    "blocker": "blocker", "highest": "highest", "critical": "highest",
    "high": "high", "major": "high", "medium": "medium", "normal": "medium",
    "low": "low", "minor": "low", "lowest": "lowest", "trivial": "lowest",
}


def value_key(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return json.dumps(value, ensure_ascii=False)


def column_order(kind, key, item, status_order=None):
    if key in ("", "null"):
        return (1, key == "")
    if kind == "status":
        if status_order and item["value"] in status_order:
            return (0, 0, status_order[item["value"]])
        name = re.sub(r"[^\w]+", " ", item["label"].casefold()).strip()
        return (0, 1, STATUS_NAMES.get(name, STATUS_CATEGORY_ORDER.get(item.get("category"), 115)))
    if kind == "option":
        return (0, 0)
    if kind in {"number", "boolean", "date", "datetime"}:
        return (0, item["value"])
    return (0, item["label"].casefold())


def assignee_colors(identity):
    digest = hashlib.blake2s(identity.encode(), digest_size=3).digest()
    hue = (28 + 295 * int.from_bytes(digest[:2], "big") / 65535) / 360
    saturation = 0.38 + 0.22 * digest[2] / 255
    dark = QColor(theme.SURFACE).lightness() < 128
    return (QColor.fromHslF(hue, saturation, 0.26 if dark else 0.87),
            QColor.fromHslF(hue, max(0.60, saturation), 0.83 if dark else 0.24))


class ColumnIndicator(QWidget):
    def __init__(self, kind, value):
        super().__init__()
        self.kind, self.value = kind, value
        self.setFixedSize(4, 13)

    def paintEvent(self, event):
        if self.kind == "user":
            _, ink = assignee_colors(self.value)
        else:
            ink = QColor({"new": theme.NEUTRAL[500], "indeterminate": theme.VAR_INK,
                          "done": theme.DIFF_ADD_INK}.get(self.value, theme.DIVIDER_STRONG))
        painter = QPainter(self)
        painter.fillRect(self.rect(), ink)


@dataclass
class BoardState:
    rows: list = field(default_factory=list)
    choices: dict = field(default_factory=dict)
    contexts: list = field(default_factory=list)
    fields: dict = field(default_factory=dict)
    status_order: dict = field(default_factory=dict)
    source_board: str = ""
    cursor: object = None
    request: dict = field(default_factory=dict)
    loading: str = ""
    message: str = ""
    move_details: str = ""
    selected: set = field(default_factory=set)
    unverified: set = field(default_factory=set)


class CardDelegate(QStyledItemDelegate):
    WIDTH = 224
    HEIGHT = 108
    GAP = 7

    def sizeHint(self, option, index):
        return QSize(self.WIDTH, self.HEIGHT + self.GAP)

    @staticmethod
    def bounds(rect):
        return QRect(rect.topLeft(), QSize(CardDelegate.WIDTH, CardDelegate.HEIGHT))

    @staticmethod
    def summary_lines(summary, metrics, width):
        words = summary.split()
        first = ""
        for index, word in enumerate(words):
            line = f"{first} {word}" if first else word
            if first and metrics.horizontalAdvance(line) > width:
                second = " ".join(words[index:])
                return (metrics.elidedText(first, Qt.ElideRight, width),
                        metrics.elidedText(second, Qt.ElideRight, width))
            first = line
        return (metrics.elidedText(first, Qt.ElideRight, width),)

    def paint(self, painter, option, index):
        key, _, summary = (index.data(Qt.DisplayRole) or "").partition("\n")
        details = index.data(DETAILS_ROLE) or {}
        selected = bool(index.data(SELECTED_ROLE))
        hovered = bool(option.state & QStyle.State_MouseOver)
        rect = self.bounds(option.rect)
        inside = rect.adjusted(10, 8, -10, -8)
        painter.save()
        painter.setClipRect(rect)
        painter.fillRect(rect, QColor(theme.SELECT_BG if selected else theme.SURFACE))
        border = theme.ACCENT if selected else theme.DIVIDER_STRONG if hovered else theme.DIVIDER
        painter.setPen(QColor(border))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))

        mono = theme.mono_font()
        mono.setPixelSize(11)
        mono.setBold(True)
        painter.setFont(mono)
        painter.setPen(QColor(theme.SELECT_MUTED if selected else theme.NEUTRAL[600]))
        header = QRect(inside.left(), inside.top(), inside.width(), 15)
        metrics = QFontMetrics(mono)
        if index.data(SAVING_ROLE):
            status = "Saving…"
            painter.drawText(header, Qt.AlignRight | Qt.AlignVCenter, status)
            header.setWidth(header.width() - metrics.horizontalAdvance(status) - 8)
        if details.get("blocked"):
            width = metrics.horizontalAdvance("Blocker") + 16
            badge = QRect(header.right() - width + 1, header.top(), width, header.height())
            self._badge(painter, badge, "Blocker", metrics, QColor(theme.BAD_TINT), QColor(theme.BAD_INK))
            header.setWidth(header.width() - width - 8)
            painter.setPen(QColor(theme.SELECT_MUTED if selected else theme.NEUTRAL[600]))
        painter.drawText(header, Qt.AlignLeft | Qt.AlignVCenter,
                         metrics.elidedText(key, Qt.ElideRight, header.width()))

        title = QFont(option.font)
        title.setPixelSize(13)
        painter.setFont(title)
        painter.setPen(QColor(theme.SELECT_INK if selected else theme.TEXT))
        metrics = QFontMetrics(title)
        line = QRect(inside.left(), header.bottom() + 3, inside.width(), 17)
        for text in self.summary_lines(summary, metrics, inside.width()):
            painter.drawText(line, Qt.AlignLeft | Qt.AlignVCenter, text)
            line.translate(0, line.height())
        self._paint_details(painter, inside, title, details, selected)
        if option.state & QStyle.State_HasFocus:
            painter.setPen(QPen(QColor(theme.SELECT_INK if selected else theme.ACCENT), 1, Qt.DotLine))
            painter.drawRect(rect.adjusted(3, 3, -4, -4))
        painter.restore()

    @staticmethod
    def _badge(painter, badge, text, metrics, background, ink, align=Qt.AlignCenter):
        painter.fillRect(badge, background)
        painter.setPen(ink)
        text_rect = badge.adjusted(6, 0, -6, 0)
        painter.drawText(text_rect, align | Qt.AlignVCenter,
                         metrics.elidedText(text, Qt.ElideRight, text_rect.width()))

    @staticmethod
    def _priority(painter, area, priority, metrics, ink):
        mark = PRIORITY_MARKS.get(priority.casefold())
        if mark:
            x, y = area.left() + 5, area.center().y()
            color = (theme.WARN_INK if mark == "medium" else
                     theme.VAR_INK if mark in {"low", "lowest"} else theme.BAD_INK)
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(QColor(color), 1.3, Qt.SolidLine, Qt.RoundCap))
            if mark == "blocker":
                painter.drawEllipse(QRect(x - 5, y - 5, 10, 10))
                painter.drawLine(x - 2, y, x + 2, y)
            elif mark == "medium":
                painter.drawLine(x - 4, y - 2, x + 4, y - 2)
                painter.drawLine(x - 4, y + 2, x + 4, y + 2)
            else:
                direction = -1 if mark in {"low", "lowest"} else 1
                offsets = {"highest": (-4, 0, 4), "lowest": (-2, 2)}.get(mark, (0,))
                for offset in offsets:
                    middle = y + offset
                    painter.drawLine(x - 4, middle + 2 * direction, x, middle - 2 * direction)
                    painter.drawLine(x, middle - 2 * direction, x + 4, middle + 2 * direction)
            painter.restore()
            area.adjust(15, 0, 0, 0)
        painter.setPen(ink)
        painter.drawText(area, Qt.AlignRight | Qt.AlignVCenter,
                         metrics.elidedText(priority, Qt.ElideRight, area.width()))

    def _paint_details(self, painter, inside, font, details, selected):
        separator = inside.top() + 55
        painter.setPen(QColor(theme.ACCENT if selected else theme.DIVIDER))
        painter.drawLine(inside.left(), separator, inside.right(), separator)

        small = QFont(font)
        small.setPixelSize(11)
        painter.setFont(small)
        metrics = QFontMetrics(small)
        muted = QColor(theme.SELECT_MUTED if selected else theme.NEUTRAL[600])
        line = QRect(inside.left(), separator + 5, inside.width(), 16)
        priority = details.get("priority")
        if priority:
            width = min(92, metrics.horizontalAdvance(priority) + (17 if priority.casefold() in PRIORITY_MARKS else 2))
            area = QRect(line.right() - width + 1, line.top(), width, line.height())
            self._priority(painter, area, priority, metrics, QColor(theme.SELECT_INK if selected else theme.TEXT))
            line.setWidth(line.width() - width - 8)
        assignee = details.get("assignee")
        if assignee:
            line.setWidth(min(line.width(), metrics.horizontalAdvance(assignee) + 16))
            background, ink = assignee_colors(details.get("assignee_id") or assignee)
            self._badge(painter, line, assignee, metrics, background, ink, Qt.AlignLeft)
        else:
            painter.setPen(muted)
            painter.drawText(line, Qt.AlignLeft | Qt.AlignVCenter, "Unassigned")

        line = QRect(inside.left(), separator + 23, inside.width(), 16)
        due_date = details.get("due_date")
        if due_date:
            due = "Due " + due_date
            painter.setPen(QColor(theme.WARN_INK if due_date < date.today().isoformat() else theme.VAR_INK))
            painter.drawText(line, Qt.AlignRight | Qt.AlignVCenter, due)
            line.setWidth(line.width() - metrics.horizontalAdvance(due) - 8)
        if details.get("story_points") is not None:
            painter.setPen(QColor(theme.SELECT_INK if selected else theme.AUTO))
            points = f'{details["story_points"]} SP'
            painter.drawText(line, Qt.AlignLeft | Qt.AlignVCenter, metrics.elidedText(points, Qt.ElideRight, line.width()))


class Column(QListWidget):
    def __init__(self, board, target, movable):
        super().__init__(board)
        self.board, self.target, self.movable = board, target, movable
        self.setFrameShape(QFrame.NoFrame)
        self.setItemDelegate(CardDelegate(self))
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setDragEnabled(movable)
        self.setAcceptDrops(movable)
        self.setDragDropMode(QAbstractItemView.DragDrop if movable else QAbstractItemView.NoDragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.itemActivated.connect(lambda item: board.open_card(item.data(Qt.UserRole)))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not (self.board.writing or self.board.state.loading):
            item = self.itemAt(event.position().toPoint())
            key = item.data(Qt.UserRole) if item else None
            self.board.select_card(key, bool(event.modifiers() & Qt.ControlModifier))
        super().mousePressEvent(event)

    def startDrag(self, _actions):
        item = self.currentItem()
        if item is None or not self.movable or self.board.state.loading or self.board.writing:
            return
        mime = QMimeData()
        mime.setData(MIME, item.data(Qt.UserRole).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.viewport().grab(CardDelegate.bounds(self.visualItemRect(item))))
        drag.exec(Qt.MoveAction)

    def _accepts(self, event):
        source = event.source()
        return (self.movable and not self.board.writing and not self.board.state.loading
                and isinstance(source, Column) and source.board is self.board
                and (source is not self or len(self.board.state.selected) > 1)
                and event.mimeData().hasFormat(MIME))

    def dragEnterEvent(self, event):
        if self._accepts(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._accepts(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self._accepts(event):
            key = bytes(event.mimeData().data(MIME)).decode()
            event.acceptProposedAction()
            self.board.move_card(key, self.target)


class BoardSkeleton(widgets.Skeleton):
    def __init__(self, host, *, compact=False):
        self.compact = compact
        area = None
        if compact:
            area = lambda: host.rect().adjusted(host.fontMetrics().horizontalAdvance(host.text()) + 16, 0, 0, 0)
        super().__init__(host, area=area)

    def shapes(self):
        if self.compact:
            y = max(0, (self.height() - 8) / 2)
            return [(QRectF(x, y, min(44, self.width() - x), 8), index)
                    for index, x in enumerate(range(0, min(self.width(), 150), 52))]

        shapes = []
        pitch = CardDelegate.WIDTH + 30
        for column, x in enumerate(range(13, self.width(), pitch)):
            width = min(CardDelegate.WIDTH, self.width() - x)
            shapes.append((QRectF(x, 10, min(width, 94), 12), column))
            for row, y in enumerate(range(32, self.height(), CardDelegate.HEIGHT + CardDelegate.GAP)):
                shapes.append((QRectF(x, y, width, CardDelegate.HEIGHT), row + column))
        return shapes

    def paint_shapes(self, painter):
        if self.compact:
            super().paint_shapes(painter)
            return
        for rect, row in self.shapes():
            shade = self.colour(row)
            if rect.height() != CardDelegate.HEIGHT:
                painter.fillRect(rect, shade)
                continue
            painter.fillRect(rect, QColor(theme.SURFACE))
            painter.setPen(QColor(theme.DIVIDER))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            inside = rect.adjusted(10, 0, -10, 0)
            if inside.width() > 0:
                for y, width, height in ((12, 0.30, 7), (30, 0.85, 9), (46, 0.66, 9), (74, 0.42, 8)):
                    painter.fillRect(QRectF(inside.left(), rect.top() + y, inside.width() * width, height), shade)


class BoardTab(QWidget):
    def __init__(self, jira, combo_class):
        super().__init__(jira)
        self.jira = jira
        self.states = {}
        self.connection_id = ""
        self.state = BoardState()
        self.extra = {}
        self.writing = False
        self.generation = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        finder = QHBoxLayout()
        self.search = QLineEdit(placeholderText="Search issues")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.search_board)
        finder.addWidget(self.search, 1)
        self.jql = QPushButton("JQL", checkable=True)
        self.jql.toggled.connect(self._jql_mode)
        finder.addWidget(self.jql)
        apply = QPushButton("Search")
        apply.clicked.connect(self.search_board)
        finder.addWidget(apply)
        self.refresh = QPushButton("Refresh")
        self.refresh.clicked.connect(self.search_board)
        finder.addWidget(self.refresh)
        self.more = QPushButton("Load more")
        self.more.clicked.connect(self.load_more)
        finder.addWidget(self.more)
        layout.addLayout(finder)

        self.projects = combo_class("All projects")
        self.projects.setAccessibleName("Board project")
        self.statuses = combo_class("All statuses")
        self.statuses.setAccessibleName("Board status")
        for operation, combo in (("projects", self.projects), ("statuses", self.statuses)):
            combo.wanted.connect(lambda kind=operation, widget=combo: self._filter_choices(kind, widget))
            combo.activated.connect(self.search_board)
        self.mine = QPushButton("Assigned to me", checkable=True)
        self.mine.clicked.connect(self.search_board)
        self.group = QComboBox()
        self.group.setAccessibleName("Board group by")
        self.group.setMaximumWidth(240)
        self.group.activated.connect(self.search_board)
        self.board_order = combo_class("Automatic order")
        self.board_order.setAccessibleName("Status order from Jira board")
        self.board_order.setMaximumWidth(230)
        self.board_order.wanted.connect(self._board_choices)
        self.board_order.activated.connect(self.search_board)
        self.add_column = QPushButton("Add column")
        self.add_column.clicked.connect(self._add_column)
        # Wraps rather than widening the page: the tabs share one minimum width.
        layout.addWidget(widgets.flow_row(self.projects, self.statuses, self.mine, None,
                                          QLabel("Group by"), self.group, self.board_order,
                                          self.add_column))

        self.scroller = QScrollArea(widgetResizable=True)
        self.scroller.setFrameShape(QScrollArea.NoFrame)
        layout.addWidget(self.scroller, 1)
        self.message = QLabel()
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        self.message.hide()
        layout.addWidget(self.message)
        self.skeleton = BoardSkeleton(self.scroller.viewport())
        self.more_skeleton = BoardSkeleton(self.message, compact=True)
        self.controls = (self.search, self.jql, apply, self.refresh, self.group, self.board_order)
        self._restore({})

    def showEvent(self, event):
        super().showEvent(event)
        self._show_progress()

    def hideEvent(self, event):
        self.skeleton.stop()
        self.more_skeleton.stop()
        super().hideEvent(event)

    def reset(self):
        self.states.clear()
        self.connection_id = ""
        self.state = BoardState()
        self.generation += 1
        self.writing = False
        self._restore({})
        self.render()

    def invalidate(self, connection_id):
        self.states.pop(connection_id, None)
        if self.connection_id == connection_id:
            self.generation += 1
            self.connection_id = ""
            self.state = BoardState()
            self.writing = False
            self._restore({})
            self.render()

    def show_connection(self, *, changed_by_user=False, opened=False):
        connection = self.jira.current() or {}
        connection_id = connection.get("id") or ""
        if connection_id != self.connection_id:
            self.generation += 1
            self.connection_id = connection_id
            self.state = self.states.setdefault(connection_id, BoardState())
            self.state.loading = ""
            self.writing = False
            saved = self.jira.settings.integrations_state().get("board") or {}
            self._restore(saved.get(connection_id) or {})
        self.render()
        if (self.isVisible() and connection.get("token_set") and not self.writing
                and not self.state.loading
                and (changed_by_user or (opened and not self.state.request))):
            self.search_board()

    def _restore(self, saved):
        self.extra = saved.get("columns") or {}
        self.search.setText(saved.get("text") or "")
        for combo, name in ((self.projects, "project"), (self.statuses, "status")):
            combo.reset()
            combo.set_value(saved.get(name) or "")
        self.mine.setChecked(bool(saved.get("mine")))
        self.jql.setChecked(bool(saved.get("jql")))
        self.board_order.reset()
        self.board_order.set_value(saved.get("board_id") or "", saved.get("board_name"))
        self._fill_group(self.state.request.get("field") or saved.get("field") or "status")

    def _fill_group(self, selected):
        self.group.clear()
        fields = (self.state.fields.values() if self.state.fields
                  else ({"id": "status", "name": "Status"},))
        for spec in fields:
            self.group.addItem(spec["name"], spec["id"])
        index = self.group.findData(selected)
        if index < 0 and not self.state.fields and selected:
            self.group.addItem(selected, selected)
            index = self.group.count() - 1
        self.group.setCurrentIndex(index if index >= 0 else max(0, self.group.findData("status")))

    def _remember(self):
        if not self.connection_id:
            return
        settings = self.jira.settings
        state = dict(settings.integrations_state())
        boards = dict(state.get("board") or {})
        boards[self.connection_id] = {
            "text": self.search.text(), "jql": self.jql.isChecked(),
            "project": self.projects.value(), "status": self.statuses.value(),
            "mine": self.mine.isChecked(), "field": self.group.currentData(),
            "columns": self.extra,
            "board_id": self.board_order.value(), "board_name": self.board_order.currentText(),
        }
        settings.save_integrations_state(dict(state, board=boards))

    def _ready(self):
        return bool((self.jira.current() or {}).get("token_set")) and self.jira.core is not None

    def _controls(self):
        enabled = self._ready() and not self.writing and not self.state.loading
        for widget in self.controls:
            widget.setEnabled(enabled)
        for widget in (self.projects, self.statuses, self.mine):
            widget.setEnabled(enabled and not self.jql.isChecked())
        spec = self.state.fields.get(self.group.currentData()) or {}
        self.board_order.setVisible(self.group.currentData() == "status")
        source = self.state.source_board
        selected_board = self.board_order.value()
        tooltip = "Choose a Jira board to use its configured status order."
        if source:
            tooltip = "Using status order from %s." % source
        elif selected_board:
            tooltip = "Jira board order is unavailable. Using workflow order."
        self.board_order.setToolTip(tooltip)
        self.add_column.setEnabled(enabled and not self.state.loading and spec.get("editable", False)
                                   and spec["kind"] in {"number", "text", "date", "datetime", "user"}
                                   and (spec["kind"] != "user" or bool(self.state.rows)))
        self.more.setVisible(self.state.cursor is not None)
        self.more.setEnabled(enabled and not self.state.loading)

    def _jql_mode(self, checked):
        self.search.setPlaceholderText("Enter a JQL query" if checked else "Search issues")
        self._controls()

    def _call(self, operation, request, done, *, write=False):
        connection_id, generation = self.connection_id, self.generation

        def answered(payload):
            if generation != self.generation or connection_id != self.connection_id:
                return
            self.jira._heard(connection_id, payload)
            done(payload)

        self.jira._call("board-write" if write else "board:" + operation,
                        lambda core, cancel: core.integrations(
            "call", connection_id, operation, document=request, cancel=cancel), answered, quiet=True)

    def _filter_choices(self, operation, combo):
        result_key, value_key = {"projects": ("projects", "key"), "statuses": ("statuses", "name"),
                                 "board_catalog": ("boards", "id")}[operation]

        def done(payload):
            if payload.get("ok"):
                combo.set_choices([(item["name"], item[value_key])
                                   for item in payload["result"].get(result_key) or []])
            else:
                combo.failed()
                self._set_message(payload.get("error") or "Jira did not answer.")

        self._call(operation, {}, done)

    def _board_choices(self):
        self._filter_choices("board_catalog", self.board_order)

    def search_board(self, *_args):
        if not self._ready() or self.writing or self.state.loading:
            return
        request = {"field": self.group.currentData() or "status"}
        if request["field"] == "status" and self.board_order.value():
            request["board_id"] = self.board_order.value()
        if self.jql.isChecked():
            if not self.search.text().strip():
                self._set_message("Enter a JQL query.")
                return
            request["jql"] = self.search.text().strip()
        else:
            request.update(text=self.search.text().strip(), project=self.projects.value(),
                           status=self.statuses.value(), mine=self.mine.isChecked())
        self._remember()
        self._load(request)

    def load_more(self):
        if self.state.cursor is not None and not self.state.loading and not self.writing:
            self._load(dict(self.state.request, cursor=self.state.cursor,
                            known_contexts=self.state.contexts), append=True)

    def _load(self, request, append=False):
        self.state.loading = "more" if append else "board"
        self._controls()
        self._show_progress()

        def done(payload):
            state = self.state
            state.loading = ""
            state.move_details = ""
            if payload.get("ok"):
                self._take_page(payload["result"], request, append)
                state.message = "" if state.rows else "No issues match this Board."
                self._fill_group(state.request["field"])
                self._remember()
            else:
                self._fill_group(state.request.get("field") or request["field"])
                state.message = payload.get("error") or "Jira did not answer."
            self.render()

        self._call("board_search", request, done)

    def _take_page(self, result, request, append=False):
        state = self.state
        if not append or result["field"] != state.request.get("field"):
            state.choices, state.rows, state.contexts = {}, [], []
            state.status_order = {status: index for index, status in enumerate(result.get("status_order") or [])}
            state.source_board = result.get("source_board") or ""
            state.unverified.clear()
            if result["field"] != state.request.get("field"):
                state.selected.clear()
        by_key = {item["key"]: item for item in state.rows}
        by_key.update((item["key"], item) for item in result["rows"])
        state.rows = list(by_key.values())
        state.selected.intersection_update(by_key)
        state.choices.update((value_key(item["value"]), item) for item in result["choices"])
        state.contexts.extend(result["contexts"])
        state.fields = {item["id"]: item for item in result["fields"]}
        state.request = {name: value for name, value in request.items()
                         if name not in {"cursor", "known_contexts"}}
        state.request["field"] = result["field"]
        state.cursor = result["cursor"]

    def _set_message(self, message):
        self.message.setText(message)
        self.message.setVisible(bool(message))
        self.message.setToolTip(self.state.move_details if message == self.state.message else "")

    def _show_progress(self):
        connection = self.jira.current() or {}
        if not self.connection_id:
            message = "Add a Jira connection in Connections."
        elif not connection.get("token_set"):
            message = "Set the API token in Connections."
        elif self.writing:
            message = "Saving Jira issue…"
        elif self.state.loading == "more":
            message = "Loading more issues…"
        elif self.state.loading:
            message = "Loading Board…"
        else:
            message = self.state.message
            if not message and self.state.request.get("board_id") and not self.state.source_board:
                message = "Jira board order is unavailable. Using workflow order."
        self._set_message(message)
        for mode, skeleton in (("board", self.skeleton), ("more", self.more_skeleton)):
            if self.state.loading == mode and self.isVisible():
                skeleton.start()
                skeleton.raise_()
            else:
                skeleton.stop()

    def render(self):
        self._controls()
        scrollbar = self.scroller.horizontalScrollBar()
        position = scrollbar.value()
        previous = self.scroller.takeWidget()
        if previous is not None:
            previous.deleteLater()
        content = QWidget()
        columns = QHBoxLayout(content)
        if not self.state.request or not self._ready():
            columns.addStretch()
            self.scroller.setWidget(content)
            self._show_progress()
            return

        field_id = self.state.request["field"]
        spec = self.state.fields.get(field_id) or {}
        kind = spec.get("kind")
        groups, choices = {}, dict(self.state.choices)
        choices.update((value_key(item["value"]), item) for item in self.extra.get(field_id, []))
        choices[value_key(None)] = EMPTY
        for row in self.state.rows:
            group = row["group"]
            key = value_key(group["value"]) if group else ""
            groups.setdefault(key, []).append(row)
            if group:
                choice = choices.setdefault(key, group)
                if group.get("category") and not choice.get("category"):
                    choices[key] = dict(choice, category=group["category"])
            else:
                choices[key] = {"value": None, "label": "Unavailable"}
        ordered = sorted(choices.items(), key=lambda entry: column_order(kind, *entry, self.state.status_order))
        label_counts = {}
        for item in choices.values():
            label_counts[item["label"]] = label_counts.get(item["label"], 0) + 1
        columns.setAlignment(Qt.AlignLeft)
        for key, item in ordered:
            rows = groups.get(key, [])
            column = QWidget()
            column.setFixedWidth(CardDelegate.WIDTH + 24)
            vertical = QVBoxLayout(column)
            vertical.setContentsMargins(4, 2, 4, 2)
            vertical.setSpacing(8)
            label = item["label"]
            if label_counts[label] > 1:
                label = "%s · %s" % (label, item["value"])
            heading = widgets.elided_line(label, "lede")
            heading.setTextFormat(Qt.PlainText)
            font = heading.font()
            font.setBold(True)
            heading.setFont(font)
            count = QLabel(str(len(rows)))
            count.setProperty("role", "mono")
            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 12, 0)
            if kind == "status" and item.get("category"):
                header.addWidget(ColumnIndicator("status", item["category"]))
            elif kind == "user" and item["value"]:
                header.addWidget(ColumnIndicator("user", item["value"]))
            header.addWidget(heading, 1)
            header.addWidget(count)
            vertical.addLayout(header)
            cards = Column(self, item["value"], bool(spec.get("editable")) and bool(key)
                           and not (key == "null" and spec.get("kind") == "status")
                           and not self.writing and not self.state.loading)
            cards.setAccessibleName(label)
            for row in rows:
                card = QListWidgetItem("%s\n%s" % (row["key"], row["summary"]))
                card.setData(Qt.UserRole, row["key"])
                details = {name: row.get(name) for name in
                           ("assignee", "assignee_id", "priority", "blocked", "story_points", "due_date")}
                card.setData(DETAILS_ROLE, details)
                card.setToolTip(self._card_tooltip(row, details))
                cards.addItem(card)
                card.setData(SELECTED_ROLE, row["key"] in self.state.selected)
            vertical.addWidget(cards, 1)
            columns.addWidget(column)
        self.scroller.setWidget(content)
        scrollbar.setValue(position)
        self._show_progress()

    @staticmethod
    def _card_tooltip(row, details):
        parts = [row["key"], row["summary"], details["assignee"] or "Unassigned"]
        if details["priority"]:
            parts.append("Priority " + details["priority"])
        if details["blocked"]:
            parts.append("Blocker")
        if details["story_points"] is not None:
            parts.append(f'Story Points {details["story_points"]}')
        if details["due_date"]:
            parts.append("Due " + details["due_date"])
        return "<p>%s</p>" % "<br>".join(escape(part) for part in parts)

    def open_card(self, key):
        if self.writing or self.state.loading:
            return
        for row in self.state.rows:
            if row["key"] == key:
                self.jira.open_board_card(row)
                return

    def _add_column(self):
        field_id = self.state.request["field"]
        spec = self.state.fields[field_id]
        if spec["kind"] == "user":
            self._find_user(field_id)
            return
        label = spec["name"]
        hint = INPUT_HINTS.get(spec["kind"])
        if hint:
            label += " (%s)" % hint
        raw, accepted = QInputDialog.getText(self, "Add column", label)
        if not accepted:
            return
        try:
            value = input_scalar(raw.strip(), spec)
        except (ValueError, OverflowError):
            self._set_message("Enter a valid value for %s." % spec["name"])
            return
        self._save_column(field_id, {"value": value, "label": str(value)})

    def _find_user(self, field_id):
        rows = self.state.rows
        issue = rows[0]["key"]
        if field_id == "assignee" and len(rows) > 1:
            labels = ["%s · %s" % (row["key"], row["summary"]) for row in rows]
            chosen, accepted = QInputDialog.getItem(self, "Add assignee column", "Issue to assign",
                                                    labels, editable=False)
            if not accepted:
                return
            issue = rows[labels.index(chosen)]["key"]
        text, accepted = QInputDialog.getText(self, "Add user column", "Find a Jira user")
        if not accepted or not text.strip():
            return

        def done(payload):
            if (field_id != self.state.request.get("field") or not self.isVisible()
                    or self.state.loading or self.writing):
                return
            choices = (payload.get("result") or {}).get("choices") or []
            if not choices:
                self._set_message(payload.get("error") or "No matching Jira users.")
                return
            labels = ["%s · %s" % (item["label"], item["value"]) for item in choices]
            chosen, accepted = QInputDialog.getItem(self, "Add user column", "Jira user",
                                                    labels, editable=False)
            if accepted:
                self._save_column(field_id, choices[labels.index(chosen)])

        self._call("board_people", {"key": issue, "field": field_id, "query": text.strip()}, done)

    def _save_column(self, field_id, item):
        values = self.extra.setdefault(field_id, [])
        key = value_key(item["value"])
        if not any(value_key(old["value"]) == key for old in values):
            values.append(item)
        self._remember()
        self.render()

    def select_card(self, key, toggle):
        selected = self.state.selected
        if key is None:
            if not toggle:
                selected.clear()
        elif toggle:
            selected.symmetric_difference_update({key})
        elif key not in selected:
            selected.clear()
            selected.add(key)
        for item in self._card_items():
            item.setData(SELECTED_ROLE, item.data(Qt.UserRole) in selected)

    def _card_items(self):
        for column in self.scroller.widget().findChildren(Column):
            for index in range(column.count()):
                yield column.item(index)

    def _mark_card_saving(self, key, saving):
        for card in self._card_items():
            if card.data(Qt.UserRole) == key:
                card.setData(SAVING_ROLE, saving)
                break

    def move_card(self, key, target):
        state = self.state
        if self.writing or state.loading:
            return
        keys = state.selected if key in state.selected else {key}
        rows = [row for row in state.rows if row["key"] in keys
                and (row["group"] is None or value_key(row["group"]["value"]) != value_key(target))]
        if not rows:
            return

        self.writing = True
        self._controls()
        generation = self.generation
        pending = iter(enumerate(rows, 1))
        affected, failures = [], []
        moved = 0

        def advance():
            if generation != self.generation or self.jira._closing:
                return
            task = next(pending, None)
            if task is None:
                self.jira._activity_changed()
                message = self._move_summary(moved, len(rows), failures)
                if affected:
                    self._reconcile(affected, message, failures)
                else:
                    self._finish(message, failures)
                return
            position, row = task
            issue = row["key"]
            if issue in state.unverified:
                fail(issue, position, "Refresh the Board before moving this card again.")
                return
            if row["group"] is None:
                fail(issue, position, "Jira did not return the current value for this card.")
                return
            progress = "Moving %s (%d/%d)…" % (issue, position, len(rows))
            self._set_message(progress)
            self._mark_card_saving(issue, True)
            request = {"key": issue, "field": state.request["field"], "value": target,
                       "from": row["group"]["value"], "updated": row["updated"]}

            def completed(payload, needs_reload):
                nonlocal moved
                self._mark_card_saving(issue, False)
                if needs_reload:
                    affected.append(row)
                if payload.get("ok"):
                    moved += 1
                    issue_detail = (payload.get("result") or {}).get("issue")
                    if issue_detail:
                        self.jira._take_board_change(issue_detail)
                    else:
                        self.jira._forget_board_card(issue)
                    state.selected.discard(issue)
                else:
                    if needs_reload:
                        self.jira._forget_board_card(issue)
                    failures.append((issue, payload.get("error") or "Jira did not take the change."))
                next_task(position)

            self._write(request, completed, progress)

        def fail(issue, position, error):
            failures.append((issue, error))
            next_task(position)

        def next_task(position):
            if position < len(rows):
                QTimer.singleShot(0, self, advance)
            else:
                advance()

        advance()

    @staticmethod
    def _move_summary(moved, total, failures):
        if total == 1:
            return failures[0][1] if failures else ""
        summary = "Moved %d of %d issues." % (moved, total)
        if failures:
            summary += " " + "  ".join("%s · %s" % item for item in failures[:2])
            if len(failures) > 2:
                summary += "  +%d more. Hover for details." % (len(failures) - 2)
        return summary

    def _write(self, request, complete, progress, attempt=1):
        generation = self.generation

        def done(payload):
            kind = payload.get("kind")
            if kind == "transitions":
                choices = payload["choices"]
                labels = ["%s · %s" % (item["name"], item["id"]) for item in choices]
                chosen, accepted = QInputDialog.getItem(self, "Move " + request["key"], "Transition",
                                                        labels, editable=False)
                if accepted:
                    self._write(dict(request, transition=choices[labels.index(chosen)]["id"]),
                                complete, progress, attempt)
                    return
            elif kind == "fields":
                dialog = TransitionFieldsDialog(self, payload["transition"], payload.get("needs") or [])
                accepted = dialog.exec() == QDialog.Accepted
                fields = dialog.fields() if accepted else None
                dialog.deleteLater()
                if accepted:
                    self._write(dict(request, transition=payload["transition_id"], fields=fields),
                                complete, progress, attempt)
                    return
            else:
                self._move_answer(request, payload, complete, progress, attempt, generation)
                return
            complete({"error": "Move canceled."}, False)

        self._call("board_move", request, done, write=True)

    def _move_answer(self, request, payload, complete, progress, attempt, generation):
        kind = payload.get("kind")
        status = payload.get("status")
        uncertain = (kind == "unknown_outcome" or (kind == "error" and status is None))
        confirming = request.get("confirm_only") or uncertain
        transient_http = status in (408, 429) or (isinstance(status, int) and 500 <= status < 600)
        retryable = uncertain or kind in {"unreachable", "busy"} or transient_http
        if retryable and attempt < MOVE_ATTEMPTS:
            # A lost write response can hide a completed mutation or a request still
            # in flight. Retrying it can run workflow side effects a second time.
            retry_request = dict(request, confirm_only=True) if confirming else request
            note = "Checking Jira" if confirming else "Retrying"
            self._set_message("%s  %s (%d/%d)…" % (progress, note, attempt + 1, MOVE_ATTEMPTS))

            def retry():
                if generation == self.generation and not self.jira._closing:
                    self._write(retry_request, complete, progress, attempt + 1)

            QTimer.singleShot(MOVE_RETRY_DELAY_MS * attempt, self, retry)
            return
        if confirming and not payload.get("ok"):
            payload = dict(payload, error="Change unconfirmed. " + (payload.get("error") or "Jira did not answer."))
        complete(payload, bool(payload.get("ok") or confirming or kind == "conflict"))

    def _reconcile(self, rows, message, failures):
        def done(payload):
            if payload.get("ok"):
                self._take_page(payload["result"], self.state.request)
                self._fill_group(self.state.request["field"])
            else:
                error = payload.get("error") or "Could not reload the Board."
                message_text = "%s %s Refresh the Board before moving these cards again." % (message, error)
                self.state.unverified.update(row["key"] for row in rows)
                self._finish(message_text.strip(), failures)
                return
            self._finish(message or ("" if self.state.rows else "No issues match this Board."), failures)

        self._call("board_reload", dict(self.state.request, size=len(self.state.rows),
                                       issue_ids=[row["issue_id"] for row in rows]), done)

    def _finish(self, message, failures=()):
        self.writing = False
        self.state.message = message
        self.state.move_details = ("<p>%s</p>" % "<br>".join(escape("%s · %s" % item) for item in failures)
                                   if failures else "")
        self.render()
