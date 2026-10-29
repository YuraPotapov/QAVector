"""Shared widgets that carry the design system's distinctive parts.

Qt stylesheets cover colour, type and borders; the pieces here are the ones a
stylesheet cannot express - the blueprint frame's registration marks, the
segmented control, the tag pill - plus small constructors so pages read as
layout rather than as widget configuration.
"""

import html
import itertools
import math
import os
import re
import weakref

from PySide6.QtCore import (Property, QEasingCurve, QEvent, QObject, QPoint,
                            QPointF, QPropertyAnimation, QRect, QRectF, QSize,
                            Qt, QTimer, Signal)
from PySide6.QtGui import (QBrush, QColor, QFontMetricsF, QIcon, QKeySequence,
                           QPainter, QPen, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QAbstractScrollArea,
                               QAbstractSpinBox, QApplication, QComboBox, QDialog,
                               QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QSizePolicy,
                               QSpinBox, QSplitter, QStackedLayout, QStyle,
                               QStyledItemDelegate, QStyleOptionToolButton, QStylePainter,
                               QTableWidget, QTableWidgetItem, QToolButton,
                               QVBoxLayout, QWidget)

from . import icons, theme


class BlueprintPanel(QFrame):
    """A plain hairline-bordered box.

    The design's corner registration marks (a small cross at each corner) were
    dropped: at this density they read as stray plus signs floating next to the
    frame rather than as draughting marks, and they collided with the controls
    sitting near the edges. The square, borderless-radius frame carries the
    blueprint look on its own.
    """

    def __init__(self, parent=None, padding=(16, 16, 16, 16), fixed=False):
        """``fixed`` fills the panel: for a part of the page, not a row someone made.

        The page this exists for stacks both kinds - a project a person added,
        and Connections, which is always there. Given one frame for both, the
        only way to tell which is which is to read the titles.
        """
        super().__init__(parent)
        self.setProperty("role", "panel-fixed" if fixed else "panel")
        self.setContentsMargins(0, 0, 0, 0)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(*padding)
        self._layout.setSpacing(10)

    def layout(self):
        return self._layout


class FloatingPanel(QFrame):
    """A frame over a window, typed into, that leaves the keyboard to the desktop.

    Search and the clarify popup used to be Qt.Popup windows. On X11 a popup
    grabs the keyboard for as long as it is open, so every key went to it -
    the desktop's own ones too: Super+Space never reached GNOME, and the
    keyboard layout could not be switched while typing in it.

    This is a frameless tool window instead, which takes focus the ordinary way.
    It goes on Esc (the subclass's) or when another window of the application
    takes focus - a click on the window under it, as with the popup. Focus
    leaving for the desktop - a layout switcher, another program - keeps it
    open: what was being typed is not done.
    """

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint)
        QApplication.instance().focusWindowChanged.connect(self._focus_moved)

    def _focus_moved(self, window):
        if window is not None and self.isVisible() and window is not self.windowHandle():
            self.hide()


class Tag(QLabel):
    """Small status pill: accent (good), outline (in flight), neutral, bad."""

    @staticmethod
    def styles():
        """(background, ink, border) for each variant, read when a pill is set.

        Not a table built at import: dark mode rewrites the theme after the
        import, and the table kept painting light pills on a dark page.
        """
        return {
            "accent": (theme.TAG_TINT, theme.TAG_INK, "transparent"),
            "outline": ("transparent", theme.ACCENT, theme.ACCENT),
            "neutral": (theme.NEUTRAL[100], theme.NEUTRAL[800], theme.NEUTRAL[300]),
            "warn": (theme.WARN_TINT, theme.WARN, "transparent"),
            "bad": (theme.BAD_TINT, theme.BAD, "transparent"),
        }

    def __init__(self, text="", variant="neutral", parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.set_variant(variant)

    def set_variant(self, variant):
        styles = self.styles()
        background, fg, border = styles.get(variant, styles["neutral"])
        self.setStyleSheet(
            "background: %s; color: %s; border: 1px solid %s;"
            "font-family: %s; font-size: 10px; letter-spacing: 0.5px;"
            "padding: 2px 8px;" % (background, fg, border, theme.MONO_CSS))

    def set(self, text, variant):
        self.setText(text)
        self.set_variant(variant)


#: The rail's badge geometry. The count goes INSIDE the point - a filled bubble
#: with the figure on it - rather than beside it, because the rail is the width
#: its names need and a number set next to a disc is room it has not got. Small,
#: because two of them have to stand past the longest label in the rail:
#: "Services & Logs" is the entry that carries both counts and it is also very
#: nearly the widest thing the rail measured itself from.
BADGE_HEIGHT = 13
BADGE_PAD = 3
BADGE_GAP = 2
BADGE_RIGHT = 4

#: How far into the label's own right padding the counts may reach. The rail's
#: rule gives every entry 13px there and a label never draws in it, so most of
#: it is room going spare - and the few px left still keep the two apart.
BADGE_ENCROACH = 9

#: What is drawn where even a bubble will not go - an entry narrower than the
#: rail measured for, which is the rail refusing to widen rather than the rail
#: being wrong. A bare point, and the figure on the tooltip that has it anyway.
POINT_DOT = 6
POINT_GAP = 3
POINT_RIGHT = 6

#: Collapsed the rail is a column of marks and a width, and neither form fits
#: beside a mark - so the points go on its own corner instead, stacked.
#: COMPACT_INSET is the air between the mark's right edge and the points: they
#: sat against it, and a point touching a drawing reads as part of the drawing.
COMPACT_DOT = 5
COMPACT_GAP = 2
COMPACT_INSET = 2

#: Badge colour tokens -> the theme attribute read when one is painted. Read
#: late, never captured: set_dark_mode rewrites the palette in place.
BADGE_INKS = {"ok": "OK", "bad": "BAD"}

#: The figure on a bubble. Both fills are dark inks in either theme - OK and BAD
#: are not among what set_dark_mode rewrites - so one light ink reads on both.
BADGE_FIGURE = "#ffffff"


class NavButton(QToolButton):
    """A rail entry that can carry live counts.

    Points on the rail because it is read at a glance and from the corner of the
    eye: what is running is a colour before it is a figure. The figure is on the
    point rather than beside it, and nothing is reserved for either, so the rail
    stays exactly the width it measured from its labels - which is the width it
    is for, expanded and collapsed both.

    Collapsed the bubbles do not fit at all, so they become bare points on the
    mark's own corner and the numbers move to the tooltip that mode already puts
    the name on.
    """

    def __init__(self, badges=2, parent=None):
        super().__init__(parent)
        self._badges = []
        self._summary = ""
        self._compact = False
        self._slots = max(0, int(badges))

    def set_compact(self, compact):
        """Marks only: the counts become points, and the numbers the tooltip."""
        compact = bool(compact)
        if compact != self._compact:
            self._compact = compact
            self.update()

    def set_badges(self, pairs, summary=""):
        """Show ``[(count, token), …]``, in the order they should read.

        A count of zero shows nothing: an idle rail should be quiet, and a point
        that is always there reading 0 is one more thing to check rather than
        one fewer. What is shown is laid out from the right, so the last one
        given - the failures - keeps its place whether or not anything is
        running beside it.

        ``summary`` is the same counts in words, for the collapsed tooltip. It
        is kept rather than composed here because what the numbers are called is
        the caller's to say; this only knows how many and in which colour.

        Repainting only on a real change matters because the run's counts come
        off RunState, which fires on every event the launcher sends.
        """
        wanted = [(int(count), token) for count, token in pairs]
        if wanted == self._badges and summary == self._summary:
            return
        self._badges, self._summary = wanted, summary
        self.update()

    def badges(self):
        return list(self._badges)

    def summary(self):
        """The counts in words. Empty when there is nothing to report."""
        return self._summary if any(c > 0 for c, _t in self._badges) else ""

    def _shown(self):
        return [(count, token) for count, token in self._badges[:self._slots]
                if count > 0]

    def _bubbles(self, metrics):
        """``[(text, width, token)]`` for what is shown, and what it comes to."""
        boxes = [(str(count),
                  max(BADGE_HEIGHT, metrics.horizontalAdvance(str(count))
                      + 2 * BADGE_PAD),
                  token)
                 for count, token in self._shown()]
        return boxes, sum(w for _t, w, _k in boxes) + BADGE_GAP * (len(boxes) - 1)

    def _room(self):
        """The width past the label that nothing else has a claim on.

        sizeHint is the label, the mark and the padding the rail's own rule
        gives them, so what is left of the button is free - plus the part of
        that padding a label never draws in. Staying inside it is the whole
        arrangement: the rail is measured from its names, and the counts take
        what that leaves rather than asking for a wider rail.
        """
        return self.width() - self.sizeHint().width() + BADGE_ENCROACH

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._shown():
            return
        from PySide6.QtGui import QPainter
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setFont(theme.mono_font(8))
        boxes, needed = self._bubbles(painter.fontMetrics())
        # The fullest form the entry has room for. A long label leaves a narrow
        # entry no space for figures, and rather than widen the rail - which is
        # the one thing the rail is not to do - it says the same in colour alone
        # and puts the numbers on the tooltip, which carries them either way.
        if self._compact or needed + BADGE_RIGHT > self._room():
            self._paint_points(painter)
        else:
            self._paint_bubbles(painter, boxes)
        painter.end()

    def _paint_bubbles(self, painter, boxes):
        """The counts on their own points, laid out from the right edge in.

        From the right because the label is laid out from the left and nothing
        holds a gap between them: what is drawn has to end where the button
        does, so a count that grows a digit grows towards the empty side.
        """
        top = (self.height() - BADGE_HEIGHT) / 2.0
        right = self.width() - BADGE_RIGHT
        for text, width, token in reversed(boxes):
            box = QRectF(right - width, top, width, BADGE_HEIGHT)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._ink(token))
            painter.drawRoundedRect(box, BADGE_HEIGHT / 2.0, BADGE_HEIGHT / 2.0)
            painter.setPen(theme.color(BADGE_FIGURE))
            painter.drawText(box, Qt.AlignCenter, text)
            right -= width + BADGE_GAP

    def _paint_points(self, painter):
        """Colour alone: a bare point per count, and the figures on the tooltip.

        Collapsed they go on the mark's own corner, stacked, because beside it
        is room the rail has not got and the mark is centred in what is left.
        Expanded they sit at the right edge like the bubbles they stand in for.
        """
        painter.setPen(Qt.NoPen)
        if not self._compact:
            top = (self.height() - POINT_DOT) / 2.0
            right = self.width() - POINT_RIGHT
            for _text, _width, token in reversed(self._bubbles(
                    painter.fontMetrics())[0]):
                painter.setBrush(self._ink(token))
                painter.drawEllipse(QRectF(right - POINT_DOT, top,
                                           POINT_DOT, POINT_DOT))
                right -= POINT_DOT + POINT_GAP
            return
        icon = self.iconSize()
        area = self.contentsRect()
        right = area.x() + (area.width() + icon.width()) / 2.0
        top = area.y() + (area.height() - icon.height()) / 2.0
        for count, token in self._badges[:self._slots]:
            if count <= 0:
                top += COMPACT_DOT + COMPACT_GAP
                continue
            painter.setBrush(self._ink(token))
            painter.drawEllipse(QRectF(right + COMPACT_INSET, top,
                                       COMPACT_DOT, COMPACT_DOT))
            top += COMPACT_DOT + COMPACT_GAP

    @staticmethod
    def _ink(token):
        return theme.color(getattr(theme, BADGE_INKS.get(token, "TEXT")))


