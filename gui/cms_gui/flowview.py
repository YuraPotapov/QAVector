"""A scenario's run as a storyboard: one card per step, with its screenshot.

The Artifacts page shows a run as files - ``result.json`` and a folder of
``screenshot_NNN.png`` - and reading which picture belongs to which step meant
holding the JSON in one hand and counting files with the other. This lays them
side by side instead: a card per step, in order, each with what the step did,
how it went, and the page as it looked straight after it, with an arrow to the
next. A failed step is outlined and says why.

It is the same report, not a new one: the cards are read from the scenario's
folder as it is on disk - ``result.json`` for the steps, and the reporter's own
file names for the pictures (``screenshot_start.png`` before the first step,
``screenshot_004.png`` after step index 3, ``screenshot_finish.png`` - or the
older ``screenshot.png`` - for the step a failed run stopped on). A step with no
picture still gets its card.

Pictures are scaled the first time a card is shown, so a folder of dozens of
them opens at once, and a card shows the top of the page - the reporter takes
the whole of it. Clicking one asks the page to show that file whole.
"""

import json
import os

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtWidgets import (QFrame, QLabel, QLayout, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import theme

#: A card's width and its picture's largest size. Wide enough to read an Odoo
#: form at a glance, narrow enough for three or four to a row.
CARD_WIDTH = 280
THUMB_WIDTH = CARD_WIDTH - 22
THUMB_HEIGHT = 170
#: The gap an arrow stands in, between two cards.
ARROW_WIDTH = 30
#: How much of a failure message a card shows; the rest is in result.json.
MESSAGE_CHARS = 280
MESSAGE_LINES = 6

#: Statuses the stylesheet tells apart (QFrame[role="flowcard"][status=...]).
PASS, FAIL, ERROR, START = "pass", "fail", "error", "start"


def nodes(folder):
    """The cards a scenario folder makes, in order, or None without result.json.

    Each is ``{"title", "detail", "status", "message", "meta", "shot"}``;
    ``shot`` is a picture's path or "".
    """
    try:
        with open(os.path.join(folder, "result.json"), encoding="utf-8") as handle:
            result = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(result, dict):
        return None

    def picture(name):
        path = os.path.join(folder, name)
        return path if os.path.isfile(path) else ""

    found, used = [], set()
    start = picture("screenshot_start.png")
    if start:
        found.append({"title": "Start", "detail": result.get("session") or "",
                      "status": START, "message": "", "meta": "", "shot": start})
    for step in result.get("steps") or []:
        if not isinstance(step, dict):
            continue
        index = int(step.get("index") or 0)
        status = str(step.get("status") or "")
        shot = picture("screenshot_%03d.png" % (index + 1))
        if not shot and status != PASS:
            # The step a run stopped on: its page is the finish picture.
            shot = picture("screenshot_finish.png") or picture("screenshot.png")
        used.add(shot)
        found.append({
            "title": "%d  %s" % (index + 1, step.get("action") or ""),
            "detail": _detail(step),
            "status": status,
            "message": _short(step.get("message")) if status != PASS else "",
            "meta": _meta(step),
            "shot": shot,
        })
    finish = picture("screenshot_finish.png")
    if finish and finish not in used:
        found.append({"title": "Finish", "detail": str(result.get("status") or ""),
                      "status": PASS if result.get("status") == PASS else FAIL,
                      "message": "", "meta": "", "shot": finish})
    return found


def _short(message):
    """The first lines of a failure, enough to say what went wrong on a card."""
    lines = [one.strip() for one in str(message or "").splitlines() if one.strip()]
    return "\n".join(lines[:MESSAGE_LINES])[:MESSAGE_CHARS]


def _detail(step):
    """What the step acted on, in one line."""
    target, value = step.get("target"), step.get("value")
    if target and value:
        return "%s = %s" % (target, value)
    return str(target or value or "")


def _meta(step):
    parts = []
    try:
        parts.append("%.1f s" % (float(step.get("duration_ms") or 0) / 1000.0))
    except (TypeError, ValueError):
        pass
    attempts = int(step.get("attempts") or 1)
    if attempts > 1:
        parts.append("%d attempts" % attempts)
    return "  ".join(parts)


class FlowView(QScrollArea):
    """The storyboard, in a scroll area of its own."""

    #: A card's picture was clicked: the path of the file to show whole.
    picked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self._board = QWidget()
        self._layout = _FlowLayout(self._board, spacing=0, margin=16)
        self.setWidget(self._board)
        self.cards = []

    def show_folder(self, folder):
        """Lay out ``folder``'s cards. False when it holds no result.json."""
        found = nodes(folder)
        self._clear()
        if found is None:
            return False
        for position, node in enumerate(found):
            if position:
                self._layout.addWidget(_Arrow())
            card = _Card(node)
            card.picked.connect(self.picked.emit)
            self._layout.addWidget(card)
            self.cards.append(card)
        if not found:
            empty = QLabel("This run has no steps to show.")
            empty.setProperty("role", "muted")
            self._layout.addWidget(empty)
        return True

    def _clear(self):
        self.cards = []
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()


class _Card(QFrame):
    """One step: what it did, its picture, how it went."""

    picked = Signal(str)

    def __init__(self, node, parent=None):
        super().__init__(parent)
        self.node = node
        self.setProperty("role", "flowcard")
        self.setProperty("status", node["status"])
        self.setFixedWidth(CARD_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Maximum)
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 8, 8, 8)
        column.setSpacing(4)

        title = QLabel(node["title"])
        title.setProperty("role", "flowtitle")
        column.addWidget(title)
        if node["detail"]:
            detail = QLabel()
            detail.setProperty("role", "mono")
            detail.setToolTip(node["detail"])
            detail.setText(detail.fontMetrics().elidedText(
                node["detail"], Qt.ElideMiddle, THUMB_WIDTH))
            column.addWidget(detail)

        self.thumb = QLabel("no screenshot")
        self.thumb.setProperty("role", "flowthumb")
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setFixedSize(THUMB_WIDTH, THUMB_HEIGHT)
        if node["shot"]:
            self.thumb.setCursor(Qt.PointingHandCursor)
            self.thumb.setToolTip("Show %s whole" % os.path.basename(node["shot"]))
            self.thumb.mousePressEvent = self._clicked
        column.addWidget(self.thumb)
        self._loaded = False

        if node["message"]:
            message = QLabel(node["message"])
            message.setProperty("role", "error")
            message.setWordWrap(True)
            # A width of its own, so the card's layout knows how tall the
            # wrapped text is before anything is drawn.
            message.setFixedWidth(THUMB_WIDTH)
            column.addWidget(message)
        if node["meta"]:
            meta = QLabel(node["meta"])
            meta.setProperty("role", "hint")
            column.addWidget(meta)

    def showEvent(self, event):              # noqa: N802 - Qt's own spelling
        if not self._loaded and self.node["shot"]:
            self._loaded = True
            pixmap = QPixmap(self.node["shot"])
            if not pixmap.isNull():
                # The top of the page, the width of the card: the reporter takes
                # the whole page, and a long one squeezed into the box was a
                # sliver nobody could read. The top is where a step's result
                # shows - the menu, the view, the dialog - and a click shows all.
                wide = pixmap.scaledToWidth(THUMB_WIDTH - 2, Qt.SmoothTransformation)
                self.thumb.setText("")
                self.thumb.setPixmap(wide.copy(0, 0, wide.width(),
                                               min(wide.height(), THUMB_HEIGHT - 2)))
        QFrame.showEvent(self, event)

    def _clicked(self, event):
        if event.button() == Qt.LeftButton:
            self.picked.emit(self.node["shot"])


