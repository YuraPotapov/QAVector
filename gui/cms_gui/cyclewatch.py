"""Listening for new tasks while the application is open.

A cycle may declare ``triggers:`` - a step whose plugin can be listened to,
and ``every:`` how many seconds apart (see ``docs/cycles.md``). What the step
reads - a Jira queue, today - is the plugin's business, not this module's.
:class:`Listener` repeats each one on that schedule, through the core
(``--cycle-watch``), and keeps what comes back as a queue of offers: "a new
task has appeared - start work on it?". The window asks about them one at a
time, and only when nothing else is running. Nothing is ever started without
that answer.

How often is the cycle file's decision alone: there is no polling interval
here. The timer is set for the moment the earliest trigger falls due, and set
again after every check.

Checks run on a :class:`QThread`, because a source that is slow to answer
would otherwise freeze the window for as long as it takes. Only one check is in
flight at a time; triggers that fall due meanwhile are asked right after it.

A check that fails - the source down, a secret not set - is kept as the trigger's
last problem and tried again on schedule. It is not an offer and is not shown
as a dialog: an unattended listener must not interrupt somebody every few
minutes because the network is off.
"""

import time

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QDialog

from . import core as core_mod

#: How long the "new task" window waits for an answer before putting the task
#: on the plan by itself - so a question nobody is there to answer does not
#: hold up every task behind it.
PLAN_AFTER_SECONDS = 20
#: The plan button, counting down: "In plan (20)".
PLAN_LABEL = "In plan"

#: When the window closes with a check still in flight, how long to wait for it
#: before letting go. Not a schedule: a QThread destroyed while it runs takes
#: the process with it, and a check cannot be cancelled halfway.
CLOSE_WAIT_MS = 5000