class CenteredToolButton(QToolButton):
    """A tool button whose mark and words sit in the middle of it.

    Qt sets a tool button's icon and text at its left edge, which on a button
    held wider than its label - Run, kept as wide as Resume so the row does not
    move - leaves the label off to one side. The button and its menu half are
    drawn as ever; the label is drawn again into a rect just its own size,
    centred in the part that is not the menu.
    """

    def paintEvent(self, _event):
        painter = QStylePainter(self)
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        label = QStyleOptionToolButton(option)
        option.text = ""
        option.icon = QIcon()
        painter.drawComplexControl(QStyle.CC_ToolButton, option)
        area = self.style().subControlRect(QStyle.CC_ToolButton, label,
                                           QStyle.SC_ToolButton, self)
        if self.popupMode() == QToolButton.MenuButtonPopup:
            # Under the stylesheet the button part is the whole width, with
            # the menu half drawn over its right end: take that end away.
            menu = self.style().subControlRect(QStyle.CC_ToolButton, label,
                                               QStyle.SC_ToolButtonMenu, self)
            if menu.isValid() and menu.left() > area.left():
                area.setRight(min(area.right(), menu.left() - 1))
        # What CE_ToolButtonLabel lays out: the icon in the middle of a slot
        # four pixels wider than it, then the text. The slot's first two
        # pixels are empty, so they are left out of what is centred.
        mark = label.iconSize.width() + 4 if not label.icon.isNull() else 0
        width = min(area.width(), mark + self.fontMetrics().horizontalAdvance(label.text))
        lead = 2 if mark else 0
        label.rect = QRect(area.x() + (area.width() - width + lead) // 2 - lead, area.y(),
                           width, area.height())
        painter.drawControl(QStyle.CE_ToolButtonLabel, label)


class Segmented(QWidget):
    """A row of mutually exclusive buttons (the design's `.seg`).

    Used where a QComboBox would hide the options: log levels, log filters -
    short, fixed vocabularies that are worth showing all at once.
    """

    changed = Signal(str)

    def __init__(self, options, current=None, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._buttons = {}
        for option in options:
            button = QPushButton(option, self)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, o=option: self.set_current(o))
            row.addWidget(button)
            self._buttons[option] = button
        row.addStretch(1)
        self.set_current(current or (options[0] if options else None), notify=False)

    def set_current(self, option, notify=True):
        self._current = option
        for name, button in self._buttons.items():
            active = name == option
            button.setChecked(active)
            button.setStyleSheet(
                "background: %s; color: %s; border: 1px solid %s; padding: 3px 11px;"
                "font-family: %s; font-size: 12px; font-weight: 500;"
                % (theme.ACCENT if active else "transparent",
                   theme.BG if active else theme.TEXT,
                   theme.ACCENT if active else theme.DIVIDER, theme.BODY_CSS))
        if notify:
            self.changed.emit(option)

    def set_text(self, option, text):
        """Relabel ``option``'s button - a count on it, say - keeping its key."""
        button = self._buttons.get(option)
        if button is not None:
            button.setText(text)

    def set_enabled(self, option, enabled, why=""):
        """Offer ``option`` or not, saying why not in its tooltip."""
        button = self._buttons.get(option)
        if button is not None:
            button.setEnabled(bool(enabled))
            button.setToolTip("" if enabled else why)

    def button(self, option):
        return self._buttons.get(option)

    def current(self):
        return self._current


class Stepper(QWidget):
    """A small number with a button on each side: [-][ 4 ][+].

    The stock spin box puts two 7px arrows stacked inside the frame - a target
    nobody can hit, and the first thing anyone notices about a form built out of
    them. Here each end is a full-height button, the number sits in the middle
    and is still typeable, and the three pieces share their hairlines so the
    control reads as one object.

    It carries the part of QSpinBox's interface the pages use, so it can stand in
    for one: :meth:`value`, :meth:`setValue`, :meth:`setRange` and the
    ``valueChanged`` signal.
    """

    valueChanged = Signal(int)

    def __init__(self, minimum=0, maximum=99, value=None, width=52, parent=None):
        super().__init__(parent)
        line = QHBoxLayout(self)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(0)

        self.down = self._button("minus", "left", -1)
        self.spin = QSpinBox()
        self.spin.setButtonSymbols(QSpinBox.NoButtons)
        self.spin.setAlignment(Qt.AlignCenter)
        self.spin.setRange(minimum, maximum)
        self.spin.setFixedWidth(width)
        self.up = self._button("plus", "right", +1)

        line.addWidget(self.down)
        line.addWidget(self.spin)
        line.addWidget(self.up)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self.spin.valueChanged.connect(self._on_value)
        self.setValue(minimum if value is None else value)
        self._match_heights()

    def _button(self, mark, edge, step):
        button = icons.button(QPushButton(""), mark)
        button.setProperty("variant", "step")
        button.setProperty("edge", edge)
        button.setCursor(Qt.PointingHandCursor)
        # The number keeps the focus: tabbing onto a plus sign helps nobody.
        button.setFocusPolicy(Qt.NoFocus)
        button.setAutoRepeat(True)
        button.setAutoRepeatDelay(400)
        button.setAutoRepeatInterval(90)
        button.clicked.connect(lambda _checked=False, s=step: self.step_by(s))
        return button

    def _match_heights(self):
        height = self.spin.sizeHint().height()
        for button in (self.down, self.up):
            button.setFixedHeight(height)

    # -- the QSpinBox interface the pages use ---------------------------------
    def value(self):
        return self.spin.value()

    def setValue(self, value):
        self.spin.setValue(int(value))
        self._update_limits()

    def setRange(self, minimum, maximum):
        self.spin.setRange(minimum, maximum)
        self._update_limits()

    def step_by(self, step):
        self.spin.setValue(self.spin.value() + step)

    def _on_value(self, value):
        self._update_limits()
        self.valueChanged.emit(value)

    def _update_limits(self):
        """Grey the end that would do nothing, rather than let it click on."""
        self.down.setEnabled(self.spin.value() > self.spin.minimum())
        self.up.setEnabled(self.spin.value() < self.spin.maximum())


class ElidedLabel(QLabel):
    """A one-line label that shortens its text instead of widening the window.

    A QLabel's minimum width is the width of its text, and a layout honours that:
    one full filesystem path in a status bar or a header is enough to push the
    whole window wider than the screen, which is how the Artifacts tree ended up
    crushed to a sliver beside a path label. Eliding in the middle keeps the two
    ends - the run folder and the file name are the parts anyone reads - and the
    whole string stays available as the tooltip.
    """

    def __init__(self, text="", mode=Qt.ElideMiddle, parent=None):
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self._eliding = False
        # Ignored, not Preferred: the point is that this label's own text must
        # never be what decides how wide anything gets.
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text):
        self._full = "" if text is None else str(text)
        self.setToolTip(self._full)
        # The size it asks for is its text's, so a new text is a new request.
        # Without this the layout kept the width of the first text it ever
        # had - a page heading that started as "Cycles" stayed that wide, and
        # every cycle's name was cut to it with room to spare beside it.
        self.updateGeometry()
        self._elide()

    def text(self):
        return self._full

    def sizeHint(self):
        # The whole text, not what is showing: a label that had to shorten once
        # would otherwise ask for only that much and never get its text back.
        # Only read under a policy other than Ignored - see elided_line.
        hint = super().sizeHint()
        margins = self.contentsMargins()
        # Measured with the fractional metrics and rounded up, with a few
        # pixels over: _elide keeps two back, and a text 184.4 pixels wide in
        # a label asked for at exactly 186 was cut short with room to spare.
        advance = math.ceil(QFontMetricsF(self.font()).horizontalAdvance(self._full))
        hint.setWidth(advance + 6 + margins.left() + margins.right())
        return hint

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(self.fontMetrics().horizontalAdvance("…") * 3)
        return hint

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        if self._eliding:
            return          # super().setText can trigger a resize, which re-enters
        self._eliding = True
        try:
            width = self.width() - 2
            if width <= 0:
                super().setText(self._full)
            else:
                super().setText(self.fontMetrics().elidedText(self._full, self._mode,
                                                              width))
        finally:
            self._eliding = False


class CheckList(QWidget):
    """A searchable list of tick boxes, with a count of what is ticked.

    Accounts, extensions and scenarios are all the same problem - "choose some
    of these, and there may be thirty of them" - so they are all the same widget.
    The search box hides rows rather than removing them, so a ticked row that
    scrolls out of the filter is still part of the selection.

    ``selected_view`` adds an *All / Selected* switch, for a list long enough that
    what is ticked gets lost in it. Selected narrows the same rows to the ticked
    ones, in the same place - so the page does not grow with the selection,
    whether that is three rows or sixty - and *Clear all* unticks the lot.
    """

    changed = Signal()

    def __init__(self, searchable=True, placeholder="Search…", selected_view=False,
                 parent=None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self._needle = ""

        self.view = None
        self.clear_button = None
        if selected_view:
            self.view = Segmented([VIEW_ALL, VIEW_SELECTED], VIEW_ALL)
            self.view.changed.connect(lambda _v: self._apply_visibility())
            self.clear_button = QPushButton("Clear all")
            self.clear_button.clicked.connect(lambda: self.set_all(False))
            column.addWidget(row(self.view, None, self.clear_button))

        self.search = QLineEdit()
        self.search.setPlaceholderText(placeholder)
        self.search.textChanged.connect(self.set_filter)
        self.search.setVisible(searchable)
        column.addWidget(self.search)

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.NoSelection)
        self.list.setMinimumHeight(140)
        self.list.itemChanged.connect(self._on_item_changed)
        column.addWidget(self.list, 1)

        self.count = QLabel("")
        self.count.setStyleSheet("font-size: 11px; color: %s; font-family: %s;"
                                 % (theme.NEUTRAL[600], theme.MONO_CSS))
        column.addWidget(self.count)
        self._quiet = False
        self._noun = "selected"
        # Only where Selected can empty the list: an empty Selected is a state to
        # explain ("nothing ticked yet"), not a list that failed to fill.
        self._empty = empty_note(self.list, "") if selected_view else None
        self._nothing_selected = ""
        self._update_count()

    # -- contents -------------------------------------------------------------
    def set_noun(self, noun):
        """What the count line calls the things in the list."""
        self._noun = noun
        self._update_count()

    def set_nothing_selected(self, text=""):
        """What an empty Selected view says; "" for the usual "tick some" line.

        For a list whose ticks come from somewhere else, where telling the user to
        tick rows would point at something they cannot do.
        """
        self._nothing_selected = text
        self._update_count()

    def clear(self):
        self._quiet = True
        self.list.clear()
        self._quiet = False
        self._update_count()

    def add(self, value, label=None, note="", enabled=True, accent=False):
        item = QListWidgetItem("%-30s %s" % (label if label is not None else value,
                                             note))
        item.setFont(theme.mono_font(9))
        item.setData(Qt.UserRole, value)
        flags = item.flags() | Qt.ItemIsUserCheckable
        if not enabled:
            # An unusable row still has to be visible - the reason it cannot be
            # picked is the useful part - so it is shown greyed rather than hidden.
            flags &= ~Qt.ItemIsEnabled
        item.setFlags(flags)
        item.setCheckState(Qt.Unchecked)
        if accent:
            item.setForeground(theme.color(theme.ACCENT_RAMP[700]))
        elif not enabled:
            item.setForeground(theme.color(theme.NEUTRAL[500]))
        self._quiet = True
        self.list.addItem(item)
        self._quiet = False
        if self.view is not None:
            item.setHidden(self._should_hide(item))
        self._update_count()
        return item

    # -- selection ------------------------------------------------------------
    def values(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]

    def checked(self):
        return [self.list.item(i).data(Qt.UserRole)
                for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]

    def set_checked(self, values, notify=False):
        wanted = set(values or ())
        self._quiet = True
        for index in range(self.list.count()):
            item = self.list.item(index)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in wanted
                               else Qt.Unchecked)
        self._quiet = False
        self._apply_visibility()
        if notify:
            self.changed.emit()

    def set_all(self, checked):
        self._quiet = True
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.flags() & Qt.ItemIsEnabled:
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._quiet = False
        self._apply_visibility()
        self.changed.emit()

    # -- filtering ------------------------------------------------------------
    def set_filter(self, text):
        self._needle = (text or "").strip().lower()
        self._apply_visibility()

    def selected_only(self):
        """Whether the Selected view is on (never, without ``selected_view``)."""
        return self.view is not None and self.view.current() == VIEW_SELECTED

    def _searched_out(self, item):
        return bool(self._needle) and self._needle not in item.text().lower()

    def _should_hide(self, item):
        return self._searched_out(item) or (self.selected_only()
                                            and item.checkState() != Qt.Checked)

    def _apply_visibility(self):
        for index in range(self.list.count()):
            item = self.list.item(index)
            item.setHidden(self._should_hide(item))
        self._update_count()

    def _on_item_changed(self, item):
        # Unticked under Selected, a row goes at once: that view is "what is
        # ticked", and a row left behind would say otherwise.
        if self.view is not None:
            item.setHidden(self._should_hide(item))
        self._update_count()
        if not self._quiet:
            self.changed.emit()

    def _update_count(self):
        total = self.list.count()
        ticked = len(self.checked())
        searched = sum(1 for i in range(total) if self._searched_out(self.list.item(i)))
        text = "%d of %d %s" % (ticked, total, self._noun)
        if searched:
            text += "   ·   %d hidden by the search" % searched
        self.count.setText(text)
        if self.view is not None:
            self.view.set_text(VIEW_ALL, "All %d" % total)
            self.view.set_text(VIEW_SELECTED, "Selected %d" % ticked)
            self.clear_button.setEnabled(ticked > 0)
        if self._empty is not None:
            # Hiding a row is not a change to the model, so the note is told.
            if self.selected_only() and not ticked:
                self._empty.say_filtered(
                    self._nothing_selected
                    or "Nothing selected yet - tick %s under All." % self._noun)
            elif self._needle:
                self._empty.say_filtered("Nothing here matches the search.")
            else:
                self._empty.say_filtered("")


