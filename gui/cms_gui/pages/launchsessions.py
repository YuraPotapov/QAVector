"""How many sessions a run works at once, in a small panel under its button.

The number is set once and rarely touched, so it does not take a block of the
page: the page shows what is in force ("One by one", "3 at a time") on one
button, and this panel - opened from it - holds the choice.

It used to be a number with "All at once" and "Auto" ticked beside it, and
what that would do had to be guessed - in particular that with the windows kept
open every window opens at the start whatever the number says, so "one at a
time" looked like all of them at once. So the choice is now one of four, and
under it the selected sessions are drawn as lanes: when each one works, and
when its window is merely open. The windows themselves are set in Advanced;
the panel says which way they are set and links there.

The panel owns the widgets and nothing else; the page reads and writes them.
``jobs`` is still the number in force - "One by one" is simply 1 - so a saved
configuration means what it meant before.
"""

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QRadioButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from .. import theme, widgets

ONE, SOME, ALL, AUTO = "one", "some", "all", "auto"


def label_for(jobs, all_at_once, auto):
    """What the page's button says is in force."""
    if auto:
        return "Auto"
    if all_at_once:
        return "All together"
    return "One by one" if jobs <= 1 else "%d at a time" % jobs


def choice_of(jobs, all_at_once, auto):
    """Which of the panel's four answers a saved setting is."""
    if auto:
        return AUTO
    if all_at_once:
        return ALL
    return ONE if int(jobs or 1) <= 1 else SOME