def _clock(seconds):
    seconds = max(0, int(round(seconds)))
    return "%d:%02d" % (seconds // 60, seconds % 60)


def _short(text, limit=60):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


class WatchThread(QThread):
    """Ask the core about each due trigger, one after another."""

    checked = Signal(str, str, object)      # cycle, trigger, payload

    def __init__(self, core, targets, parent=None):
        super().__init__(parent)
        self.core, self.targets = core, list(targets)

    def run(self):
        for cycle_id, trigger_id in self.targets:
            try:
                payload = self.core.cycle_watch(cycle_id, trigger_id)
            except core_mod.CoreError as exc:
                payload = {"ok": False, "problems": [str(exc)]}
            self.checked.emit(cycle_id, trigger_id, payload)


class Listener(QObject):
    """The schedule, the queue of offers, and what each trigger last said."""

    #: Something is waiting to be offered; the window decides when to ask.
    offers_changed = Signal()

    def __init__(self, core, parent=None, clock=time.monotonic):
        super().__init__(parent)
        self.core = core
        self.clock = clock
        self.enabled = True
        self.triggers = {}          # (cycle, trigger) -> {"every", "pin", "name"}
        self.last = {}              # (cycle, trigger) -> when it was last asked
        self.problems = {}          # (cycle, trigger) -> the last check's problem
        self.queue = []             # offers, oldest first
        #: The offer somebody is looking at right now. Not in the queue any
        #: more, and still not to be queued again by a check that lands while
        #: its window is open - that is how one task came to be asked twice.
        self.asking = None
        self.thread = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.tick)

    # -- what to listen for ---------------------------------------------------
    def set_inventory(self, inventory):
        """Listen for every trigger in every cycle that has no problems.

        A cycle that no longer declares a trigger is dropped, and so are its
        waiting offers: they would start a cycle that no longer asks for it.
        """
        found = {}
        for row in inventory.cycles:
            if row.get("problems"):
                continue
            for one in row.get("triggers") or []:
                if one.get("enabled") is False:
                    continue                # turned off in the step's settings
                found[(row["id"], one["id"])] = {
                    "every": int(one["every"]),
                    "pin": one.get("pin") or "",
                    "name": row.get("name") or row["id"]}
        self.triggers = found
        self.queue = [one for one in self.queue
                      if (one["cycle"], one["trigger"]) in found]
        self.tick()

    def set_core(self, core):
        self.core = core

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        if self.enabled:
            self.tick()
        else:
            self.timer.stop()

    # -- the schedule ---------------------------------------------------------
    def due(self):
        now = self.clock()
        return [key for key, trigger in sorted(self.triggers.items())
                if key not in self.last or now - self.last[key] >= trigger["every"]]

    def seconds_to_next(self):
        """How long until the earliest trigger falls due, or None for none."""
        if not self.triggers:
            return None
        now = self.clock()
        return max(0.0, min((self.last[key] + trigger["every"] - now)
                            if key in self.last else 0.0
                            for key, trigger in self.triggers.items()))

    def _schedule(self):
        wait = self.seconds_to_next()
        if wait is None or not self.enabled:
            self.timer.stop()
        else:
            self.timer.start(int(wait * 1000))

    def tick(self):
        if not self.enabled or self.core is None:
            self.timer.stop()
            return
        if self.thread is not None and self.thread.isRunning():
            return                          # _finished schedules the next
        targets = self.due()
        if not targets:
            self._schedule()
            return
        now = self.clock()
        for key in targets:
            self.last[key] = now
        self.thread = WatchThread(self.core, targets, self)
        self.thread.checked.connect(self._checked)
        self.thread.finished.connect(self._schedule)
        self.thread.start()

    def _checked(self, cycle_id, trigger_id, payload):
        key = (cycle_id, trigger_id)
        trigger = self.triggers.get(key)
        if trigger is None:
            return
        if not payload.get("ok"):
            self.problems[key] = "; ".join(payload.get("problems") or ["no answer"])
            return
        self.problems.pop(key, None)
        waiting = {(one["cycle"], one["key"]) for one in self.queue}
        if self.asking is not None:
            waiting.add((self.asking["cycle"], self.asking["key"]))
        added = False
        for issue in payload.get("new") or []:
            if (cycle_id, issue.get("key")) in waiting:
                continue
            self.queue.append({"cycle": cycle_id, "trigger": trigger_id,
                               "name": trigger["name"],
                               "pin": payload.get("pin") or trigger["pin"],
                               "key": issue.get("key", ""),
                               "title": issue.get("title", ""),
                               "url": issue.get("url", "")})
            added = True
        if added:
            self.offers_changed.emit()

    # -- what to show ----------------------------------------------------------
    def status(self):
        """``(line, detail, trouble)`` for the status bar, or ``("", "", False)``
        when nothing is listened for.

        The line is short: how many cycles, when the next check is - or, when
        the last check of one failed, that. The detail is one line per trigger,
        for a tooltip.
        """
        if not self.triggers or not self.enabled:
            return "", "", False
        names = sorted({one["name"] for one in self.triggers.values()})
        now = self.clock()
        detail = []
        for key, trigger in sorted(self.triggers.items()):
            left = (self.last[key] + trigger["every"] - now) if key in self.last else 0
            said = self.problems.get(key)
            detail.append("%s: %s" % (trigger["name"], ("last check failed - " + said)
                                      if said else "next check in %s" % _clock(left)))
        if self.problems:
            (cycle, _trigger), said = sorted(self.problems.items())[0]
            name = self.triggers.get((cycle, _trigger), {}).get("name", cycle)
            return ("Listening: %s - %s" % (name, _short(said)), "\n".join(detail),
                    True)
        if self.thread is not None and self.thread.isRunning():
            when = "checking now"
        else:
            wait = self.seconds_to_next()
            when = "next check %s" % _clock(wait or 0)
        count = "%d cycle%s" % (len(names), "" if len(names) == 1 else "s")
        return ("Listening: %s - %s" % (count, when), "\n".join(detail), False)

    # -- answering ------------------------------------------------------------
    def next_offer(self):
        """The oldest waiting offer, taken off the queue, or None. It stays
        :attr:`asking` until :meth:`answered`, so it is not queued again."""
        self.asking = self.queue.pop(0) if self.queue else None
        return self.asking

    def answered(self, offer):
        """Somebody answered about ``offer``: drop any copy still waiting."""
        self.asking = None
        self.queue = [one for one in self.queue
                      if (one["cycle"], one["key"]) != (offer["cycle"], offer["key"])]

    def seen(self, offer):
        """Never offer this task again. Never raises: a failure only means it
        is offered once more."""
        try:
            self.core.cycle_watch_seen(offer["cycle"], offer["key"], offer["trigger"])
        except core_mod.CoreError:
            pass

    def plan(self, offer):
        """Keep it to look at later. Never raises: a failure means it is offered
        again at the next check, which loses nothing."""
        try:
            self.core.cycle_watch_plan(offer["cycle"], offer["key"], offer["trigger"],
                                       offer.get("title", ""), offer.get("url", ""))
            return True
        except core_mod.CoreError:
            return False

    def shutdown(self):
        self.timer.stop()
        if self.thread is not None and self.thread.isRunning():
            self.thread.wait(CLOSE_WAIT_MS)


def ask_about(parent, offer):
    """The "new task" window. Returns "Start work", "Ignore", PLAN_LABEL or ""
    for Not now.

    The plan button counts down and presses itself at zero: somebody who is
    not at the screen has the task kept for later rather than every task
    behind it held up by a question nobody is answering.
    """
    from . import widgets

    dialog = widgets.Message(
        parent, "New task",
        "Start work on %s - %s?" % (offer["key"], offer["title"]),
        "%s found it in the queue it watches. Starting runs the cycle on this "
        "task; the cycle still asks before any code is written. If nobody "
        "answers, it goes on the plan in the Subjects list on the Cycles page."
        % offer["name"], "", "Start work", "Not now",
        choices=("Ignore", "%s (%d)" % (PLAN_LABEL, PLAN_AFTER_SECONDS)))
    plan_button = dialog.choice_buttons[-1]
    left = [PLAN_AFTER_SECONDS]

    def count():
        left[0] -= 1
        if left[0] <= 0:
            countdown.stop()
            plan_button.click()
        else:
            plan_button.setText("%s (%d)" % (PLAN_LABEL, left[0]))

    countdown = QTimer(dialog)
    countdown.setObjectName("plan-countdown")
    countdown.timeout.connect(count)
    countdown.start(1000)
    try:
        accepted = dialog.exec() == QDialog.Accepted
        chosen = dialog.chosen if accepted else ""
    finally:
        countdown.stop()
        dialog.deleteLater()
    return PLAN_LABEL if chosen.startswith(PLAN_LABEL) else chosen