#: The two views a :class:`CheckList` with ``selected_view`` switches between.
VIEW_ALL = "All"
VIEW_SELECTED = "Selected"


class UrlField(QWidget):
    """One address: shown as it will open, edited as it is written.

    Showing, it is a box the size of an input holding the address a run will
    open, with each part a variable supplied drawn in :data:`theme.VAR_INK` and
    the rest in the ordinary ink - an address with no variables in it is all
    plain text. Clicking the plain text, or the pencil, turns the same box into
    the editor for what was actually written (placeholders and all); Enter or
    leaving it shows the address again, Escape puts back what was there.
    Clicking a highlighted part asks for that variable instead
    (:attr:`value_clicked`) - the page decides what that opens.

    Two widgets in one place rather than one painted editor: a QLineEdit cannot
    colour part of its text, and a label cannot be typed into. They are held to
    one height so switching between them never moves the page.
    """

    #: The text written, committed by Enter or by leaving the editor.
    edited = Signal(str)
    #: The name of the variable whose highlighted value was clicked.
    value_clicked = Signal(str)

    def __init__(self, placeholder="", parent=None):
        super().__init__(parent)
        self._parts = []
        self._template = ""
        self._placeholder = placeholder
        self._editing = False

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self.frame = QFrame()
        self.frame.setProperty("role", "urlfield")
        self.frame.setCursor(Qt.IBeamCursor)
        row = QHBoxLayout(self.frame)
        # The border, the input's 8px padding and the 2px a QLineEdit keeps
        # inside itself: the shown text starts where the typed text will.
        row.setContentsMargins(10, 0, 2, 0)
        row.setSpacing(4)
        self.label = _UrlLabel(self)
        # Never as wide as its text: a long address is clipped in the box (the
        # whole of it is one click away, in the editor) rather than widening
        # the page - and with it the window - past the screen.
        self.label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.label.setMinimumWidth(0)
        row.addWidget(self.label, 1)
        self.pencil = QToolButton()
        icons.button(self.pencil, "edit")
        self.pencil.setToolTip("Edit the address as it is written")
        self.pencil.setCursor(Qt.PointingHandCursor)
        self.pencil.clicked.connect(self.begin_edit)
        row.addWidget(self.pencil)

        self.editor = _UrlEditor(self)
        self.editor.setProperty("mono", True)
        self.editor.returnPressed.connect(self._commit)
        self.editor.editingFinished.connect(self._commit)

        self._stack.addWidget(self.frame)
        self._stack.addWidget(self.editor)
        self.frame.setFixedHeight(self.editor.sizeHint().height())
        self._render()

    # -- contents -------------------------------------------------------------
    def set_address(self, parts, template=""):
        """Show ``parts`` and keep ``template`` as what the editor starts from.

        ``parts`` is ``[(text, variable name or "")]`` in order; ``template``
        the address as written. Ignored while the editor is open, so a refresh
        never takes the text out from under someone typing it.
        """
        self._parts = [(str(text), name or "") for text, name in parts if text]
        self._template = template or ""
        if not self._editing:
            self._render()

    def set_placeholder(self, text):
        self._placeholder = text or ""
        self.editor.setPlaceholderText(self._placeholder)
        self._render()

    def set_variables(self, names):
        """The variables the address can name, offered while it is typed."""
        self.editor.set_names(names)

    def text(self):
        """The address as shown, variables filled in."""
        return "".join(text for text, _name in self._parts)

    def is_editing(self):
        return self._editing

    # -- switching ------------------------------------------------------------
    def begin_edit(self):
        if self._editing or not self.isEnabled():
            return
        self._editing = True
        self.editor.setText(self._template)
        self._stack.setCurrentWidget(self.editor)
        self.editor.setFocus(Qt.MouseFocusReason)
        self.editor.end(False)

    def cancel_edit(self):
        if not self._editing:
            return
        self._editing = False
        self._stack.setCurrentWidget(self.frame)
        self._render()

    def _commit(self):
        # returnPressed and editingFinished both arrive for one Enter, and
        # leaving the box sends the second on its own: only the first counts.
        if not self._editing:
            return
        self._editing = False
        text = self.editor.text().strip()
        self._stack.setCurrentWidget(self.frame)
        self._render()
        self.edited.emit(text)

    # -- drawing --------------------------------------------------------------
    def _render(self):
        if not self._parts:
            self.label.setProperty("empty", "true")
            self.label.setText(html.escape(self._placeholder))
        else:
            self.label.setProperty("empty", "false")
            chunks = []
            for text, name in self._parts:
                escaped = html.escape(text)
                if name:
                    # Read now, not at import: set_dark_mode rewrites the ink.
                    chunks.append('<a href="var:%s" style="color: %s; '
                                  'text-decoration: none;">%s</a>'
                                  % (html.escape(name, quote=True), theme.VAR_INK,
                                     escaped))
                else:
                    chunks.append(escaped)
            self.label.setText("".join(chunks))
        self.label.style().unpolish(self.label)
        self.label.style().polish(self.label)

    def changeEvent(self, event):             # noqa: N802 - Qt's own spelling
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange):
            self._render()


class _UrlLabel(QLabel):
    """The shown address: a highlighted part asks for its variable, the rest edits."""

    def __init__(self, field):
        super().__init__()
        self._field = field
        self.setTextFormat(Qt.RichText)
        self.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.linkActivated.connect(self._link)
        self.linkHovered.connect(self._hover)
        self._hovered = ""

    def _link(self, href):
        if href.startswith("var:"):
            self._field.value_clicked.emit(href[len("var:"):])

    def _hover(self, href):
        self._hovered = href or ""
        self.setCursor(Qt.PointingHandCursor if href else Qt.IBeamCursor)

    def mouseReleaseEvent(self, event):       # noqa: N802 - Qt's own spelling
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton and not self._hovered:
            self._field.begin_edit()


#: What may stand after an open ``{{`` while a name is still being typed.
_PARTIAL_NAME = re.compile(r"\s*(vars\.)?[A-Za-z0-9_]*")


class _UrlEditor(QLineEdit):
    """The editor half of :class:`UrlField`: Escape gives the old text back.

    It also finishes variable names. After ``{{`` - or ``{{po`` - the rest of
    the first name that fits is written in and left selected: typing on
    replaces it, Enter, Tab or Right keeps it. Inline rather than in a pop-up
    list, because a pop-up holds the X11 keyboard while it is open and the
    keyboard layout could not be switched under it. Deleting never completes,
    or a completion could not be taken back. The context menu lists them too.
    """

    def __init__(self, field):
        super().__init__()
        self._field = field
        self._names = []
        # What was typed, without a completion pending: a key that replaces the
        # selected completion makes the text shorter than with it, and is still
        # typing on rather than deleting.
        self._typed = ""
        self.textEdited.connect(self._complete)

    def set_names(self, names):
        self._names = sorted(names or ())
        self.setToolTip("Type {{ for a variable: %s" % ", ".join(self._names)
                        if self._names else "")

    def setText(self, text):                  # noqa: N802 - Qt's own spelling
        super().setText(text)
        self._typed = text

    def _complete(self, text):
        grew = len(text) > len(self._typed)
        self._typed = text
        cursor = self.cursorPosition()
        if not grew or not self._names or re.match(r"[A-Za-z0-9_]", text[cursor:cursor + 1]):
            return                  # deleting, nothing to offer, or inside a word
        before = text[:cursor]
        start = before.rfind("{{")
        if start < 0 or "}}" in before[start:]:
            return
        partial = before[start + 2:]
        if not _PARTIAL_NAME.fullmatch(partial):
            return
        bare = partial.strip()
        bare = bare[len("vars."):] if bare.startswith("vars.") else bare
        match = next((name for name in self._names if name.startswith(bare)), None)
        if match is None:
            return
        rest = match[len(bare):] + ("" if text[cursor:].startswith("}}") else "}}")
        if not rest:
            return
        QLineEdit.setText(self, text[:cursor] + rest + text[cursor:])
        self.setSelection(cursor, len(rest))

    def variable_actions(self, menu):
        """Add "Insert variable" to ``menu``; returns the actions added."""
        if not self._names:
            return []
        menu.addSeparator()
        inner = menu.addMenu("Insert variable")
        found = []
        for name in self._names:
            action = inner.addAction("{{%s}}" % name)
            action.triggered.connect(lambda _c=False, n=name: self.insert("{{%s}}" % n))
            found.append(action)
        return found

    def contextMenuEvent(self, event):        # noqa: N802 - Qt's own spelling
        menu = self.createStandardContextMenu()
        self.variable_actions(menu)
        menu.exec(event.globalPos())
        menu.deleteLater()

    def keyPressEvent(self, event):           # noqa: N802 - Qt's own spelling
        if event.key() == Qt.Key_Escape and self._field.is_editing():
            self._field.cancel_edit()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):           # noqa: N802 - Qt's own spelling
        super().focusOutEvent(event)
        # A menu of this box's own (right click) is not leaving it.
        if event.reason() != Qt.PopupFocusReason:
            self._field._commit()