class SessionLanes(QWidget):
    """The selected sessions as lanes: when each works, and when it only waits.

    Time runs left to right in turns, one turn being one session's scenarios -
    a picture of the order, not of durations nobody knows yet. With the windows
    kept open, a lane is the window's whole life: open from the start, working
    in its turn, open again until the run is over. With them closed after, a
    lane is only its turn, because that is all the window exists for.

    Where the order decides anything - one by one, or a few at a time - a lane
    can be dragged up or down, and that is the order the sessions run in.
    """

    LANE = 12
    GAP = 6
    GRIP = 14
    NAME = 110
    MAX_LANES = 6

    #: A lane was dropped somewhere else: every login, in the new order.
    reordered = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries = []                # (login, name)
        self._choice = ONE
        self._jobs = 1
        self._kept = True
        self._drag = None                 # the lane in hand
        self._lift = 0.0                  # where its top is now, following the mouse
        self._grab = 0.0                  # where on it the mouse took hold
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_plan(self, entries, choice, jobs, kept):
        plan = ([tuple(entry) for entry in entries], choice, max(1, int(jobs or 1)), bool(kept))
        if plan == (self._entries, self._choice, self._jobs, self._kept):
            return
        self._entries, self._choice, self._jobs, self._kept = plan
        self._drag = None
        self.setToolTip("Drag a session up or down to change the order they run in."
                        if self.draggable() else
                        "The order does not matter here: they all run together."
                        if len(self._entries) > 1 else "")
        self.updateGeometry()
        self.update()

    def logins(self):
        return [login for login, _name in self._entries]

    def draggable(self):
        return self._choice in (ONE, SOME) and len(self._entries) > 1

    def turns(self):
        """``([(start, length)] per lane, total)`` in turns."""
        count = len(self._entries)
        if not count:
            return [], 1
        if self._choice in (ALL, AUTO):
            return [(0, 1)] * count, 1
        width = 1 if self._choice == ONE else self._jobs
        spans = [(index // width, 1) for index in range(count)]
        return spans, max(start + 1 for start, _length in spans)

    def _shown(self):
        """Lanes drawn, and how many more there are that are not."""
        if len(self._entries) <= self.MAX_LANES:
            return len(self._entries), 0
        return self.MAX_LANES - 1, len(self._entries) - (self.MAX_LANES - 1)

    def sizeHint(self):                       # noqa: N802 - Qt's own spelling
        drawn, more = self._shown()
        rows = drawn + (1 if more else 0)
        legend = self.fontMetrics().height() + 4
        return QSize(360, rows * (self.LANE + self.GAP) + legend)

    def minimumSizeHint(self):                # noqa: N802 - Qt's own spelling
        return self.sizeHint()

    # -- dragging -------------------------------------------------------------
    # The whole lane - name and bar - is picked up and follows the mouse; the
    # others step aside to make room, and every bar is drawn for the place its
    # lane would have if dropped now, so the order and its timing move together.
    def _pitch(self):
        return self.LANE + self.GAP

    def _row_at(self, y):
        row = int(y // self._pitch())
        return row if 0 <= row < self._shown()[0] else None

    def _target(self):
        """The place the lane in hand would land in, 0..drawn-1."""
        return max(0, min(self._shown()[0] - 1, int(round(self._lift / float(self._pitch())))))

    def _preview(self):
        """Lane indices in the order they would run if dropped now."""
        order = list(range(len(self._entries)))
        if self._drag is not None:
            order.insert(self._target(), order.pop(self._drag))
        return order

    def mousePressEvent(self, event):         # noqa: N802 - Qt's own spelling
        y = event.position().y()
        row = self._row_at(y)
        if event.button() == Qt.LeftButton and self.draggable() and row is not None:
            self._drag = row
            self._lift = float(row * self._pitch())
            self._grab = y - self._lift
            self.setCursor(Qt.ClosedHandCursor)
            self.update()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):          # noqa: N802 - Qt's own spelling
        y = event.position().y()
        if self._drag is not None:
            last = (self._shown()[0] - 1) * self._pitch()
            self._lift = max(0.0, min(float(last), y - self._grab))
            self.update()
            return
        over = self.draggable() and self._row_at(y) is not None
        self.setCursor(Qt.OpenHandCursor if over else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):       # noqa: N802 - Qt's own spelling
        if self._drag is None:
            super().mouseReleaseEvent(event)
            return
        row, target = self._drag, self._target()
        self._drag = None
        self.setCursor(Qt.OpenHandCursor)
        self.update()
        moved = self.moved(row, target + 1 if target > row else target)
        if moved is not None:
            self.reordered.emit(moved)

    def moved(self, row, gap):
        """Every login with lane ``row`` put into ``gap``; None if nothing moves."""
        if gap in (row, row + 1):
            return None
        logins = self.logins()
        login = logins.pop(row)
        logins.insert(gap - 1 if gap > row else gap, login)
        return logins

    # -- painting ---------------------------------------------------------------
    def paintEvent(self, _event):             # noqa: N802 - Qt's own spelling
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        metrics = self.fontMetrics()
        spans, total = self.turns()
        drawn, more = self._shown()
        pitch = self._pitch()
        order = self._preview()
        lifted = None
        for place in range(drawn):
            index = order[place]
            if index == self._drag:
                lifted = place               # drawn last, on top of the rest
                continue
            self._paint_lane(painter, metrics, place * pitch, index, spans[place], total)
        if lifted is not None:
            self._paint_lane(painter, metrics, self._lift, self._drag, spans[lifted], total,
                             in_hand=True)
        y = drawn * pitch
        muted = QColor(theme.NEUTRAL[600])
        if more:
            painter.setPen(muted)
            painter.drawText(QRectF(0, y - 2, self.width(), self.LANE + 4),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             "and %d more" % more)
            y += pitch
        # The legend: what the two shades mean, only the ones that are drawn.
        running, waiting = QColor(theme.ACCENT), QColor(theme.NEUTRAL[300])
        x = float(self.NAME)
        # Waiting is only said where a lane shows it: all at once, nobody waits.
        keys = [(running, "running")] + ([(waiting, "window open, waiting")]
                                         if self._kept and total > 1 else [])
        for colour, words in keys:
            painter.setPen(Qt.NoPen)
            painter.setBrush(colour)
            middle = y + metrics.height() / 2.0
            painter.drawRoundedRect(QRectF(x, middle - 4, 14, 8), 2, 2)
            painter.setPen(muted)
            painter.drawText(QRectF(x + 20, y, 200, metrics.height() + 2),
                             Qt.AlignLeft | Qt.AlignVCenter, words)
            x += 20 + metrics.horizontalAdvance(words) + 18
        painter.end()

    def _paint_lane(self, painter, metrics, y, index, span, total, in_hand=False):
        """One lane - grip, name, the window's life and its turn - with its top at ``y``."""
        track = self.width() - self.NAME
        unit = track / float(total)
        running = QColor(theme.ACCENT)
        if in_hand:
            # Lifted off the others: a ground of its own and an edge.
            painter.setPen(QColor(theme.NEUTRAL[400]))
            painter.setBrush(QColor(theme.NEUTRAL[200]))
            painter.drawRoundedRect(QRectF(0, y - self.GAP / 2.0, self.width() - 1, self._pitch()), 3, 3)
        left = 0
        if self.draggable():
            # Six dots: the usual mark for "this can be picked up".
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.NEUTRAL[500]))
            for column in (2, 6):
                for dot in (2, 6, 10):
                    painter.drawEllipse(QRectF(column, y + dot - 1, 2, 2))
            left = self.GRIP
        name = metrics.elidedText(self._entries[index][1], Qt.ElideRight, self.NAME - 8 - left)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(QRectF(left, y - 2, self.NAME - 8 - left, self.LANE + 4),
                         Qt.AlignLeft | Qt.AlignVCenter, name)
        painter.setPen(Qt.NoPen)
        if self._kept:
            painter.setBrush(QColor(theme.NEUTRAL[300]))
            painter.drawRoundedRect(QRectF(self.NAME, y, track, self.LANE), 3, 3)
        start, length = span
        bar = QRectF(self.NAME + start * unit, y, max(4.0, length * unit - 2), self.LANE)
        # Auto's lanes are hatched: they may run together or take turns,
        # as the machine allows - the picture must not promise either.
        if self._choice == AUTO:
            painter.setPen(running)
            painter.setBrush(QBrush(running, Qt.BDiagPattern))
        else:
            painter.setBrush(running)
        painter.drawRoundedRect(bar, 3, 3)
        painter.setPen(Qt.NoPen)