class _Arrow(QWidget):
    """The arrow from one card to the next, drawn - not a glyph."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(ARROW_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def sizeHint(self):
        return QSize(ARROW_WIDTH, 60)

    def paintEvent(self, _event):            # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        colour = QColor(theme.NEUTRAL[500])      # read now: dark mode rewrites theme
        middle = min(self.height() // 2, 110)
        painter.setPen(QPen(colour, 2))
        painter.drawLine(4, middle, ARROW_WIDTH - 9, middle)
        painter.setPen(Qt.NoPen)
        painter.setBrush(colour)
        painter.drawPolygon(QPolygon([QPoint(ARROW_WIDTH - 4, middle),
                                      QPoint(ARROW_WIDTH - 11, middle - 5),
                                      QPoint(ARROW_WIDTH - 11, middle + 5)]))
        painter.end()


class _FlowLayout(QLayout):
    """Widgets left to right, wrapping onto the next row, rows top-aligned."""

    def __init__(self, parent=None, spacing=0, margin=0):
        super().__init__(parent)
        self._items = []
        self._gap = spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):                  # noqa: N802
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):                  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):                  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):            # noqa: N802
        return Qt.Orientations(0)

    def hasHeightForWidth(self):              # noqa: N802
        return True

    def heightForWidth(self, width):          # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect):              # noqa: N802
        QLayout.setGeometry(self, rect)
        self._arrange(rect, move=True)

    def sizeHint(self):                       # noqa: N802
        return self.minimumSize()

    def minimumSize(self):                    # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _arrange(self, rect, move):
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        x, y, row_height, row = area.x(), area.y(), 0, []
        for item in self._items:
            hint = item.sizeHint()
            # A card with a wrapped message is taller than its size hint says;
            # its minimum, and its height at its own width, tell the truth.
            tall = max(hint.height(), item.minimumSize().height())
            if item.hasHeightForWidth():
                tall = max(tall, item.heightForWidth(hint.width()))
            hint = QSize(hint.width(), tall)
            if row and x + hint.width() > area.right() + 1:
                self._place_row(row, row_height, move)
                x, y, row_height, row = area.x(), y + row_height + 18, 0, []
            row.append((item, x, y, hint))
            x += hint.width() + self._gap
            row_height = max(row_height, hint.height())
        self._place_row(row, row_height, move)
        return y + row_height - rect.y() + margins.bottom()

    @staticmethod
    def _place_row(row, height, move):
        if not move:
            return
        for item, x, y, hint in row:
            # Arrows stretch to the row so they point at the cards' middles.
            tall = height if isinstance(item.widget(), _Arrow) else hint.height()
            item.setGeometry(QRect(QPoint(x, y), QSize(hint.width(), tall)))