class Disclosure(QWidget):
    """A titled section that folds away, for settings most runs never touch.

    Advanced options have to be reachable without being in the way: a collapsed
    section keeps them one click deep instead of pushing them onto another page
    or behind a mode switch.
    """

    toggled = Signal(bool)

    def __init__(self, title, expanded=False, parent=None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        self._title = title
        self.button = QPushButton("")
        self.button.setProperty("variant", "ghost")
        self.button.setCheckable(True)
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.toggled.connect(self._on_toggled)
        # A row rather than the button alone, so a control can be put on the
        # title's line without being put beside the whole section - see
        # add_to_header.
        self._header = QWidget()
        header_row = QHBoxLayout(self._header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)
        header_row.addWidget(self.button, 0, Qt.AlignLeft)
        header_row.addStretch(1)
        column.addWidget(self._header)

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(10)
        column.addWidget(self._body)

        self.button.setChecked(expanded)
        self._on_toggled(expanded)

    def body(self):
        return self._body_layout

    def add_to_header(self, widget):
        """Put a control at the right-hand end of the title's own line.

        Not the same as standing the whole section next to it. A Disclosure is
        header and body stacked inside one widget, so a hbox holding the two
        side by side takes the control's width off the *body* as well - which is
        how the Server log's box ended up 150px narrower than the card it sits
        in, for a button that shares none of its line.
        """
        self._header.layout().addWidget(widget, 0, Qt.AlignTop)
        return widget

    def set_title(self, title):
        """Change the title in place, keeping the chevron's state.

        A section whose header carries a running tally has to be able to say a
        new one without being rebuilt - rebuilding it would fold it shut and drop
        whatever was selected inside.
        """
        self._title = title
        icons.button(self.button,
                     "disclosure_open" if self.is_expanded() else "disclosure_closed",
                     title)

    def title(self):
        """What the header reads now, tally and all."""
        return self._title

    def set_expanded(self, expanded):
        self.button.setChecked(bool(expanded))

    def is_expanded(self):
        return self.button.isChecked()

    def _on_toggled(self, checked):
        self._body.setVisible(checked)
        # A chevron that turns: pointing down at an open section, right at a
        # folded one. Whatever the mark, it has to say "this opens" while the
        # section is shut, which is the state most people meet it in.
        icons.button(self.button,
                     "disclosure_open" if checked else "disclosure_closed",
                     self._title)
        self.toggled.emit(checked)


_scope_counter = itertools.count()


def scoped_style(widget, css):
    """Apply ``css`` to ``widget`` alone - never to what is inside it.

    A stylesheet set on a widget also applies to its children, and it outranks
    the application sheet. So a plain ``background:`` on a container silently
    repaints every button in it: that is how the primary RUN button in the
    preview strip ended up filled with the strip's own colour, painting its
    near-white label on a near-white background. Scoping the rule to the
    widget's own object name confines it.
    """
    name = widget.objectName() or "scoped%d" % next(_scope_counter)
    widget.setObjectName(name)
    widget.setStyleSheet("#%s { %s }" % (name, css))
    return widget


class EmptyNote(QLabel):
    """What a table says when there is nothing in it.

    A table with no rows is otherwise a header and a blank band, which reads as
    something that failed to load rather than as something not configured yet -
    and a page carrying four of them is four unanswered questions.

    It lives on the table's viewport so it scrolls and clips with the rows, and
    follows the model rather than being told: whoever fills the table has one
    less thing to remember, which is the only way this stays true.

    A table can also be empty for the other reason - every row hidden by a search
    - and the two are different claims. Rows are counted as they are *shown*, and
    :meth:`say_filtered` puts the second wording up while a search is on, so a
    filtered table never reads as an unconfigured one.
    """

    def __init__(self, table, text):
        super().__init__(text, table.viewport())
        self._table = table
        self._text = text
        self._filtered = ""
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        # The role, not a colour: a literal captured here would go on painting
        # the light palette after dark mode was set.
        self.setProperty("role", "hint")
        table.viewport().installEventFilter(self)
        model = table.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.modelReset,
                       model.layoutChanged):
            signal.connect(self.sync)
        self.sync()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize:
            self.sync()
        return False

    def say(self, text):
        """What to say when the table is empty, for a table whose reason changes -
        a filter showing nothing is not a list that has nothing in it."""
        self._text = text or ""
        self.sync()

    def say_filtered(self, text):
        """What to say instead while a search is hiding rows. "" for none."""
        self._filtered = text or ""
        self.sync()

    def sync(self, *_args):
        self.setGeometry(self._table.viewport().rect())
        self.setText(self._filtered or self._text)
        self.setVisible(not self._shown_rows())

    def _shown_rows(self):
        return sum(1 for row in range(self._table.model().rowCount())
                   if not self._table.isRowHidden(row))


def empty_note(table, text):
    """Say what an empty table means. Returns the note, which follows the table."""
    return EmptyNote(table, text)


class Elsewhere(QWidget):
    """What sits where something was, while a separate window has it.

    A blank space would read as "it stopped". A pulse reads as "it is still
    coming, just not here" - which is the one thing this has to say, and it
    says it without a second copy of the content rendered off screen. Made
    for the Run page's server log and used by the Cycles page's Stages too;
    whatever else belongs on the line goes in with :meth:`add_widget`.
    """

    show_requested = Signal()

    def __init__(self, text, show_text="Show window", parent=None):
        super().__init__(parent)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 6, 0, 6)
        self._row.setSpacing(10)

        self._dot = PulseDot(self)
        self._row.addWidget(self._dot, 0, Qt.AlignVCenter)
        self.label = QLabel(text)
        self.label.setProperty("role", "elsewhere")
        self._row.addWidget(self.label)
        self._row.addStretch(1)
        self.show_button = QPushButton(show_text)
        self.show_button.setProperty("variant", "ghost")
        self.show_button.clicked.connect(self.show_requested)
        self._row.addWidget(self.show_button)

    def add_widget(self, widget):
        """One more control at the end of the line."""
        self._row.addWidget(widget)

    def set_text(self, text):
        self.label.setText(text)

    def start(self):
        self._dot.start()

    def stop(self):
        # Stopped whenever it is not on screen: an animation nobody can see is a
        # timer waking the process up for nothing.
        self._dot.stop()


class PulseDot(QWidget):
    """A dot that breathes. Its opacity is the animated property."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self._opacity = 1.0
        self._animation = QPropertyAnimation(self, b"pulse", self)
        self._animation.setDuration(1100)
        self._animation.setStartValue(1.0)
        self._animation.setEndValue(0.2)
        self._animation.setEasingCurve(QEasingCurve.InOutSine)
        self._animation.setLoopCount(-1)
        # Back down and up again rather than snapping to full brightness.
        self._animation.setDirection(QPropertyAnimation.Forward)
        self._animation.finished.connect(self._flip)

    def _flip(self):
        self._animation.setDirection(
            QPropertyAnimation.Backward
            if self._animation.direction() == QPropertyAnimation.Forward
            else QPropertyAnimation.Forward)

    def start(self):
        if self._animation.state() != QPropertyAnimation.Running:
            self._animation.start()

    def stop(self):
        self._animation.stop()
        self._set_pulse(1.0)

    def running(self):
        return self._animation.state() == QPropertyAnimation.Running

    def _get_pulse(self):
        return self._opacity

    def _set_pulse(self, value):
        self._opacity = value
        self.update()

    pulse = Property(float, _get_pulse, _set_pulse)

    def paintEvent(self, _event):             # noqa: N802 - Qt's own spelling
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # Read now, not captured: dark mode rewrites theme.ACCENT in place.
        colour = QColor(theme.ACCENT)
        colour.setAlphaF(max(0.0, min(1.0, self._opacity)))
        painter.setBrush(colour)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))


class Busy(QWidget):
    """A page held still while something it depends on is being written.

    Covers the whole page, so nothing on it can be clicked or typed into -
    acting on a cycle that is half way through being saved would act on the
    old one. The part being written (``area``, the canvas) is veiled and says
    what is happening in the middle, with a breathing dot: the window was
    frozen during a save before this, and a frozen window cannot be told from
    a hung one.

    Input is held at once, the veil shown only after :data:`DELAY_MS`: a save
    quicker than that is over before anybody could wonder, and a veil flashed
    for a moment is only a flicker.
    """

    DELAY_MS = 150
    FRAME_MS = 40
    PERIOD_MS = 1100

    def __init__(self, host, area=None):
        super().__init__(host)
        self._host = host
        self._area = area
        self._text = ""
        self._holding = False
        self._shown = False
        self._frame = 0
        self._focus = None
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.timeout.connect(self._appear)
        self._frames = QTimer(self)
        self._frames.setInterval(self.FRAME_MS)
        self._frames.timeout.connect(self._advance)
        self.setFocusPolicy(Qt.StrongFocus)
        self.hide()
        host.installEventFilter(self)

    def start(self, text):
        """Hold the page now; veil the area and say ``text`` in a moment."""
        self._text = text
        if self._holding:
            self.update()
            return
        self._holding = True
        self._focus = QApplication.focusWidget()
        self.setGeometry(self._host.rect())
        self.raise_()
        self.show()
        self.setFocus()
        self._delay.start(self.DELAY_MS)

    def stop(self):
        self._delay.stop()
        self._frames.stop()
        self._holding = self._shown = False
        self.unsetCursor()
        self.hide()
        if self._focus is not None and _alive_widget(self._focus):
            self._focus.setFocus()
        self._focus = None

    def active(self):
        """Whether the page is being held - from :meth:`start` to :meth:`stop`."""
        return self._holding

    def veiled(self):
        """Whether the veil and its words are showing yet."""
        return self._shown

    def text(self):
        return self._text

    def _appear(self):
        self._shown = True
        self._frame = 0
        self.setCursor(Qt.BusyCursor)
        self._frames.start()
        self.update()

    def _advance(self):
        self._frame += 1
        self.update()

    def _veil_rect(self):
        if self._area is None:
            return QRectF(self.rect())
        corner = self._area.mapTo(self._host, QPoint(0, 0))
        return QRectF(corner.x(), corner.y(), self._area.width(),
                      self._area.height())

    def eventFilter(self, watched, event):
        if watched is self._host and event.type() == QEvent.Resize and self._holding:
            self.setGeometry(self._host.rect())
        return False

    def event(self, event):
        # Every click, wheel and key the page would have had stops here.
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease,
                            QEvent.MouseButtonDblClick, QEvent.Wheel,
                            QEvent.KeyPress, QEvent.KeyRelease,
                            QEvent.ContextMenu):
            event.accept()
            return True
        return super().event(event)

    def paintEvent(self, _event):             # noqa: N802 - Qt's own spelling
        if not self._shown:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        veil = self._veil_rect()
        # Read now, not captured: dark mode rewrites the palette in place.
        ground = QColor(theme.BG)
        ground.setAlphaF(0.72)
        painter.fillRect(veil, ground)

        font = painter.font()
        font.setPointSizeF(max(font.pointSizeF(), 10.0))
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        dot, gap, pad = 10.0, 10.0, 16.0
        width = pad + dot + gap + metrics.horizontalAdvance(self._text) + pad
        height = max(36.0, metrics.height() + 16.0)
        pill = QRectF(veil.center().x() - width / 2.0,
                      veil.center().y() - height / 2.0, width, height)
        painter.setPen(QPen(QColor(theme.DIVIDER), 1.0))
        painter.setBrush(QColor(theme.BG))
        painter.drawRect(pill)

        breath = self._frame * self.FRAME_MS / float(self.PERIOD_MS)
        accent = QColor(theme.ACCENT)
        accent.setAlphaF(0.25 + 0.75 * (1.0 + math.cos(2.0 * math.pi * breath)) / 2.0)
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(QRectF(pill.left() + pad, pill.center().y() - dot / 2.0,
                                   dot, dot))
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(QRectF(pill.left() + pad + dot + gap, pill.top(),
                                width - pad - dot - gap - pad, height),
                         Qt.AlignVCenter | Qt.AlignLeft, self._text)


def _alive_widget(widget):
    try:
        import shiboken6
        return shiboken6.isValid(widget)
    except ImportError:
        return True


class Skeleton(QWidget):
    """The shape of what is on its way, pulsing slowly, until it arrives.

    A region that stays as it was while its data loads looks exactly like a
    program that has stopped; one that goes blank looks like one that lost
    the data. This lays grey shapes where the rows, the lines or the graph
    will be, breathing slowly from dark to light, so a wait reads as a wait.
    Each row runs a little behind the one above it, which reads as calm rather
    than as blinking.

    It sits over its host and follows its size. It appears only after
    :data:`DELAY_MS`: an answer quicker than that is on screen before anybody
    could have wondered, and a skeleton flashed for a moment before it is
    only a flicker. While it shows it takes the clicks, because what is under
    it is about to be replaced and acting on it would act on the old thing.

    A region nobody can see - a panel folded shut - costs nothing: the host
    has no size, so there is nothing to paint.

    Colours are read from ``theme`` at paint time, never kept, so dark mode
    moves them with everything else. A region with a look of its own - the
    cycle canvas's ruled paper - subclasses :meth:`paint_ground` and
    :meth:`paint_shapes`.
    """

    #: What it can look like: the rows of a list, lines of text, or a graph.
    SHAPES = ("rows", "lines", "graph")
    #: How long an answer may take before the skeleton is worth showing.
    DELAY_MS = 180
    #: How often it is repainted, and how long one breath takes.
    FRAME_MS = 40
    PERIOD_MS = 1800
    #: How far behind the one before each row breathes, as part of a breath.
    STAGGER = 0.08

    def __init__(self, host, shape="lines", ground="BG", area=None):
        super().__init__(host)
        self._host = host
        #: Which part of the host to cover, when not all of it - a tab
        #: widget's pages without its row of tabs, say.
        self._area = area or host.rect
        self._shape = shape if shape in self.SHAPES else "lines"
        self._ground = ground
        self._frame = 0
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.timeout.connect(self._appear)
        self._frames = QTimer(self)
        self._frames.setInterval(self.FRAME_MS)
        self._frames.timeout.connect(self._advance)
        self.setAccessibleName("Loading")
        self.setToolTip("Loading...")
        self.hide()
        host.installEventFilter(self)

    # -- showing it -------------------------------------------------------------
    def start(self, delay=None):
        """Show it once the delay has passed, unless :meth:`stop` comes first."""
        if self.isVisible() or self._delay.isActive():
            return
        self._delay.start(self.DELAY_MS if delay is None else int(delay))

    def stop(self):
        self._delay.stop()
        self._frames.stop()
        self.hide()

    def active(self):
        """Waiting to appear, or showing."""
        return self._delay.isActive() or self.isVisible()

    def _appear(self):
        self._frame = 0
        self.setGeometry(self._area())
        self.raise_()
        self.show()
        self._frames.start()

    def _advance(self):
        self._frame += 1
        self.update()

    def eventFilter(self, watched, event):
        if watched is self._host and event.type() == QEvent.Resize and self.isVisible():
            self.setGeometry(self._area())
        return False

    # -- the breath ----------------------------------------------------------------
    def level(self, row=0):
        """How light row ``row`` is now, from 0 (dark) to 1 (light)."""
        breath = self._frame * self.FRAME_MS / float(self.PERIOD_MS) - row * self.STAGGER
        return (1.0 - math.cos(2.0 * math.pi * breath)) / 2.0

    def colour(self, row=0, dark=None, light=None):
        """The shade row ``row`` is painted in now, between ``dark`` and ``light``."""
        dark = QColor(dark or theme.NEUTRAL[300])
        light = QColor(light or theme.NEUTRAL[100])
        mix = self.level(row)
        return QColor(round(dark.red() + (light.red() - dark.red()) * mix),
                      round(dark.green() + (light.green() - dark.green()) * mix),
                      round(dark.blue() + (light.blue() - dark.blue()) * mix))

    # -- drawing it -------------------------------------------------------------
    def shapes(self):
        """``[(rectangle, row)]`` - where the grey goes, and which row it is in."""
        width, height = self.width(), self.height()
        if self._shape == "graph":
            return [(box, row) for row, layer in enumerate(self.layers()) for box in layer]
        found = []
        if self._shape == "rows":
            y, index = 10.0, 0
            while y + 30 < height:
                found.append((QRectF(12, y + 2, 14, 14), index))
                found.append((QRectF(34, y, (width - 46) * (0.55 + 0.3 * _spread(index)), 11),
                              index))
                found.append((QRectF(34, y + 17,
                                     (width - 46) * (0.3 + 0.2 * _spread(index + 7)), 8), index))
                y += 42
                index += 1
            return found
        y, index = 12.0, 0
        while y + 10 < height:
            found.append((QRectF(14, y, (width - 28) * (0.35 + 0.6 * _spread(index)), 9), index))
            y += 20
            index += 1
        return found

    def bars(self):
        """Where the grey goes, as rectangles - worked out from the size alone."""
        return [rect for rect, _row in self.shapes()]

    def layers(self):
        """The graph shape's boxes, one list per layer, top to bottom."""
        width, height = self.width(), self.height()
        box_w, box_h, gap = min(200.0, width * 0.42), 40.0, 26.0
        found, y, row = [], 24.0, 0
        while y + box_h < height and row < 12:
            if row % 3 == 2 and width > box_w * 2 + 36:
                # Every third layer forks, so it reads as a graph and not as a
                # list: two boxes side by side.
                found.append([QRectF(x, y, box_w, box_h)
                              for x in (width / 2.0 - box_w - 12, width / 2.0 + 12)])
            else:
                found.append([QRectF((width - box_w) / 2.0, y, box_w, box_h)])
            y += box_h + gap
            row += 1
        return found

    def paint_ground(self, painter):
        """What the region looks like with nothing in it."""
        if self._ground:
            painter.fillRect(self.rect(), QColor(getattr(theme, self._ground, theme.BG)))

    def paint_shapes(self, painter):
        """The shapes, each in its row's shade of the breath."""
        if self._shape == "graph":
            # Every box of a layer joined to every box of the next, the way a
            # fork and a join are drawn on the real canvas.
            painter.setPen(QPen(QColor(theme.NEUTRAL[400]), 1.2))
            layers = self.layers()
            for above, below in zip(layers, layers[1:]):
                for one in above:
                    for two in below:
                        painter.drawLine(QPointF(one.center().x(), one.bottom()),
                                         QPointF(two.center().x(), two.top()))
        painter.setPen(Qt.NoPen)
        radius = 6 if self._shape == "graph" else 3
        for rect, row in self.shapes():
            painter.setBrush(QBrush(self.colour(row)))
            painter.drawRoundedRect(rect, radius, radius)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self.paint_ground(painter)
        self.paint_shapes(painter)
        painter.end()


