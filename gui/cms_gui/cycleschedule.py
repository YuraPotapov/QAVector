"""Starting something on the plan later: when, and the timer that does it.

An entry on the plan (``cycle/planner.py``) may carry ``run_at``: the moment
somebody asked for it to start, at most :data:`HORIZON` ahead. Two things here:

* :class:`WhenDialog` - what a double-click on a plan entry opens: start it
  now, or pick a day and a time. Built on :class:`widgets.Message`, so it wears
  the application's own frame and buttons like every other question it asks.
  The day is picked from a short list - today and the three after it, which is
  all the horizon allows - rather than from a calendar: a calendar would offer
  a month of days that cannot be chosen.
* :class:`PlanScheduler` - holds the plan's start times and says when one is
  due. On time - the application was open - it is simply started. Missed - the
  application was closed then, or the machine asleep - it is asked about first.
  Stale - further back than the same three days - it is never started nor
  asked about: the application may be opened a month later, and what was
  planned a month ago is not today's work.

Nothing here starts a run; the window does, the way it starts any planned entry.
"""

import time

from PySide6.QtCore import QObject, Qt, QTime, QTimer, Signal
from PySide6.QtWidgets import (QAbstractSpinBox, QComboBox, QDialog, QHBoxLayout,
                               QLabel, QTimeEdit)

from . import theme, widgets

#: The same as ``cycle.planner.HORIZON``: how far ahead a start may be put,
#: and how long after its time a missed one is still worth asking about.
HORIZON = 3 * 24 * 3600
#: How late a timer may fire and still count as on time. Later than this the
#: machine was asleep, and a start nobody is watching for is asked about.
LATE = 10 * 60
#: The longest the timer is set for at once. A long wait is re-measured, so a
#: clock moved or a machine woken from sleep is noticed within the hour.
LONGEST_WAIT = 3600
#: What a person picks when nothing says otherwise: the end of the working day.
DEFAULT_HOUR = 18

START_NOW = "Start now"
SCHEDULE = "Schedule"
CLEAR = "Clear schedule"


def stale(item, now):
    """True when ``item`` was scheduled further back than :data:`HORIZON`."""
    run_at = (item or {}).get("run_at")
    if not isinstance(run_at, (int, float)) or isinstance(run_at, bool):
        return False
    return now - run_at > HORIZON


def when_text(seconds):
    return time.strftime("%d %b %H:%M", time.localtime(seconds))


def _midnight(seconds):
    day = time.localtime(seconds)
    return time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1))


def default_start(now):
    """Today at :data:`DEFAULT_HOUR`, or an hour from now when that has passed -
    on a five-minute mark, since nobody schedules anything for 14:07."""
    evening = _midnight(now) + DEFAULT_HOUR * 3600
    if evening > now + 60:
        return evening
    later = now + 3600
    return later - later % 300 + 300


