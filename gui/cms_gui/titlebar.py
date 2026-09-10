"""The window's own title bar, in place of the one the desktop draws.

The desktop's frame is the one part of the window the design never reached:
GNOME paints it in its own grey with the window's title in the system
font, Windows in its own colours - a default the moment the window opens, above
everything that is not. So the frame is switched off and this is drawn instead:
the mark and the name in the design's type, on the icon's own slate, and the
three window controls.

What the frame did besides decorate has to be given back, and is here too:

- moving: a drag on the bar hands the move to the window manager
  (``QWindow.startSystemMove``), which is what keeps edge tiling, snapping and
  drag-to-maximise working - a window that moved itself would lose all three;
- resizing: thin grips along every edge and corner, handed over the same way;
- an edge: a frameless window on a desktop of its own colour has none, so the
  grips draw a hairline where the frame was.

Only the head of the window changes. The menus, the toolbar and everything under
them are the window's own layout, left exactly as built and pushed down by the
bar's height through the window's contents margins - not rebuilt around it.

``CMS_SYSTEM_FRAME=1`` keeps the desktop's frame: a tiling window manager, or
one that will not take a move from a client, is better off with its own.
"""

import os

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QApplication, QFrame, QHBoxLayout,
                               QLabel, QWidget)

from . import icon, theme
from . import version as gui_version

#: Set to anything but 0/false/no/off to keep the desktop's own frame.
SYSTEM_FRAME_ENV = "CMS_SYSTEM_FRAME"

#: Height of the bar, in px - about what the desktop's own was, so the window
#: under it does not change height by being given a different head.
HEIGHT = 36

#: The window controls fill the bar top to bottom and are wider than tall: they
#: are hit targets in the corner a pointer gets thrown at.
BUTTON_WIDTH = 46

#: The glyph inside a window control, in px.
GLYPH = 10

#: The application mark at the head of the bar, in px.
MARK = 18

#: How far in from the edge a press resizes rather than clicks, and how far the
#: corner grips reach along each edge.
GRIP = 4
CORNER = 10

#: The hairline round the window where the desktop's frame was, in px. The
#: layout is inset by this much, so the line never covers the window's contents.
EDGE = 1

NAME = "QAVECTOR"


def wanted(environ=None):
    """Whether to draw this bar rather than keep the desktop's frame."""
    value = (os.environ if environ is None else environ).get(SYSTEM_FRAME_ENV, "")
    return value.strip().lower() in ("", "0", "false", "no", "off")


def install(window):
    """Take the desktop's frame off ``window`` and put this one on. Call once."""
    return Frame(window)


