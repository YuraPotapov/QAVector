"""What is pinned to the sidebar: one mark per link, above the bottom group.

It knows links, not integrations: a link is what a provider handed out
(``provider``, ``target``) with the ``title`` and ``mark`` to show, and it is
opened by whoever made the rail - the Integrations page, the way its own Open
does. A new integration needs nothing here.

The rail is as tall as the window lets it be, so the group shows what fits and
puts the rest behind one "+N" entry. How much fits is worked out when the rail
changes size or what it holds changes - never on a timer.
"""

import weakref

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from . import brandmarks, icons, widgets

#: The side of a mark on the rail, in pixels.
MARK = 18


def mark_icon(name, size=MARK):
    """A maker's mark as an icon, painted sharp at the screen's own density."""
    screen = QApplication.primaryScreen()
    ratio = screen.devicePixelRatio() if screen is not None else 1.0
    canvas = QPixmap(round(size * ratio), round(size * ratio))
    canvas.setDevicePixelRatio(ratio)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    brandmarks.paint(painter, name, QRectF(0, 0, size, size))
    painter.end()
    return QIcon(canvas)


class PinnedRail(QWidget):
    """The pinned links, as rail entries; those that do not fit, behind "+N"."""

    #: A link was picked: open it.
    opened = Signal(dict)
    #: Unpin was picked on a link.
    unpinned = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("seethrough", True)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.links = []
        self.buttons = []
        self.collapsed = False
        self.room = None
        self.more = icons.button(self._entry(), "more")
        self.more.setObjectName("pinnedMore")
        self.more.setPopupMode(QToolButton.InstantPopup)
        self.more.setProperty("menuglyph", "false")
        self.more.setAccessibleName("More pinned")
        self.more_menu = QMenu(self.more)
        self.more.setMenu(self.more_menu)
        self.more.setVisible(False)
        self._layout.addWidget(self.more)
        self.setVisible(False)

    def _entry(self):
        button = widgets.NavButton(0)
        button.setProperty("variant", "nav")
        button.setCursor(Qt.PointingHandCursor)
        button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button.setIconSize(QSize(MARK, MARK))
        return button

    # -- what it holds ----------------------------------------------------------
    def set_links(self, links):
        for button in self.buttons:
            # Disconnected first: a deleted button's slots must not be what
            # holds anything else alive (gui-suite-known-failures).
            button.clicked.disconnect()
            button.customContextMenuRequested.disconnect()
            self._layout.removeWidget(button)
            button.hide()
            button.deleteLater()
        self.links = [dict(one) for one in links]
        self.buttons = []
        for index, link in enumerate(self.links):
            button = self._entry()
            button.setIcon(mark_icon(link.get("mark") or link.get("provider")))
            button.setText(link.get("title") or link.get("provider"))
            button.setToolTip(link.get("title") or link.get("provider"))
            button.setAccessibleName(link.get("title") or link.get("provider"))
            button.setContextMenuPolicy(Qt.CustomContextMenu)
            button.clicked.connect(lambda _c=False, link=link: self.opened.emit(dict(link)))
            button.customContextMenuRequested.connect(
                lambda point, button=button, link=link: self._menu(button, link, point))
            self._layout.insertWidget(index, button)
            self.buttons.append(button)
        self.set_collapsed(self.collapsed)
        self.fit(self.room)

    def _menu(self, button, link, point):
        menu = QMenu(button)
        menu.addAction("Open", lambda: self.opened.emit(dict(link)))
        menu.addAction("Unpin", lambda: self.unpinned.emit(dict(link)))
        menu.popup(button.mapToGlobal(point))

    def set_collapsed(self, collapsed):
        """Marks only, as the rest of the rail when it is folded."""
        self.collapsed = bool(collapsed)
        for button in self.buttons + [self.more]:
            button.setToolButtonStyle(Qt.ToolButtonIconOnly if self.collapsed
                                      else Qt.ToolButtonTextBesideIcon)
            button.set_compact(self.collapsed)
        self._say_more()

    # -- what fits --------------------------------------------------------------
    def row_height(self):
        probe = self.buttons[0] if self.buttons else self.more
        return max(1, probe.sizeHint().height())

    def fit(self, room):
        """Show as many links as ``room`` pixels hold, the rest behind "+N".
        None is room for all of them."""
        self.room = room
        count = len(self.links)
        rows = count if room is None else max(0, int(room) // self.row_height())
        shown = count if rows >= count else max(0, rows - 1)
        for index, button in enumerate(self.buttons):
            button.setVisible(index < shown)
        self.hidden_links = self.links[shown:]
        self.more_menu.clear()
        for link in self.hidden_links:
            self.more_menu.addAction(mark_icon(link.get("mark") or link.get("provider")),
                                     link.get("title") or link.get("provider"),
                                     lambda link=link: self.opened.emit(dict(link)))
        self.more.setVisible(bool(self.hidden_links))
        self._say_more()
        self.setVisible(bool(self.links))

    def _say_more(self):
        hidden = len(getattr(self, "hidden_links", ()) or ())
        self.more.setText("+%d more" % hidden)
        self.more.setToolTip("%d more pinned" % hidden)


class Resized(QObject):
    """Calls ``then()`` when the one widget it watches changes size - how the
    rail learns its room without a timer or an application-wide filter.

    ``then`` is held weakly: a bound method of the window would make this child
    the holder of its parent, and freeing it during the window's own deletion
    freed the window a second time (see gui-suite-known-failures)."""

    def __init__(self, watched, then):
        super().__init__(watched)
        self._then = weakref.WeakMethod(then) if hasattr(then, "__self__") else (lambda: then)
        watched.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize:
            then = self._then()
            if then is not None:
                then()
        return False