def _spread(index):
    """A width between 0 and 1 that varies from row to row, the same every time.

    Deterministic, so a skeleton does not jitter between repaints; varied, so
    it reads as text of different lengths rather than as a ruled grid.
    """
    return ((index * 7919) % 97) / 97.0


def start_dir(hint, fallback="~"):
    """Where a chooser should open, given whatever the field says now.

    The reason this is worth a function: the paths this application asks for live
    in dotted directories - a project's interpreter is ``.venv/bin/python``, an
    ssh key is in ``~/.ssh`` - and a file chooser does not list those. Opening
    *inside* one does show its contents, though, because they are not themselves
    hidden. So a chooser that starts where the answer already is asks nobody to
    know about Ctrl+H.
    """
    for candidate in (hint, fallback):
        path = os.path.expanduser((candidate or "").strip())
        if not path:
            continue
        if os.path.isdir(path):
            return path
        parent = os.path.dirname(path)
        if os.path.isdir(parent):
            return parent
    return os.path.expanduser("~")


def pick_path(parent, title, start="", directory=False, save=False):
    """The platform's own file chooser, opened where the answer is likely to be.

    Native on purpose: Qt's own dialog is the only one whose hidden-file filter
    can be set, but under the Fusion style it draws its toolbar marks from an
    icon theme that is not there, and it looks nothing like the rest of the
    desktop. :func:`start_dir` solves the dotted-directory problem without it.
    """
    where = start_dir(start)
    if directory:
        return QFileDialog.getExistingDirectory(parent, title, where) or ""
    if save:
        name = os.path.basename(os.path.expanduser((start or "").strip()))
        chosen, _filter = QFileDialog.getSaveFileName(
            parent, title, os.path.join(where, name) if name else where)
        return chosen or ""
    chosen, _filter = QFileDialog.getOpenFileName(parent, title, where)
    return chosen or ""


def heading(text, role="h1"):
    label = QLabel(text)
    label.setProperty("role", role)
    return label


def elided_line(text="", role="h1"):
    """A heading or a status that may be long, in a row it shares with buttons.

    It asks for its whole text and gets it when there is room, but gives way
    before the buttons do: a cycle's name in the header was enough to hold the
    window wider than a laptop screen, and so unable to maximize.
    """
    label = ElidedLabel(text, Qt.ElideRight)
    label.setProperty("role", role)
    label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
    return label


#: The widest a picker in the main window grows to fit its items.
PICKER_MAX_WIDTH = 260


def capped(combo, width=PICKER_MAX_WIDTH):
    """A picker as wide as its items, up to ``width``, and no wider.

    A combo sizes itself to its longest item when it first shows, and every
    page in the window's stack sets the window's minimum width: one 45-character
    session name in the Log page's picker held the window wider than a 1366 px
    screen, and so unable to maximize. Past the cap the text is shortened; the
    list still opens as wide as its items.
    """
    combo.setMaximumWidth(width)
    return combo


def kicker(text):
    return heading(text.upper(), "kicker")


def lede(text):
    label = QLabel(text)
    label.setProperty("role", "lede")
    label.setWordWrap(True)
    return label


def empty_zone(title, detail):
    """A whole region saying nothing is configured yet, on its own ground.

    :func:`empty_note` answers the same question for one table, from inside it.
    This is for the case where there is no table to be inside: a page whose main
    subject has no rows at all shows only the parts that are always there, which
    reads as a page that failed to load rather than as one waiting to be filled.

    Dashed rather than solid, and taller than it needs to be: the shape says
    "something goes here" without pretending to be a row already.
    """
    frame = QFrame()
    frame.setProperty("role", "emptyzone")
    box = QVBoxLayout(frame)
    box.setContentsMargins(20, 26, 20, 26)
    box.setSpacing(6)
    head = QLabel(title)
    head.setProperty("role", "emptytitle")
    head.setAlignment(Qt.AlignCenter)
    body = QLabel(detail)
    body.setProperty("role", "hint")
    body.setAlignment(Qt.AlignCenter)
    body.setWordWrap(True)
    box.addWidget(head)
    box.addWidget(body)
    return frame


def mono(text=""):
    label = QLabel(text)
    label.setProperty("role", "mono")
    return label


def elided_mono(text="", mode=Qt.ElideMiddle):
    """Monospace machine text that may be a long path. See :class:`ElidedLabel`."""
    label = ElidedLabel(text, mode)
    label.setProperty("role", "mono")
    return label


def hline():
    line = QFrame()
    line.setProperty("role", "hline")
    line.setFixedHeight(1)
    line.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return line


def vline(height=20):
    line = QFrame()
    line.setProperty("role", "vline")
    line.setFixedSize(1, height)
    return line


class Phrasing:
    """The same thing said two ways: plainly, and with the flag named.

    Developer mode is the switch between the two interaction levels, and the
    wording belongs to a level as much as the pages do. "--flows-dir" is noise
    to someone launching sessions and the only thing worth knowing to someone
    building a command line, so a string that names a flag is written twice and
    the mode picks which one is showing. Nothing is hidden either way: the plain
    wording says what the control does, not less.

    A page collects its pairs as it builds - each call sets the plain wording and
    hands the widget straight back, so it still reads as one line - and applies
    them from its own ``set_developer_mode``. The mode itself is the window's.
    """

    def __init__(self):
        self._entries = []

    def text(self, widget, plain, developer):
        """Register a widget's label text. Returns the widget, ready to add."""
        self._entries.append((widget.setText, plain, developer))
        widget.setText(plain)
        return widget

    def apply(self, developer):
        for setter, plain, spelled_out in self._entries:
            setter(spelled_out if developer else plain)