class WindowButton(QAbstractButton):
    """Minimize, maximize/restore or close, painted at the pixel.

    Not a mark from icons.py: those are strokes on a 24-unit grid scaled to the
    size asked for, which at 10px lands a line across two pixel rows and comes
    out grey. These are laid on the half-pixel grid instead, which is the one way
    to get the hairline the rest of the design is made of.
    """

    TIPS = {"minimize": "Minimize", "maximize": "Maximize",
            "restore": "Restore", "close": "Close"}

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(BUTTON_WIDTH, HEIGHT)
        self.setFocusPolicy(Qt.NoFocus)
        # Repaint on enter and leave: the hover fill is the only sign these are
        # buttons at all.
        self.setAttribute(Qt.WA_Hover, True)
        self.set_kind(kind)

    def set_kind(self, kind):
        self.kind = kind
        self.setToolTip(self.TIPS[kind])
        self.update()

    def sizeHint(self):
        return QSize(BUTTON_WIDTH, HEIGHT)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if self.isDown() or self.underMouse():
            # Close goes red on hover, as it does on every desktop: it is the one
            # of the three that loses something if it was not meant.
            if self.kind == "close":
                fill = theme.BAD
            else:
                fill = theme.TITLEBAR_PRESSED if self.isDown() else theme.TITLEBAR_HOVER
            painter.fillRect(self.rect(), QColor(fill))

        pen = QPen(QColor(theme.TITLEBAR_TEXT))
        pen.setWidthF(1.0)
        pen.setCapStyle(Qt.SquareCap)
        pen.setJoinStyle(Qt.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        left = (self.width() - GLYPH) // 2 + 0.5
        top = (self.height() - GLYPH) // 2 + 0.5
        span = GLYPH - 1
        if self.kind == "minimize":
            middle = top + span // 2
            painter.drawLine(QPointF(left, middle), QPointF(left + span, middle))
        elif self.kind == "maximize":
            painter.drawRect(QRectF(left, top, span, span))
        elif self.kind == "restore":
            # Two windows, the front one whole and the back one only where it
            # shows - the same picture every desktop uses for "not maximized".
            back = 2
            painter.drawRect(QRectF(left, top + back, span - back, span - back))
            painter.drawPolyline([QPointF(left + back, top + back),
                                  QPointF(left + back, top),
                                  QPointF(left + span, top),
                                  QPointF(left + span, top + span - back),
                                  QPointF(left + span - back, top + span - back)])
        else:
            painter.drawLine(QPointF(left, top), QPointF(left + span, top + span))
            painter.drawLine(QPointF(left + span, top), QPointF(left, top + span))


class TitleBar(QFrame):
    """The bar itself: the mark, the name, and the window controls.

    Anywhere that is not a control moves the window, and a double click there
    maximizes it - what the desktop's bar did, so a hand that knows that one
    knows this one.
    """

    def __init__(self, window):
        super().__init__(window)
        self.setProperty("role", "titlebar")
        self._press = None          # where a drag began, until it is handed on
        self._origin = None         # where the window was when it did
        self._dragging = False

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 0, 0)
        row.setSpacing(0)
        mark = QLabel()
        mark.setPixmap(icon.app_icon().pixmap(MARK, MARK))
        self.name = QLabel(NAME)
        self.name.setProperty("role", "brand")
        self.version = QLabel(gui_version())
        self.version.setProperty("role", "brandtag")
        row.addWidget(mark)
        row.addSpacing(10)
        row.addWidget(self.name)
        row.addSpacing(10)
        row.addWidget(self.version)
        row.addStretch(1)

        self.minimize_button = WindowButton("minimize", self)
        self.minimize_button.clicked.connect(lambda: self.window().showMinimized())
        self.maximize_button = WindowButton("maximize", self)
        self.maximize_button.clicked.connect(self.toggle_maximized)
        self.close_button = WindowButton("close", self)
        self.close_button.clicked.connect(lambda: self.window().close())
        for button in (self.minimize_button, self.maximize_button, self.close_button):
            row.addWidget(button)

    def toggle_maximized(self):
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()

    # -- moving ---------------------------------------------------------------
    # The move is handed over on the first motion past the drag distance rather
    # than on the press: a press given away to the window manager never comes
    # back as a release, and the double click that maximizes needs it to.
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._press = event.globalPosition().toPoint()
        self._origin = self.window().pos()
        self._dragging = False
        event.accept()

    def mouseMoveEvent(self, event):
        if self._press is None or not event.buttons() & Qt.LeftButton:
            super().mouseMoveEvent(event)
            return
        window = self.window()
        here = event.globalPosition().toPoint()
        if not self._dragging:
            if (here - self._press).manhattanLength() < QApplication.startDragDistance():
                return
            self._dragging = True
            handle = window.windowHandle()
            if handle is not None and handle.startSystemMove():
                self._press = None          # the window manager has it now
                return
        # A platform that will not take the move: carry the window by hand. Not
        # a maximized one - there is nowhere for it to go.
        if not (window.isMaximized() or window.isFullScreen()):
            window.move(self._origin + here - self._press)

    def mouseReleaseEvent(self, event):
        self._press = None
        self._dragging = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def _cursor(edges):
    left_or_right = bool(edges & (Qt.LeftEdge | Qt.RightEdge))
    top_or_bottom = bool(edges & (Qt.TopEdge | Qt.BottomEdge))
    if left_or_right and top_or_bottom:
        falling = edges in (Qt.TopEdge | Qt.LeftEdge, Qt.BottomEdge | Qt.RightEdge)
        return Qt.SizeFDiagCursor if falling else Qt.SizeBDiagCursor
    return Qt.SizeHorCursor if left_or_right else Qt.SizeVerCursor


