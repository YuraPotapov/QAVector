"""The providers on Integrations as cards: what each is, where it stands, what it can do.

A provider is a whole subsystem - Jira's reads and changes, Teams' posts,
the agents' conversations and delegation - so the catalog shows one card each
rather than one row: its mark, a word for what it is, how many connections it
has and whether one needs attention, the things it can do, and the two ways
to open it. Three providers are three cards; the grid gives way to two columns,
then one, when the page is narrow, so it never holds the window wide.

A card's figures are what the page already read - files the core reads, never
the service itself. What is not read yet is said so, not shown as nought.
"""

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QPushButton,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from .. import brandmarks, icons, widgets


class Provider:
    """What the catalog says about one provider; the figures come from its view."""

    def __init__(self, key, name, kind, what, mark, can, flag=None):
        self.key, self.name, self.kind, self.what, self.mark, self.can = key, name, kind, what, mark, can
        #: ``(word, why)`` worn beside the name - Teams' "Limited" - or None.
        self.flag = flag


class Standing:
    """Where a provider stands, as its card says it: ``count`` (None = not read
    yet), a ``state`` for the tag (ok / warn / idle), the tag and a line under the count."""

    def __init__(self, count=None, state="idle", tag="NOT READ YET", detail=""):
        self.count, self.state, self.tag, self.detail = count, state, tag, detail


def standing_of(connections, listed, waiting=0, kinds=False):
    """A provider's :class:`Standing` from the connections its view has read.

    A connection without its token or webhook needs attention; so do changes
    agents asked for that wait for the person. ``kinds`` names the programs
    (Agents) where the others name nothing more.
    """
    if not listed:
        return Standing(None, "idle", "NOT READ YET", "Its connections are read when the page opens.")
    count = len(connections)
    if not count:
        return Standing(0, "idle", "NOT SET UP", "No connection yet: open it to add one.")
    missing = sum(1 for one in connections if one.get("token_set") is False)
    if missing:
        detail = "%d without %s" % (missing, "a token" if missing == 1 else "tokens")
        return Standing(count, "warn", "NEEDS ATTENTION", detail)
    if waiting:
        return Standing(count, "warn", "%d WAITING" % waiting,
                        "%d change%s agents asked for wait%s for you" % (
                            waiting, "" if waiting == 1 else "s", "s" if waiting == 1 else ""))
    if kinds:
        names = sorted({str(one.get("kind") or "").capitalize() for one in connections if one.get("kind")})
        detail = ", ".join(names) if names else "Ready"
    else:
        detail = "Every connection is ready" if count > 1 else "The connection is ready"
    return Standing(count, "ok", "CONNECTED", detail)


class _Mark(QWidget):
    """The provider's maker's mark (:mod:`brandmarks`), painted in the palette of
    the moment - so a switch to dark mode needs nobody to repaint it by hand."""

    SIZE = 44

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.name = name
        self.setFixedSize(self.SIZE, self.SIZE)

    def set_name(self, name):
        if name != self.name:
            self.name = name
            self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        brandmarks.paint(painter, self.name, QRectF(0, 0, self.SIZE, self.SIZE))
        painter.end()


def _label(text, role, wrap=False):
    label = QLabel(text)
    label.setProperty("role", role)
    label.setWordWrap(wrap)
    return label