def field(label_text, widget, hint=None):
    """A labelled control: the design's `.field` (small label above the input)."""
    box = QWidget()
    column = QVBoxLayout(box)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(4)
    label = QLabel(label_text)
    label.setStyleSheet("font-size: 11px; color: %s; font-family: %s;"
                        % (theme.NEUTRAL[700], theme.MONO_CSS))
    column.addWidget(label)
    column.addWidget(widget)
    if hint:
        note = QLabel(hint)
        note.setStyleSheet("font-size: 11px; color: %s;" % theme.NEUTRAL[600])
        note.setWordWrap(True)
        column.addWidget(note)
    # Kept reachable so a form can relabel a control whose meaning depends on
    # another answer - a log's target is a path, a container, a unit or a URL.
    box.label = label
    return box


class _LevelWith(QObject):
    """Keeps buttons as tall as the control they stand beside (see level_with)."""

    def __init__(self, anchor, buttons):
        super().__init__(anchor)
        self._buttons = list(buttons)
        anchor.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize:
            for button in self._buttons:
                button.setFixedHeight(watched.height())
        return False


def level_with(anchor, row_widget, *buttons):
    """Stand ``buttons`` level with ``anchor`` in ``row_widget``'s row.

    For a row where a labelled control (a combo with its caption above it)
    sits beside plain buttons: centred on the row, the buttons stood level with
    the caption and the control together - higher than the control. Here they
    take the control's height, whatever the stylesheet makes it, and its
    bottom edge, so the two share their top, bottom and centre.
    """
    for button in buttons:
        row_widget.layout().setAlignment(button, Qt.AlignBottom)
    return _LevelWith(anchor, buttons)


def see_through(section):
    """Let a ``role="section"`` frame's ground show through what it holds.

    The theme gives every QWidget the page's background, so the rows, fields
    and lists inside a section would each paint a patch of page over it. The
    plain containers among them - and a list's viewport - are marked
    transparent; inputs and buttons keep their own fill. Call it once the
    section is filled.
    """
    for child in section.findChildren(QWidget):
        if type(child) is QWidget or isinstance(child, (CheckList, Segmented,
                                                         QListWidget)):
            child.setProperty("seethrough", "true")


def row(*widgets, spacing=8, stretch_last=False):
    """A horizontal strip; ints become fixed spacers, None becomes a stretch."""
    box = QWidget()
    line = QHBoxLayout(box)
    line.setContentsMargins(0, 0, 0, 0)
    line.setSpacing(spacing)
    for widget in widgets:
        if widget is None:
            line.addStretch(1)
        elif isinstance(widget, int):
            line.addSpacing(widget)
        else:
            line.addWidget(widget)
    if stretch_last and line.count():
        line.setStretch(line.count() - 1, 1)
    return box


def dress(window, title="", resizable=True, minimizable=False):
    """Put the application's own title bar on a window. Returns its frame.

    Every window that is not the main one kept the desktop's frame, so the
    application ended at the top of each of them and the operating system
    started - which is the one part of a window somebody looks at to know what
    they are in. One call rather than ten copies of it, because the tenth copy
    is the one that gets forgotten.

    **Resizable by default, and that is not a preference.** Taking the
    desktop's frame off takes its resize handles with it, so every window
    dressed without them silently loses something it had - and a form whose
    field is too small to read what is in it is a form somebody cannot use.
    Only a dialog that never had a frame to begin with says no.

    ``minimizable`` adds minimize and maximize, for the windows somebody
    *works* in - a console filling with output, a log being read - rather than
    fills in and closes.
    """
    from . import titlebar

    if not titlebar.wanted():
        return None
    return titlebar.install(
        window, title=title or window.windowTitle(),
        controls=titlebar.CONTROLS if minimizable else ("close",),
        grips=resizable)