def days(now):
    """``[(label, midnight)]`` for today and the days the horizon reaches."""
    found = []
    for offset in range(HORIZON // 86400 + 1):
        midnight = _midnight(_midnight(now) + offset * 86400 + 3 * 3600)
        name = ("Today" if offset == 0 else "Tomorrow" if offset == 1
                else time.strftime("%A", time.localtime(midnight)))
        found.append(("%s, %s" % (name, time.strftime("%d %b", time.localtime(midnight))),
                      midnight))
    return found


class WhenDialog(widgets.Message):
    """Start a planned entry now, or at a day and time within the horizon."""

    def __init__(self, parent, item, now=None, title=""):
        self.now = now if now is not None else time.time()
        run_at = item.get("run_at")
        if isinstance(run_at, (int, float)) and not stale(item, self.now):
            detail = ("It is scheduled for %s. Pick another time, start it now, "
                      "or take the time off." % when_text(run_at))
        else:
            detail = ("Start it now, or pick when - at most %d days ahead. The "
                      "application starts it then if it is open; if it was "
                      "closed, it asks when it opens."
                      % (HORIZON // 86400))
        super().__init__(parent, "Start later", "Start %s?" % (title or "this"),
                         detail, "", SCHEDULE, "Cancel",
                         choices=((CLEAR,) if run_at else ()) + (START_NOW,))

        self.day = QComboBox()
        for label, midnight in days(self.now):
            self.day.addItem(label, midnight)
        self.at = QTimeEdit()
        self.at.setDisplayFormat("HH:mm")
        # No arrows: a stylesheet cannot draw them inside the frame (see the
        # stepper in theme.py), and the keyboard and the wheel both work.
        self.at.setButtonSymbols(QAbstractSpinBox.NoButtons)
        start = run_at if isinstance(run_at, (int, float)) and run_at > self.now \
            else default_start(self.now)
        moment = time.localtime(start)
        for index in range(self.day.count()):
            if self.day.itemData(index) == _midnight(start):
                self.day.setCurrentIndex(index)
        self.at.setTime(QTime(moment.tm_hour, moment.tm_min))
        self.problem = QLabel("")
        self.problem.setStyleSheet("color: %s;" % theme.BAD)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.day, 1)
        row.addWidget(self.at)
        layout = self.layout()
        layout.insertLayout(layout.count() - 1, row)
        layout.insertWidget(layout.count() - 1, self.problem)
        self.day.currentIndexChanged.connect(self._check)
        self.at.timeChanged.connect(self._check)
        self._check()

    def run_at(self):
        """The moment picked, in seconds since the epoch."""
        picked = self.at.time()
        midnight = self.day.currentData()
        day = time.localtime(midnight)
        return time.mktime((day.tm_year, day.tm_mon, day.tm_mday,
                            picked.hour(), picked.minute(), 0, 0, 0, -1))

    def _check(self, *_args):
        when = self.run_at()
        if when <= self.now:
            said = "That time has already passed."
        elif when > self.now + HORIZON:
            said = "At most %d days ahead - until %s." % (
                HORIZON // 86400, when_text(self.now + HORIZON))
        else:
            said = ""
        self.problem.setText(said)
        self.problem.setVisible(bool(said))
        self.agree_button.setEnabled(not said)


def ask_when(parent, item, title="", now=None):
    """``(answer, run_at)``: START_NOW, SCHEDULE with the time picked, CLEAR,
    or "" when the window was closed."""
    dialog = WhenDialog(parent, item, now=now, title=title)
    try:
        if dialog.exec() != QDialog.Accepted:
            return "", None
        answer = dialog.chosen
        return answer, (dialog.run_at() if answer == SCHEDULE else None)
    finally:
        dialog.deleteLater()


def ask_missed(parent, item, title=""):
    """The application was closed when ``item`` was due: start it now?"""
    return widgets.confirm(
        parent, "Scheduled start missed",
        "Start %s now?" % (title or "it"),
        "It was scheduled for %s, and the application was not open then. Not "
        "now keeps it on the plan without a time." % when_text(item["run_at"]),
        agree="Start now", refuse="Not now")


class PlanScheduler(QObject):
    """Says when a planned entry's start time has come.

    ``due(item, missed)``: ``missed`` is true when its time passed while the
    application was closed, or the timer fired more than :data:`LATE` after it
    - a machine asleep. Each time is said once; a new time for the same entry
    is a new one.
    """

    due = Signal(dict, bool)

    def __init__(self, parent=None, clock=time.time):
        super().__init__(parent)
        self.clock = clock
        self.started = clock()
        self.items = []
        self.said = set()           # (entry id, run_at) already announced
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        # A coarse timer may be off by 5% - three minutes in an hour - and a
        # start somebody set for 18:00 should not wait for 18:03.
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)

    def set_planned(self, items):
        self.items = [one for one in items or [] if isinstance(one, dict)
                      and isinstance(one.get("run_at"), (int, float))]
        self.tick()

    def waiting(self, now=None):
        """The entries with a start time still to come or to be asked about."""
        now = self.clock() if now is None else now
        return [one for one in self.items
                if not stale(one, now)
                and (one.get("id"), one["run_at"]) not in self.said]

    def tick(self):
        now = self.clock()
        for item in sorted(self.waiting(now), key=lambda one: one["run_at"]):
            if item["run_at"] > now:
                continue
            self.said.add((item.get("id"), item["run_at"]))
            missed = item["run_at"] < self.started or now - item["run_at"] > LATE
            self.due.emit(item, missed)
        ahead = [one["run_at"] - now for one in self.waiting(now)
                 if one["run_at"] > now]
        if ahead:
            self.timer.start(int(min(min(ahead), LONGEST_WAIT) * 1000) + 50)
        else:
            self.timer.stop()