class SessionsPanel(widgets.FloatingPanel):
    """See the module docstring."""

    WIDTH = 460

    #: "change in Advanced" was clicked: the page shows where the windows are set.
    advanced_requested = Signal()
    #: A lane was dragged: every login, in the order they are to run.
    reordered = Signal(list)
    #: "Reset order": back to the accounts' own order.
    reset_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "clarifypopup")
        self.setFixedWidth(self.WIDTH)
        self._last_some = 2
        self._syncing = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QFrame()
        head.setProperty("role", "clarifyhead")
        band = QHBoxLayout(head)
        band.setContentsMargins(12, 7, 12, 7)
        band.setSpacing(8)
        band.addWidget(widgets.kicker("Sessions at once"), 1)
        keys = QLabel("Esc closes")
        keys.setProperty("role", "keys")
        band.addWidget(keys)
        outer.addWidget(head)

        body = QWidget(self)
        column = QVBoxLayout(body)
        column.setContentsMargins(12, 10, 12, 12)
        column.setSpacing(8)

        self.jobs = widgets.Stepper(1, 64)
        self.jobs_one = QRadioButton("One by one")
        self.jobs_some = QRadioButton("")
        self.jobs_some.setAccessibleName("Several at a time")
        self.jobs_all = QRadioButton("All together")
        self.jobs_auto = QRadioButton("Auto - the machine decides, fewer when memory is short")
        self.jobs_auto.setToolTip(
            "Start at one window per processor core and let the machine decide: "
            "fewer while memory is under real pressure, back up again once it is "
            "not. A number you type here is never changed for you.")
        self.choice = QButtonGroup(self)
        for button in (self.jobs_one, self.jobs_some, self.jobs_all, self.jobs_auto):
            self.choice.addButton(button)
        self.at_a_time = QLabel("at a time")
        column.addWidget(self.jobs_one)
        column.addWidget(widgets.row(self.jobs_some, self.jobs, self.at_a_time, None, spacing=8))
        column.addWidget(self.jobs_all)
        column.addWidget(self.jobs_auto)
        self.jobs_one.toggled.connect(self._one_chosen)
        self.jobs_some.toggled.connect(self._some_chosen)
        self.jobs.valueChanged.connect(self._number_changed)

        self.lanes = SessionLanes()
        self.lanes.reordered.connect(self.reordered)
        column.addSpacing(4)
        column.addWidget(self.lanes)
        # Only once an order of its own is set: then it is worth a way back.
        self.reset = QLabel('<a href="reset" style="color: %s;">Reset order</a>' % theme.ACCENT)
        self.reset.setProperty("role", "hint")
        self.reset.setTextFormat(Qt.RichText)
        self.reset.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.reset.linkActivated.connect(lambda _href: self.reset_requested.emit())
        self.reset.setVisible(False)
        column.addWidget(self.reset, 0, Qt.AlignRight)
        self.idle = QLabel("Only windows are opened; this applies when scenarios run.")
        self.idle.setProperty("role", "hint")
        self.idle.setWordWrap(True)
        self.idle.setVisible(False)
        column.addWidget(self.idle)
        self.windows = QLabel("")
        self.windows.setProperty("role", "hint")
        self.windows.setWordWrap(True)
        self.windows.setTextFormat(Qt.RichText)
        self.windows.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.windows.linkActivated.connect(lambda _href: self.advanced_requested.emit())
        column.addWidget(self.windows)
        self.metrics = QLabel("System load: CPU --% · RAM --%")
        self.metrics.setProperty("role", "hint")
        self.metrics.setWordWrap(True)
        column.addWidget(self.metrics)
        outer.addWidget(body)
        self.jobs_one.setChecked(True)

    # -- the choice -------------------------------------------------------------
    # One number underneath, as saved: "One by one" is 1, "N at a time" is the
    # number, and moving the number moves the answer with it. "All together"
    # and "Auto" leave the number as it was, greyed by the page.
    def _one_chosen(self, on):
        if on and not self._syncing:
            self.jobs.setValue(1)

    def _some_chosen(self, on):
        if on and not self._syncing and self.jobs.value() < 2:
            self.jobs.setValue(self._last_some)

    def _number_changed(self, value):
        if value >= 2:
            self._last_some = value
        if self.jobs_all.isChecked() or self.jobs_auto.isChecked():
            return
        self._syncing = True
        try:
            (self.jobs_one if value <= 1 else self.jobs_some).setChecked(True)
        finally:
            self._syncing = False

    def set_choice(self, jobs, all_at_once, auto):
        """Show a saved setting: the number, and the answer it is."""
        jobs = max(1, int(jobs or 1))
        self._syncing = True
        try:
            self.jobs.setValue(jobs)
            if jobs >= 2:
                self._last_some = jobs
            {ONE: self.jobs_one, SOME: self.jobs_some, ALL: self.jobs_all,
             AUTO: self.jobs_auto}[choice_of(jobs, all_at_once, auto)].setChecked(True)
        finally:
            self._syncing = False

    def current(self):
        return choice_of(self.jobs.value(), self.jobs_all.isChecked(), self.jobs_auto.isChecked())

    # -- what it will do ----------------------------------------------------------
    def set_plan(self, entries, kept, running, custom=False):
        """The sessions selected, as ``(login, name)`` in the order they run;
        whether their windows stay open; whether scenarios run at all - nothing
        of this applies to opening windows; and whether that order is one of
        their own rather than the accounts'."""
        for button in (self.jobs_one, self.jobs_some, self.jobs_all, self.jobs_auto):
            button.setEnabled(running)
        # Greyed with the number it reads with (the page greys the number).
        self.at_a_time.setEnabled(running and self.jobs.isEnabled())
        self.lanes.set_plan(entries, self.current(), self.jobs.value(), kept)
        self.lanes.setVisible(running and bool(entries))
        self.reset.setText('<a href="reset" style="color: %s;">Reset order</a>' % theme.ACCENT)
        self.reset.setVisible(running and custom and self.lanes.draggable())
        self.idle.setVisible(not running)
        self.windows.setVisible(running)
        said = ("kept open after the run" if kept else
                "each opens when its turn comes and closes when it is done")
        self.windows.setText('Windows: %s - <a href="advanced" style="color: %s;">'
                             'change in Advanced</a>' % (said, theme.ACCENT))
        self._fit()

    def _fit(self):
        """As tall as what is shown now. Fixed, not resized: a panel changed
        while hidden kept the minimum height a hidden row once needed, and the
        header took up the difference."""
        # A hidden widget does not invalidate its layout when what it holds
        # changes size, so both are told to forget what they measured.
        body = self.lanes.parentWidget()
        self.lanes.updateGeometry()
        body.layout().invalidate()
        body.updateGeometry()
        layout = self.layout()
        layout.invalidate()
        layout.activate()
        height = max(layout.heightForWidth(self.WIDTH) if layout.hasHeightForWidth() else 0,
                     layout.sizeHint().height())
        self.setFixedHeight(height)

    def open_below(self, anchor):
        """Show under ``anchor`` (the page's button), on the answer in force."""
        self._fit()
        self.move(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))
        self.show()
        self.raise_()
        self.activateWindow()
        target = self.choice.checkedButton() or self.jobs_one
        if target.isEnabled():
            target.setFocus(Qt.PopupFocusReason)

    def keyPressEvent(self, event):          # noqa: N802 - Qt's own spelling
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
