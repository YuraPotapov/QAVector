"""Shared widgets that carry the design system's distinctive parts.

Qt stylesheets cover colour, type and borders; the pieces here are the ones a
stylesheet cannot express - the blueprint frame's registration marks, the
segmented control, the tag pill - plus small constructors so pages read as
layout rather than as widget configuration.
"""

import itertools
import os
import weakref

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, Signal
from PySide6.QtWidgets import (QAbstractScrollArea, QAbstractSpinBox,
                               QApplication, QComboBox, QDialog,
                               QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QSizePolicy,
                               QSpinBox, QSplitter, QToolButton,
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


class Tag(QLabel):
    """Small status pill: accent (good), outline (in flight), neutral, bad."""

    STYLES = {
        "accent": (theme.ACCENT_RAMP[100], theme.ACCENT_RAMP[800], "transparent"),
        "outline": ("transparent", theme.ACCENT, theme.ACCENT),
        "neutral": (theme.NEUTRAL[100], theme.NEUTRAL[800], theme.NEUTRAL[300]),
        "warn": ("#f7efe2", theme.WARN, "transparent"),
        "bad": (theme.BAD_TINT, theme.BAD, "transparent"),
    }

    def __init__(self, text="", variant="neutral", parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.set_variant(variant)

    def set_variant(self, variant):
        background, fg, border = self.STYLES.get(variant, self.STYLES["neutral"])
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
        self._elide()

    def text(self):
        return self._full

    def sizeHint(self):
        # The whole text, not what is showing: a label that had to shorten once
        # would otherwise ask for only that much and never get its text back.
        # Only read under a policy other than Ignored - see elided_line.
        hint = super().sizeHint()
        margins = self.contentsMargins()
        hint.setWidth(self.fontMetrics().horizontalAdvance(self._full) + 2
                      + margins.left() + margins.right())
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
        self._update_count()

    # -- contents -------------------------------------------------------------
    def set_noun(self, noun):
        """What the count line calls the things in the list."""
        self._noun = noun
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
                self._empty.say_filtered("Nothing selected yet - tick %s under All."
                                         % self._noun)
            elif self._needle:
                self._empty.say_filtered("Nothing here matches the search.")
            else:
                self._empty.say_filtered("")


#: The two views a :class:`CheckList` with ``selected_view`` switches between.
VIEW_ALL = "All"
VIEW_SELECTED = "Selected"


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

    def __init__(self, parent, title, message, detail="", kind="",
                 agree="OK", refuse="", output="", choices=()):
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


def output(parent, title, message, text="", kind=""):
    """What a command printed, in a box somebody can select a line out of."""
    return _shown(Message(parent, title, message, "", kind, output=text))


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