class Message(QDialog):
    """Something said to somebody, in the application's own clothes.

    ``QMessageBox`` is not styled by the stylesheet - it draws its own icon,
    asks the platform for its buttons and keeps the desktop's frame - so on
    every page it arrived looking like every other application on the machine
    and none of this one. Built out of the same widgets as everything else, a
    dialog picks the theme up for nothing, dark mode included, and wears the
    title bar the main window wears.

    Three parts, and the middle one is why this is a class rather than a
    one-line wrapper: what happened, the **consequence** in smaller type under
    it, and the answers. A dialog that only says "are you sure" tells somebody
    nothing they did not know when they pressed the button.

    ``refuse`` is what makes it a question. Left empty there is one button and
    this is a notice; given, there are two and the answer means something.

    ``output`` is for the dialogs that carry what a command printed: a
    read-only mono box that scrolls, rather than a label, because somebody
    reading a traceback wants to select a line out of it. ``choices`` is for
    the rare case with more than two answers - "continue this one", "start a
    new one", "cancel" is three, and folding it into a yes/no would lose the
    middle one.
    """

    #: kind -> the colour of the rule down the left of the text. Named, and
    #: read at build time from theme, so dark mode moves them with everything
    #: else. A plain notice has no rule: most of what this shows is ordinary.
    RULES = {"warn": "WARN", "error": "BAD", "ok": "OK"}

    #: How thick that rule is, and how far the text sits off it.
    RULE = 3
    RULE_GAP = 12

    #: How much of a command's output is shown before the box starts to
    #: scroll. Tall enough for a traceback to be read rather than guessed at.
    OUTPUT_HEIGHT = 260

    #: How tall a several-line answer's box is, in lines of its own text.
    TEXT_LINES = 5

    def __init__(self, parent, title, message, detail="", kind="",
                 agree="OK", refuse="", output="", choices=(), text=None,
                 items=(), lines=1):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self.frame = dress(self, title, resizable=False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        said = QVBoxLayout()
        said.setContentsMargins(0, 0, 0, 0)
        said.setSpacing(8)
        self.message = QLabel(message)
        self.message.setWordWrap(True)
        self.message.setStyleSheet("font-size: 14px; font-weight: 600;")
        said.addWidget(self.message)
        self.detail = None
        if detail:
            self.detail = QLabel(detail)
            self.detail.setWordWrap(True)
            self.detail.setStyleSheet("color: %s;" % theme.NEUTRAL[700])
            said.addWidget(self.detail)

        colour = self.RULES.get(kind)
        if colour:
            rule = QFrame()
            rule.setFixedWidth(self.RULE)
            rule.setStyleSheet("background: %s;" % getattr(theme, colour))
            beside = QHBoxLayout()
            beside.setContentsMargins(0, 0, 0, 0)
            beside.setSpacing(self.RULE_GAP)
            beside.addWidget(rule)
            beside.addLayout(said, 1)
            layout.addLayout(beside)
        else:
            layout.addLayout(said)

        self.output = None
        if output:
            self.output = QPlainTextEdit(output)
            self.output.setReadOnly(True)
            self.output.setFont(theme.mono_font())
            self.output.setMaximumHeight(self.OUTPUT_HEIGHT)
            layout.addWidget(self.output, 1)

        # An answer to type or to pick, under the question that asks for it.
        # QInputDialog was the last of the desktop's dialogs on these pages:
        # its own frame, the platform's icons on its buttons, nothing of the
        # theme - the one window that looked like another application.
        self.field = None
        if items:
            self.field = QComboBox()
            self.field.addItems([str(one) for one in items])
            layout.addWidget(self.field)
        elif text is not None and lines > 1:
            # Words that may run to a few lines - a note - get a box that
            # shows them; Ctrl+Enter agrees, since Enter is a new line here.
            self.field = QPlainTextEdit(text)
            self.field.setTabChangesFocus(True)
            self.field.setFixedHeight(
                self.field.fontMetrics().lineSpacing() * max(lines, 2) + 14)
            layout.addWidget(self.field)
            for keys in ("Ctrl+Return", "Ctrl+Enter"):
                QShortcut(QKeySequence(keys), self, activated=self.accept)
        elif text is not None:
            self.field = QLineEdit(text)
            self.field.selectAll()
            layout.addWidget(self.field)

        #: Which of ``choices`` was pressed, or "" - the agree button counts
        #: as its own label, so one reading answers both shapes.
        self.chosen = ""
        self.choice_buttons = []
        for label in choices:
            button = QPushButton(label)
            button.setProperty("variant", "ghost")
            button.clicked.connect(
                lambda _checked=False, text=label: self._choose(text))
            self.choice_buttons.append(button)

        self.refuse_button = None
        if refuse:
            self.refuse_button = QPushButton(refuse)
            self.refuse_button.setProperty("variant", "ghost")
            self.refuse_button.clicked.connect(self.reject)
        self.agree_button = QPushButton(agree)
        self.agree_button.setProperty("variant", "primary")
        self.agree_button.clicked.connect(self.accept)
        self.agree_button.setDefault(True)
        self.agree_button.clicked.connect(
            lambda: setattr(self, "chosen", agree))

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        if self.refuse_button is not None:
            buttons.addWidget(self.refuse_button)
        for button in self.choice_buttons:
            buttons.addWidget(button)
        buttons.addWidget(self.agree_button)
        layout.addLayout(buttons)

    def _choose(self, label):
        self.chosen = label
        self.accept()

    def answer(self):
        """What was typed or picked, stripped; "" when there is no field."""
        if isinstance(self.field, QComboBox):
            return self.field.currentText()
        if isinstance(self.field, QPlainTextEdit):
            return self.field.toPlainText().strip()
        return self.field.text().strip() if self.field is not None else ""

    def showEvent(self, event):         # noqa: N802 - Qt's own spelling
        super().showEvent(event)
        if self.field is not None:
            self.field.setFocus()


def confirm(parent, title, question, detail="", agree="Yes", refuse="Cancel",
            kind=""):
    """Ask, and say whether they agreed."""
    return _shown(Message(parent, title, question, detail, kind, agree, refuse))


def warn(parent, title, message, detail=""):
    """Something went wrong, and there is one thing to say about it.

    Named for the call it replaces - ``QMessageBox.warning`` - so converting a
    page was a change of name and nothing else. Sixty-odd of these were spread
    over eleven files; rewriting each one's arguments as well would have been
    sixty chances to change what a page says by accident.
    """
    return _shown(Message(parent, title, message, detail, "error"))


def note(parent, title, message, detail=""):
    """Something worth saying that is not a problem."""
    return _shown(Message(parent, title, message, detail))


def choose(parent, title, message, detail="", choices=(), agree="OK",
           refuse="Cancel", kind=""):
    """More than two answers. Returns the label pressed, or "" for none.

    The label rather than an index, because a caller comparing against the
    words it wrote down cannot get the order wrong the day a third answer is
    added in the middle.
    """
    dialog = Message(parent, title, message, detail, kind, agree, refuse,
                     choices=choices)
    try:
        return dialog.chosen if dialog.exec() == QDialog.Accepted else ""
    finally:
        dialog.deleteLater()


def ask_text(parent, title, question, value="", detail="", agree="OK",
             refuse="Cancel", lines=1):
    """A line of text, asked for. Returns it stripped, or None if refused.

    None rather than "" for a refusal, so a caller can tell "cancelled" from
    "agreed to nothing" - the second is usually a field it should refuse.
    ``lines`` above 1 asks for several lines in a box that tall.
    """
    dialog = Message(parent, title, question, detail, "", agree, refuse,
                     text=value or "", lines=lines)
    try:
        return dialog.answer() if dialog.exec() == QDialog.Accepted else None
    finally:
        dialog.deleteLater()


def ask_item(parent, title, question, items, current=0, detail="",
             agree="OK", refuse="Cancel"):
    """One of ``items``, picked from a list. Returns it, or None if refused."""
    dialog = Message(parent, title, question, detail, "", agree, refuse,
                     items=items)
    dialog.field.setCurrentIndex(max(0, min(int(current), len(items) - 1)))
    try:
        return dialog.answer() if dialog.exec() == QDialog.Accepted else None
    finally:
        dialog.deleteLater()


def output(parent, title, message, text="", kind=""):
    """What a command printed, in a box somebody can select a line out of."""
    return _shown(Message(parent, title, message, "", kind, output=text))


def confirm_output(parent, title, question, detail="", text="", agree="Yes",
                   refuse="Cancel", kind="warn"):
    """Ask, with the thing being agreed to shown in full - a diff, say.

    For the questions whose honest answer depends on reading something: "save
    this?" means little until the lines that change are in front of you.
    """
    return _shown(Message(parent, title, question, detail, kind, agree, refuse,
                          output=text))


def _shown(dialog):
    try:
        return dialog.exec() == QDialog.Accepted
    finally:
        dialog.deleteLater()


class NoWheelSteal(QObject):
    """Stops a scroll wheel changing the value of a dropdown or a spin box.

    Qt's default is that the wheel moves a combo box's selection whenever the
    pointer is over it. On a form inside a scroll area that is a trap: somebody
    scrolls the page, the pointer happens to pass over a dropdown, and the
    value changes underneath them - silently, without a click, and often
    without being noticed until something runs with the wrong setting.

    So the wheel never changes one of these. The event is not simply eaten,
    though: it is handed to whatever the widget is scrolling **inside**, so the
    page still moves. Eating it would trade a silent wrong value for a page
    that mysteriously refuses to scroll over half its own controls.

    Installed on the controls and their editors. The application style attaches
    it when polishing new widgets, so dialogs created later are covered too.
    An application-wide Python event filter crashes PySide6 when WebEngine
    delivers events for its private QObjects, before eventFilter is even called.
    """

    #: The widgets whose value a stray wheel must not change.
    WATCHED = (QComboBox, QAbstractSpinBox)

    def __init__(self, app):
        super().__init__(app)
        self._watched = weakref.WeakSet()

    def watch(self, widget):
        """Called for existing controls, and by the style as new ones appear."""
        if isinstance(widget, self.WATCHED):
            targets = [widget] + widget.findChildren(QLineEdit)
        elif isinstance(widget, QLineEdit) and _guarded(widget) is not None:
            targets = [widget]
        else:
            return
        for target in targets:
            if target not in self._watched:
                target.installEventFilter(self)
                self._watched.add(target)

    def stop(self):
        """Detach without retaining the controls or their containing windows."""
        for widget in list(self._watched):
            try:
                widget.removeEventFilter(self)
            except RuntimeError:       # Qt may already have deleted the widget
                pass
        self._watched.clear()
        app = self.parent()
        if getattr(app, "_no_wheel_steal", None) is self:
            app._no_wheel_steal = None
        self.deleteLater()

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Wheel:
            return False
        guarded = _guarded(watched)
        if guarded is None:
            return False
        if _list_of(guarded) is watched:
            return False          # the open list itself: scrolling it is right
        area = _scroller(guarded)
        if area is not None:
            QApplication.sendEvent(area.viewport(), event)
        return True


def _guarded(widget):
    """The dropdown or spin box this event is really about, or None.

    Walked up from whatever received it rather than tested directly, because
    the wheel does not always land on the control itself: an editable combo
    box and every spin box are made of child widgets, and a wheel over the
    line edit inside one is a wheel over the control as far as anybody using
    it is concerned.
    """
    while widget is not None:
        if isinstance(widget, NoWheelSteal.WATCHED):
            return widget
        if not isinstance(widget, QWidget):
            return None
        widget = widget.parentWidget()
    return None


def _list_of(box):
    """The list a dropdown opens, if it has one and it is showing."""
    view = getattr(box, "view", None)
    return view() if callable(view) else None


def _scroller(widget):
    """The nearest ancestor that scrolls, or None."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


def stop_wheel_stealing(app):
    """Guard existing controls; the application style guards new ones on polish."""
    previous = getattr(app, "_no_wheel_steal", None)
    if previous is not None:
        previous.stop()
    filter_ = NoWheelSteal(app)
    app._no_wheel_steal = filter_
    for widget in app.allWidgets():
        filter_.watch(widget)
    return filter_


# -- a splitter that says where a panel went ------------------------------------
#: How thick the mark over a folded panel is, in px. Thick on purpose: the
#: ordinary handle is a hairline, and a panel dragged shut behind a hairline is
#: a panel nobody can find again.
FOLD_MARK = 4

#: How much of its edge the mark covers, centred on it. Half, so the marks of
#: two panels folded at one corner stay apart instead of meeting in an L.
FOLD_SPAN = 0.5

#: What a folded panel comes back at when it was never seen open.
FOLD_RESTORE = 240


class _FoldMark(QWidget):
    """The bold line over a folded panel's edge. Clicking it opens the panel."""

    clicked = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("A panel is folded away here. Click to show it.")

    def paintEvent(self, _event):
        from PySide6.QtGui import QColor, QPainter
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        # Read at paint time: theme.set_dark_mode rewrites the palette in
        # place, and a colour captured when the mark was made would be stale.
        painter.setBrush(QColor(theme.ACCENT))
        radius = min(self.width(), self.height()) / 2.0
        painter.drawRoundedRect(QRectF(self.rect()), radius, radius)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class FoldingSplitter(QSplitter):
    """A splitter whose panels can be dragged shut without being lost.

    Dragging a panel all the way closed is how a splitter hides one, and the
    only trace left is its handle - a one-pixel divider indistinguishable from
    the border of whatever sits beside it. So a folded panel gets a bold line
    in the accent colour along the edge it went into, and clicking the line
    brings the panel back at the size it last had.

    The line is a widget laid over the splitter rather than a restyled handle:
    every handle of a splitter shares one width, so a handle cannot be thick
    for the folded panel and a hairline for the open one beside it.
    """

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._marks = {}                 # panel index -> _FoldMark
        self._open_sizes = {}            # panel index -> last size seen open
        self._store = None               # (settings, key) - see remember_in
        self.splitterMoved.connect(lambda *_args: self._moved())

    def remember_in(self, settings, key):
        """Keep how this splitter is left - folds included - across restarts.

        Restores what ``settings`` holds under ``key`` now, and writes it back
        whenever a person moves a handle or opens a folded panel. Only those:
        the sizes a layout pass happens to produce are not a choice anybody
        made. A stored state for a different number of panels is ignored - it
        belongs to an older version of the page.
        """
        self._store = (settings, key)
        try:
            state = settings.splitter(key) or {}
        except Exception:                # noqa: BLE001 - a view preference
            state = {}
        sizes = state.get("sizes") or []
        if (len(sizes) == self.count() and all(isinstance(one, int) and one >= 0
                                               for one in sizes) and any(sizes)):
            for index, size in enumerate(state.get("open") or []):
                if isinstance(size, int) and size > 0 and index < self.count():
                    self._open_sizes[index] = size
            self.setSizes(sizes)

    def _moved(self):
        self._refresh()
        self._save()

    def _save(self):
        if self._store is None:
            return
        settings, key = self._store
        try:
            settings.save_splitter(key, {
                "sizes": self.sizes(),
                "open": [self._open_sizes.get(index, 0)
                         for index in range(self.count())]})
        except Exception:                # noqa: BLE001 - a view preference
            pass

    def folded(self):
        """Which panels are folded shut, by index."""
        return [index for index, size in enumerate(self.sizes())
                if size == 0 and self.widget(index) is not None
                and not self.widget(index).isHidden()]

    def unfold(self, index):
        """Open a folded panel again, taking the room from its widest sibling."""
        sizes = self.sizes()
        if not 0 <= index < len(sizes) or sizes[index]:
            return
        want = self._open_sizes.get(index) or FOLD_RESTORE
        donor = max((one for one in range(len(sizes)) if one != index),
                    key=lambda one: sizes[one], default=None)
        if donor is None:
            return
        give = min(want, max(sizes[donor] - FOLD_RESTORE // 2, 0)) or want // 2
        sizes[index], sizes[donor] = give, max(sizes[donor] - give, 0)
        self.setSizes(sizes)
        self._refresh()
        self._save()

    def childEvent(self, event):
        """Keep the marks out of the panels.

        A splitter makes every child widget it is given into another panel,
        so a mark parented here would become a fourth pane of zero width -
        folded, and so given a mark of its own.
        """
        if isinstance(event.child(), _FoldMark):
            return
        super().childEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh()

    def setSizes(self, sizes):
        super().setSizes(sizes)
        self._refresh()

    def _refresh(self):
        sizes = self.sizes()
        for index, size in enumerate(sizes):
            if size:
                self._open_sizes[index] = size
        folded = set(self.folded())
        for index in list(self._marks):
            if index not in folded:
                self._marks[index].hide()
        horizontal = self.orientation() == Qt.Horizontal
        for index in folded:
            mark = self._marks.get(index)
            if mark is None:
                # Built without a parent and handed over once it is whole, so
                # childEvent can tell what it is - see there.
                mark = self._marks[index] = _FoldMark(None)
                mark.setParent(self)
                mark.clicked.connect(lambda one=index: self.unfold(one))
            # Along the edge the panel was pushed against: the start for the
            # first panel, the far end for the last, its own handle otherwise.
            extent = self.width() if horizontal else self.height()
            if index == 0:
                edge = 0
            elif index == self.count() - 1:
                edge = extent - FOLD_MARK
            else:
                area = self.handle(index).geometry()
                edge = area.left() if horizontal else area.top()
            edge = max(0, min(edge, extent - FOLD_MARK))
            across = self.height() if horizontal else self.width()
            length = max(int(across * FOLD_SPAN), FOLD_MARK)
            start = (across - length) // 2
            if horizontal:
                mark.setGeometry(edge, start, FOLD_MARK, length)
            else:
                mark.setGeometry(start, edge, length, FOLD_MARK)
            mark.show()
            mark.raise_()


# --- tables ------------------------------------------------------------------
# The services page's table and its search row, shared: a dialog that shows rows
# to pick from is a table too, and two recipes for one thing is how the columns
# and the boxes over them stop lining up.

#: How much of the visible table one opened row's details may take before they
#: scroll inside themselves: enough to read, never so much that the rows around
#: it - what somebody came to the list to compare - leave the screen.
DETAIL_SHARE = 2.0 / 3.0
#: The least an opened row's details are given, whatever the window's height.
DETAIL_MIN = 160

class Table(QTableWidget):
    """A table whose selected row can be un-selected, and which can carry a
    search row under its header.

    **Selection.** Clicking a selected row clears it. Selection is what aims Open
    Tail and Open Full at a log, and what narrows a project's Start and Stop to
    part of it, so there has to be a way to aim at nothing. With one row in the
    list there was none: the first click selected it and every click after that
    re-selected the same thing, and a list of one could never be returned to
    having nothing chosen. Where the table selects several rows at a time, Qt
    already toggles on click, so this stands aside.

    **The search row** is a child of the *view* rather than of the page, parked
    in the margin between the header and the first row: that way it stays put
    while the rows change under it, and it is measured in the table's own
    coordinates, which is what lets a box line up with the column it searches.
    That margin belongs to QTableView - it recomputes it on every geometry change
    to make room for the header - so the only place to claim a strip of it is
    here, straight after the base class has had its say.

    **Rows that open in place** (``expandable=True``). :meth:`open_row` puts a
    widget in a row of its own straight under a row, across every column, so
    the columns above and below it stay where they were and the list stays one
    list rather than a list beside a page. That row is told from a row of data
    by a mark on its first cell rather than by a map of row numbers, which every
    insert above it would shift. The cursor steps over it, the search row shows
    and hides it with the row it belongs to, and Right or Enter on a row asks
    for it to open, Left for it to close (:attr:`toggle_requested`) - the page
    decides what opening means, because only it knows what to put there.
    """

    #: The mark an opened row's details carry on their first cell.
    DETAIL_ROLE = Qt.UserRole + 41

    #: (row, open): the person asked for a row's details to open or close.
    toggle_requested = Signal(int, bool)

    def __init__(self, *args, reclick_clears=True, expandable=False, **kwargs):
        """``reclick_clears=False`` keeps a row selected when it is clicked
        again: for a table whose selection only says which row is being looked
        at, where clearing it would blank what is shown for that row.
        ``expandable`` lets rows open in place (see the class docstring)."""
        super().__init__(*args, **kwargs)
        self._reclick_clears = reclick_clears
        self._expandable = expandable
        self._search_row = None
        if expandable:
            # By the pixel: an opened row is far taller than the others, and
            # scrolling a row at a time jumps over it in one go.
            self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        # updateGeometries runs again from inside setViewportMargins, and the
        # nested call would hand the margins back to the header alone - which is
        # exactly the strip we are claiming.
        self._laying_out = False

    def set_search_row(self, widget):
        self._search_row = widget
        widget.setParent(self)
        widget.show()
        self.updateGeometries()

    def top_margin(self):
        """Header plus search row: everything above the first row of data."""
        return self.viewportMargins().top()

    def updateGeometries(self):
        if not hasattr(self, "_laying_out"):
            # Called by Qt from its own constructor and destructor, when the
            # Python half of this table is not there yet or no longer is - a
            # table given its own header is laid out while that header is
            # swapped in, and again while both are torn down.
            try:
                super().updateGeometries()
            except RuntimeError:
                pass
            return
        if self._laying_out:
            return
        self._laying_out = True
        try:
            super().updateGeometries()
            row = self._search_row
            if row is None:
                return
            margins = self.viewportMargins()
            height = row.sizeHint().height()
            self.setViewportMargins(margins.left(), margins.top() + height,
                                    margins.right(), margins.bottom())
            row.setGeometry(margins.left() + self.frameWidth(),
                            margins.top() + self.frameWidth(),
                            self.viewport().width(), height)
        finally:
            self._laying_out = False

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if (self._reclick_clears
                and self.selectionMode() == QAbstractItemView.SingleSelection
                and index.isValid()
                and index.row() in {i.row() for i in self.selectedIndexes()}):
            self.clearSelection()
            self.setCurrentIndex(self.model().index(-1, -1))
            return
        super().mousePressEvent(event)

    # -- rows that open in place --------------------------------------------------
    def is_detail_row(self, row):
        """Whether ``row`` holds another row's details rather than data."""
        item = self.item(row, 0) if 0 <= row < self.rowCount() else None
        return bool(item is not None and item.data(self.DETAIL_ROLE))

    def detail_of(self, row):
        """The widget open under ``row``, or None when it is closed."""
        if self.is_detail_row(row + 1):
            return self.cellWidget(row + 1, 0)
        return None

    def owner_of(self, row):
        """The row of data ``row`` belongs to: itself, or the one above its details."""
        return row - 1 if self.is_detail_row(row) else row

    def data_rows(self):
        """Every row of data, in order - the details rows left out."""
        return [row for row in range(self.rowCount()) if not self.is_detail_row(row)]

    def open_row(self, row, widget):
        """Put ``widget`` under ``row``, across every column. Returns its row.

        Opening a row already open replaces what was there.
        """
        self.close_row(row)
        below = row + 1
        self.insertRow(below)
        holder = QTableWidgetItem()
        # Never selected and never edited: there is nothing here to aim at but
        # the widget, which takes its own clicks.
        holder.setFlags(Qt.ItemIsEnabled)
        holder.setData(self.DETAIL_ROLE, True)
        self.setItem(below, 0, holder)
        if self.columnCount() > 1:
            self.setSpan(below, 0, 1, self.columnCount())
        self.setCellWidget(below, 0, widget)
        self.setRowHidden(below, self.isRowHidden(row))
        self.fit_detail(row)
        return below

    def close_row(self, row):
        """Take away what is open under ``row``. True when something was."""
        if self.detail_of(row) is None:
            return False
        # Removing the row deletes its widget with it: whoever opened it keeps
        # no reference past this.
        self.removeRow(row + 1)
        return True

    def fit_detail(self, row):
        """Size the details open under ``row`` to what they hold, up to a share
        of the table (see :meth:`detail_cap`), past which they scroll."""
        widget = self.detail_of(row)
        if widget is None:
            return
        cap = self.detail_cap(row)
        self.setRowHeight(row + 1, max(1, min(widget.sizeHint().height(), cap)))
        # And the widget moved into the row as it now is. The view puts a cell's
        # widget in place when it next lays itself out, which a spanned row that
        # got shorter does not always prompt: the widget kept its old height and
        # hung over the row below.
        self.updateEditorGeometries()

    def detail_cap(self, row=None):
        """The tallest an opened row's details are allowed to be just now.

        A share of the view (:data:`DETAIL_SHARE`), so the rows around them stay
        in sight - or, for details whose ``fills_view()`` says so, all of it but
        their own row: what is read as it grows, a run's Cockpit or its feed,
        needs a window's worth of room to be read at all.
        """
        widget = self.detail_of(row) if row is not None else None
        fills = getattr(widget, "fills_view", None)
        if callable(fills) and fills():
            return max(DETAIL_MIN, self.viewport().height() - self.rowHeight(row))
        return max(DETAIL_MIN, int(self.viewport().height() * DETAIL_SHARE))

    def keyPressEvent(self, event):
        if self._expandable and self.currentRow() >= 0:
            row = self.owner_of(self.currentRow())
            key = event.key()
            opened = self.detail_of(row) is not None
            if key == Qt.Key_Right and not opened:
                self.toggle_requested.emit(row, True)
                return
            if key == Qt.Key_Left and opened:
                self.toggle_requested.emit(row, False)
                return
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self.toggle_requested.emit(row, not opened)
                return
        super().keyPressEvent(event)

    def moveCursor(self, action, modifiers):                 # noqa: N802 - Qt's name
        index = super().moveCursor(action, modifiers)
        if not self._expandable or not index.isValid() or not self.is_detail_row(index.row()):
            return index
        # The cursor lands on rows of data only: details are read where they
        # are, not stepped into.
        down = action in (QAbstractItemView.MoveDown, QAbstractItemView.MoveNext,
                          QAbstractItemView.MovePageDown, QAbstractItemView.MoveEnd)
        step = 1 if down else -1
        row = index.row() + step
        while 0 <= row < self.rowCount() and (self.is_detail_row(row) or self.isRowHidden(row)):
            row += step
        if not 0 <= row < self.rowCount():
            return self.currentIndex()
        return self.model().index(row, index.column())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._expandable:
            for row in self.data_rows():
                self.fit_detail(row)


class CellEditor(QStyledItemDelegate):
    """Typing into a table cell: an editor that is the cell, not a form field.

    Qt's default editor is a styled QLineEdit, which in this design is 30px
    tall with a field's padding - in a 30px row less the item padding it came
    out taller than the row, shifted right of the text it replaced and hanging
    over the gridlines. This one takes the cell's rectangle exactly, the
    cell's font, and puts the text where the cell had it.
    """

    def createEditor(self, parent, option, index):      # noqa: N802 - Qt's name
        editor = QLineEdit(parent)
        editor.setProperty("role", "celledit")
        font = index.data(Qt.FontRole)
        if font is not None:
            editor.setFont(font)
        return editor

    def updateEditorGeometry(self, editor, option, index):  # noqa: N802
        editor.setGeometry(option.rect)

    def paint(self, painter, option, index):
        # The theme styles ::item, and a styled item never paints the
        # background its model gives it - so a row marked with one (url, a
        # reserved variable) is painted here, under the text, unless selected.
        brush = index.data(Qt.BackgroundRole)
        if brush is not None and not option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, brush)
        QStyledItemDelegate.paint(self, painter, option, index)


def cell_editing(table):
    """Give ``table`` the :class:`CellEditor`. Returns the table."""
    table.setItemDelegate(CellEditor(table))
    return table


class SearchRow(QWidget):
    """One search box per column, under the table's header.

    A box per column rather than one over the whole row, because the columns ask
    different questions: "postgres" in Name finds a service, in Config it finds
    every service that mentions the container, and in Status it finds nothing at
    all. Typing in more than one narrows - they are joined with AND, which is what
    a row of boxes looks like it does.

    What is searched is what the table *says*, cell by cell, not the rows behind
    it. So a search matches exactly what a person can read on screen, including
    the columns that are computed rather than stored - a service's status, a log's
    format - and no column needs a rule of its own here.
    """

    changed = Signal()

    #: The strip's height, and the box's inside it. Sized from the row rather
    #: than from a form field: this stands between the header and the first row
    #: and has to read as part of the table's own furniture.
    HEIGHT = 26
    INSET = 3

    def __init__(self, table, skip=(), parent=None):
        super().__init__(parent)
        self.setProperty("role", "searchrow")
        # A plain QWidget paints no background from a stylesheet without this,
        # so the strip would show the page through it.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(self.HEIGHT)
        self._table = table
        self._edits = {}
        for column in range(table.columnCount()):
            if column in set(skip):
                continue
            edit = QLineEdit(self)
            heading = table.horizontalHeaderItem(column)
            edit.setPlaceholderText((heading.text() if heading else "").lower()
                                    or "search")
            edit.setProperty("role", "search")
            edit.textChanged.connect(self.apply)
            self._edits[column] = edit
        table.set_search_row(self)
        header = table.horizontalHeader()
        header.sectionResized.connect(self._place)
        table.horizontalScrollBar().valueChanged.connect(self._place)

    def sizeHint(self):
        return QSize(self._table.viewport().width(), self.HEIGHT)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place()

    def _place(self, *_args):
        """Every box over the column it searches, at whatever width that is now.

        Flush with the column's own edges rather than inset inside them. Inset,
        the first box sat against the table's border while every other one had a
        gap on each side, so a row of boxes meant to line up with the columns
        read as a row of unevenly spaced ones. Sharing edges also means the boxes
        divide the strip exactly where the header divides its sections.
        """
        header = self._table.horizontalHeader()
        for column, edit in self._edits.items():
            if self._table.isColumnHidden(column):
                edit.hide()
                continue
            edit.show()
            # One pixel wider than the column, so this box's right border lands
            # under the next box's left one and the two read as a single rule
            # rather than as a double.
            edit.setGeometry(header.sectionViewportPosition(column), self.INSET,
                             max(header.sectionSize(column) + 1, 0),
                             self.HEIGHT - 2 * self.INSET)

    def box(self, column):
        """The search box over one column, or None where there is not one."""
        return self._edits.get(column)

    def needles(self):
        """{column: what to look for}, lower-cased, empty boxes left out."""
        return {column: edit.text().strip().lower()
                for column, edit in self._edits.items() if edit.text().strip()}

    def searching(self):
        return bool(self.needles())

    def place(self):
        """Put every box over its column again - for a table that has just set
        its own widths, which a stretching column takes without saying so."""
        self._place()

    def matches(self, row):
        """Whether ``row`` passes what is typed - for a table that hides rows on
        more than this alone and so decides the hiding itself."""
        return self._matches(row, self.needles())

    def clear(self):
        for edit in self._edits.values():
            edit.clear()

    def state(self):
        """What is typed here, keyed by column, for putting back after a rebuild."""
        return {str(column): edit.text() for column, edit in self._edits.items()
                if edit.text()}

    def set_state(self, state):
        for column, edit in self._edits.items():
            edit.setText((state or {}).get(str(column), ""))

    def apply(self, *_args):
        """Hide what does not match. Called again whenever the rows are rebuilt.

        A row's open details have no text of their own to match: they are
        shown and hidden with the row they belong to, which is always the one
        above them.
        """
        needles = self.needles()
        table = self._table
        detail = getattr(table, "is_detail_row", None)
        hidden = False
        for row in range(table.rowCount()):
            if detail is not None and detail(row):
                table.setRowHidden(row, hidden)
                continue
            hidden = not self._matches(row, needles)
            table.setRowHidden(row, hidden)
        self._place()
        self.changed.emit()

    def _matches(self, row, needles):
        for column, needle in needles.items():
            item = self._table.item(row, column)
            if needle not in (item.text().lower() if item is not None else ""):
                return False
        return True