class Grip(QWidget):
    """One edge or corner of the window: a resize handle, and the hairline on it.

    Transparent apart from that line - a QWidget subclass gets no stylesheet
    background of its own, so what is under it shows through - and raised over
    the window's contents, which it overlaps by ``GRIP`` px so there is something
    to take hold of.
    """

    def __init__(self, window, edges):
        super().__init__(window)
        self.edges = edges
        self.setCursor(_cursor(edges))
        self._press = None
        self._start = None

    def place(self, width, height):
        left, right = bool(self.edges & Qt.LeftEdge), bool(self.edges & Qt.RightEdge)
        top, bottom = bool(self.edges & Qt.TopEdge), bool(self.edges & Qt.BottomEdge)
        if (left or right) and (top or bottom):
            w = h = CORNER
        elif left or right:
            w, h = GRIP, max(0, height - 2 * CORNER)
        else:
            w, h = max(0, width - 2 * CORNER), GRIP
        x = 0 if left else (width - w if right else CORNER)
        y = 0 if top else (height - h if bottom else CORNER)
        self.setGeometry(x, y, w, h)

    def paintEvent(self, _event):
        painter = QPainter(self)
        ink = QColor(theme.WINDOW_EDGE)
        if self.edges & Qt.LeftEdge:
            painter.fillRect(0, 0, EDGE, self.height(), ink)
        if self.edges & Qt.RightEdge:
            painter.fillRect(self.width() - EDGE, 0, EDGE, self.height(), ink)
        if self.edges & Qt.TopEdge:
            painter.fillRect(0, 0, self.width(), EDGE, ink)
        if self.edges & Qt.BottomEdge:
            painter.fillRect(0, self.height() - EDGE, self.width(), EDGE, ink)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        handle = self.window().windowHandle()
        if handle is not None and handle.startSystemResize(self.edges):
            return
        self._press = event.globalPosition().toPoint()
        self._start = self.window().geometry()

    def mouseMoveEvent(self, event):
        if self._press is None:
            super().mouseMoveEvent(event)
            return
        window = self.window()
        moved = event.globalPosition().toPoint() - self._press
        least = window.minimumSizeHint().expandedTo(window.minimumSize())
        rect = QRect(self._start)
        if self.edges & Qt.LeftEdge:
            rect.setLeft(min(rect.left() + moved.x(), rect.right() + 1 - least.width()))
        if self.edges & Qt.RightEdge:
            rect.setRight(max(rect.right() + moved.x(), rect.left() - 1 + least.width()))
        if self.edges & Qt.TopEdge:
            rect.setTop(min(rect.top() + moved.y(), rect.bottom() + 1 - least.height()))
        if self.edges & Qt.BottomEdge:
            rect.setBottom(max(rect.bottom() + moved.y(), rect.top() - 1 + least.height()))
        window.setGeometry(rect)

    def mouseReleaseEvent(self, event):
        self._press = None
        super().mouseReleaseEvent(event)


#: The eight grips: four corners, four edges.
GRIP_EDGES = (Qt.TopEdge | Qt.LeftEdge, Qt.TopEdge | Qt.RightEdge,
              Qt.BottomEdge | Qt.LeftEdge, Qt.BottomEdge | Qt.RightEdge,
              Qt.TopEdge, Qt.BottomEdge, Qt.LeftEdge, Qt.RightEdge)


class Frame(QObject):
    """The frameless window's frame: the bar on top, the grips round the edge.

    Kept in step with the window from its own events rather than by the window
    calling in: the frame is a fitting on the window, and the window should not
    have to know it is wearing one.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.bar = TitleBar(window)
        self.grips = [Grip(window, edges) for edges in GRIP_EDGES]
        window.setWindowFlag(Qt.FramelessWindowHint, True)
        window.installEventFilter(self)
        self.sync()

    def framed(self):
        """Whether there is an edge to draw and to resize by: not when maximized."""
        return not self.window.windowState() & (Qt.WindowMaximized | Qt.WindowFullScreen)

    def sync(self):
        framed = self.framed()
        edge = EDGE if framed else 0
        # The layout's own space, moved down by the bar and in by the edge.
        # Qt skips this when nothing changed, so it is safe on every resize.
        self.window.setContentsMargins(edge, edge + HEIGHT, edge, edge)
        width, height = self.window.width(), self.window.height()
        self.bar.setGeometry(edge, edge, width - 2 * edge, HEIGHT)
        self.bar.raise_()
        self.bar.maximize_button.set_kind("maximize" if framed else "restore")
        for grip in self.grips:
            grip.setVisible(framed)
            grip.place(width, height)
            grip.raise_()

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() in (
                QEvent.Resize, QEvent.WindowStateChange, QEvent.Show):
            self.sync()
        return False