def _restyle(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class ProviderCard(QFrame):
    """One provider. A click picks it; a double click, Enter or Open opens it;
    the corner button opens it in a window of its own."""

    picked = Signal(str)
    open_requested = Signal(str)
    detach_requested = Signal(str)
    WHAT_LINES = 3

    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.setProperty("role", "providercard")
        self.setProperty("selected", False)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setAccessibleName("%s integration" % provider.name)

        column = QVBoxLayout(self)
        column.setContentsMargins(18, 16, 18, 14)
        column.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(12)
        self.mark = _Mark(provider.mark)
        head.addWidget(self.mark, 0, Qt.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(widgets.kicker(provider.kind))
        self.name_label = _label(provider.name, "providername")
        self.flag = None
        if provider.flag:
            word, why = provider.flag
            self.flag = _label(word.upper(), "providerflag")
            self.flag.setToolTip(why)
            self.flag.setAccessibleDescription(why)
            line = QHBoxLayout()
            line.setSpacing(8)
            line.addWidget(self.name_label)
            line.addWidget(self.flag, 0, Qt.AlignVCenter)
            line.addStretch(1)
            names.addLayout(line)
        else:
            names.addWidget(self.name_label)
        head.addLayout(names, 1)
        self.tag = _label("", "providerstate")
        head.addWidget(self.tag, 0, Qt.AlignTop)
        column.addLayout(head)

        self.what = _label(provider.what, "providerwhat", wrap=True)
        # Three lines kept for it on every card, so the figures under it stand at
        # one height across a row - and on a card alone in the last row too.
        self.what.setMinimumHeight(self.what.fontMetrics().lineSpacing() * self.WHAT_LINES)
        self.what.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        column.addWidget(self.what)

        figures = QHBoxLayout()
        figures.setSpacing(10)
        self.count = _label("", "providercount")
        figures.addWidget(self.count, 0, Qt.AlignBottom)
        under = QVBoxLayout()
        under.setSpacing(0)
        under.addStretch(1)
        under.addWidget(widgets.kicker("Connections"))
        self.detail = _label("", "providerdetail")
        under.addWidget(self.detail)
        figures.addLayout(under, 1)
        column.addLayout(figures)

        column.addWidget(widgets.hline())
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.chips = [_label(word.upper(), "capchip") for word in provider.can]
        for chip in self.chips:
            chips.addWidget(chip)
        chips.addStretch(1)
        column.addLayout(chips)
        column.addWidget(widgets.hline())

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.open_button = QPushButton("Open")
        self.open_button.setProperty("variant", "primary")
        self.open_button.setCursor(Qt.PointingHandCursor)
        self.open_button.clicked.connect(lambda: self.open_requested.emit(provider.key))
        actions.addWidget(self.open_button)
        actions.addStretch(1)
        self.window_button = icons.button(QToolButton(), "popout")
        self.window_button.setIconSize(QSize(16, 16))
        self.window_button.setToolTip("Open %s in a separate window" % provider.name)
        self.window_button.setAccessibleName("Open %s in a separate window" % provider.name)
        self.window_button.setCursor(Qt.PointingHandCursor)
        self.window_button.clicked.connect(lambda: self.detach_requested.emit(provider.key))
        actions.addWidget(self.window_button)
        column.addLayout(actions)
        self.set_standing(Standing())

    # -- what it says ---------------------------------------------------------
    def set_standing(self, standing):
        self.standing = standing
        self.count.setText("—" if standing.count is None else "%d" % standing.count)
        self.count.setProperty("state", "idle" if not standing.count else "")
        self.tag.setText(standing.tag)
        self.tag.setProperty("state", standing.state)
        self.detail.setText(standing.detail)
        self.detail.setProperty("state", "warn" if standing.state == "warn" else "")
        # Nothing to open into yet: the button says what opening is for.
        self.open_button.setText("Set up" if standing.count == 0 else "Open")
        for widget in (self.count, self.tag, self.detail):
            _restyle(widget)

    def set_mark(self, name):
        """Show another maker's mark - for Agents, the program used last."""
        self.mark.set_name(name if name in brandmarks.MARKS else self.provider.mark)

    def set_selected(self, selected):
        if bool(self.property("selected")) != bool(selected):
            self.setProperty("selected", bool(selected))
            _restyle(self)

    def selected(self):
        return bool(self.property("selected"))

    # -- what it is told ------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setFocus(Qt.MouseFocusReason)
            self.picked.emit(self.provider.key)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.open_requested.emit(self.provider.key)
        super().mouseDoubleClickEvent(event)

    def focusInEvent(self, event):
        self.picked.emit(self.provider.key)
        super().focusInEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.open_requested.emit(self.provider.key)
            return
        super().keyPressEvent(event)


class ProviderGrid(QWidget):
    """The cards, three to a row on a wide page, fewer on a narrow one."""

    #: The narrowest a card is laid out at before the grid drops a column.
    CARD_WIDTH = 350

    picked = Signal(str)
    open_requested = Signal(str)
    detach_requested = Signal(str)

    def __init__(self, providers, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        # The columns follow the width (resizeEvent), so the layout must not
        # hold the width at three cards' worth.
        self.grid.setSizeConstraint(QLayout.SetNoConstraint)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(14)
        self.cards = {}
        for provider in providers:
            card = ProviderCard(provider, self)
            card.picked.connect(self.pick)
            card.open_requested.connect(self.open_requested)
            card.detach_requested.connect(self.detach_requested)
            self.cards[provider.key] = card
        self._columns = 0
        self._place(len(self.cards) or 1)
        if self.cards:
            self.pick(next(iter(self.cards)))

    def _place(self, columns):
        if columns == self._columns:
            return
        self._columns = columns
        for card in self.cards.values():
            self.grid.removeWidget(card)
        for index, card in enumerate(self.cards.values()):
            self.grid.addWidget(card, index // columns, index % columns)
        for column in range(max(len(self.cards), 3)):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)

    def columns(self):
        return self._columns

    def resizeEvent(self, event):
        super().resizeEvent(event)
        spacing = self.grid.horizontalSpacing()
        fits = max(1, (event.size().width() + spacing) // (self.CARD_WIDTH + spacing))
        self._place(max(1, min(len(self.cards) or 1, fits)))

    def minimumSizeHint(self):
        # One column at the narrowest: the window is never held wide by a card.
        return QSize(self.CARD_WIDTH, super().minimumSizeHint().height())

    def pick(self, key):
        if key not in self.cards:
            return
        for name, card in self.cards.items():
            card.set_selected(name == key)
        self.picked.emit(key)

    def selected(self):
        return next((key for key, card in self.cards.items() if card.selected()), "")
