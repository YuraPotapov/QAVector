"""Cycles that start on a schedule, with nobody asked.

A trigger with ``start: auto`` (see ``docs/cycles.md``) has no step to watch
and nothing to offer: every ``every`` seconds while the application is open,
its cycle is simply run. The validator only lets a cycle that is safe to repeat
unattended have one - no important step, no agent, nothing that asks a person.

**Every project, whichever one is on screen.** A health check of one project
must not stop because somebody is working in another, so :class:`AutoStarts`
keeps the auto triggers of every project, keyed by project. The one on screen
is read from its inventory; the others from their own cycle folders, through a
core aimed at that project (``MainWindow._load_other_schedules``).

**A run of its own.** :class:`ScheduledRuns` starts each due run in a launcher
process of its own, built from the core of the trigger's project - its cycles,
secrets, memory, runs and browser profiles - and never through the window's
run. So a scheduled check neither takes over the page somebody is looking at
nor waits for the project they switched to; a person's run is not held up by
it either. Its record goes to its own project's run history, with its log.

**What waits.** One scheduled run goes at a time; the rest wait in order. A
trigger has at most one waiting entry, so a long run cannot pile up five
hourly checks behind it. And a cycle the window is running right now is not
started a second time beside it - it waits until that run ends.

**What does not happen.** Nothing runs while the application is closed, and
runs missed then - or while the machine slept - are not caught up. The first
run comes ``every`` seconds after a trigger is first seen, not at once: a
restart of the window must not start a run of every scheduled cycle.
"""

import os
import time

from PySide6.QtCore import QObject, QTimer, Signal

from . import core as core_mod, history as history_mod
from .runner import LauncherProcess

#: How long the window's close waits for a scheduled run to stop.
CLOSE_WAIT_MS = 5000
#: The most log lines kept for one scheduled run's record.
LOG_LINES = 5000


def _clock(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds >= 3600:
        return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)
    return "%d:%02d" % (seconds // 60, seconds % 60)


def trigger_value(entry):
    """What ``--cycle-trigger`` says about a run this entry starts."""
    return "schedule:%s" % entry["trigger"]


def auto_triggers(rows):
    """``[(cycle id, trigger id, every, name)]`` of every auto trigger in an
    inventory's cycle rows that is turned on and in a cycle with no problems."""
    found = []
    for row in rows or []:
        if row.get("problems"):
            continue
        for one in row.get("triggers") or []:
            if one.get("start") != "auto" or one.get("enabled") is False:
                continue
            found.append((row["id"], one["id"], int(one.get("every") or 0),
                          row.get("name") or row["id"]))
    return found


class AutoStarts(QObject):
    """The schedule of every auto trigger, in every project."""

    #: ``{"project", "project_name", "cycle", "trigger", "name"}``: run it now.
    run_due = Signal(dict)

    def __init__(self, parent=None, clock=time.monotonic):
        super().__init__(parent)
        self.clock = clock
        self.enabled = True
        #: (project, cycle, trigger) -> {"every", "name", "project_name"}
        self.triggers = {}
        self.last = {}              # (project, cycle, trigger) -> when it last ran
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.tick)

    # -- what to run ------------------------------------------------------------
    def set_project(self, project_id, project_name, rows):
        """The auto triggers of ``project_id`` are those in ``rows`` now.

        One seen for the first time is counted from now, so its first run
        comes a full ``every`` later. One that stays keeps its count; one that
        went - its cycle broken, turned off, deleted - is forgotten.
        """
        now = self.clock()
        found = {key: one for key, one in self.triggers.items() if key[0] != project_id}
        for cycle_id, trigger_id, every, name in auto_triggers(rows):
            key = (project_id, cycle_id, trigger_id)
            found[key] = {"every": max(1, every), "name": name,
                          "project_name": project_name or project_id}
            self.last.setdefault(key, now)
        self.triggers = found
        self.last = {key: when for key, when in self.last.items() if key in found}
        self._schedule()

    def keep_projects(self, project_ids):
        """Forget every project not in ``project_ids`` - taken off the list."""
        for project_id in {key[0] for key in self.triggers} - set(project_ids):
            self.set_project(project_id, "", [])

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self._schedule()

    # -- the schedule ---------------------------------------------------------
    def due(self):
        now = self.clock()
        return [key for key, one in sorted(self.triggers.items())
                if now - self.last.get(key, now) >= one["every"]]

    def seconds_to_next(self, project_id=None):
        """How long until the earliest one falls due - of ``project_id`` only,
        when given - or None when there is none."""
        now = self.clock()
        left = [self.last.get(key, now) + one["every"] - now
                for key, one in self.triggers.items()
                if project_id is None or key[0] == project_id]
        return max(0.0, min(left)) if left else None

    def _schedule(self):
        wait = self.seconds_to_next()
        if wait is None or not self.enabled:
            self.timer.stop()
        else:
            self.timer.start(int(wait * 1000) + 1)

    def tick(self):
        if not self.enabled:
            self.timer.stop()
            return
        now = self.clock()
        for key in self.due():
            self.last[key] = now
            one = self.triggers[key]
            self.run_due.emit({"project": key[0], "project_name": one["project_name"],
                               "cycle": key[1], "trigger": key[2], "name": one["name"]})
        self._schedule()

    # -- what to show ----------------------------------------------------------
    def cycles_of(self, project_id):
        """The ids of ``project_id``'s cycles that start on a schedule."""
        return {key[1] for key in self.triggers if key[0] == project_id}

    def status(self, current_project=""):
        """``(line, detail)`` for the status bar; ``("", "")`` with nothing
        scheduled. Another project's cycle is named with its project."""
        if not self.triggers or not self.enabled:
            return "", ""
        now = self.clock()
        detail = []
        for key, one in sorted(self.triggers.items()):
            name = one["name"] if key[0] == current_project else "%s (%s)" % (
                one["name"], one["project_name"])
            detail.append("%s: next run in %s" % (
                name, _clock(self.last.get(key, now) + one["every"] - now)))
        count = len({key[:2] for key in self.triggers})
        return ("Scheduled: %d cycle%s - next run %s" % (
            count, "" if count == 1 else "s", _clock(self.seconds_to_next() or 0)),
            "\n".join(detail))


class ScheduledRuns(QObject):
    """Due auto triggers, run one at a time, each in a process of its own.

    ``window`` answers three questions, so this needs no knowledge of
    projects or settings:

    * ``scheduled_command(entry)`` -> ``(argv, display, working_dir)`` for a
      run of that entry with its own project's core; raises ``CoreError``;
    * ``scheduled_history(project_id)`` -> the run history of that project,
      read fresh, to write one entry into; raises ``CoreError`` for a project
      no longer on the list, whose run is then recorded nowhere else;
    * ``scheduled_blocked(entry)`` -> whether the window is running that very
      cycle now.
    """

    #: A scheduled run ended: the entry and its exit code.
    finished = Signal(dict, int)
    #: One started, one ended, or one is waiting: the status line may change.
    changed = Signal()

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.waiting = []
        self.current = None         # the entry running now
        self._entry_id = None
        self._log = []
        self._run_dir = ""
        self._stopping = False
        self.process = LauncherProcess(self)
        self.process.event.connect(self._event)
        self.process.log.connect(self._line)
        self.process.finished.connect(self._finished)
        self.process.failed.connect(self._failed)

    @staticmethod
    def _key(entry):
        return entry["project"], entry["cycle"], entry["trigger"]

    def due(self, entry):
        """Run ``entry`` when it can be. False when it is already waiting or
        running - a trigger has at most one of each."""
        key = self._key(entry)
        if any(self._key(one) == key for one in self.waiting):
            return False
        self.waiting.append(dict(entry))
        self.start_next()
        self.changed.emit()
        return True

    def forget_project(self, project_id):
        """Drop what waits for a project taken off the list."""
        self.waiting = [one for one in self.waiting if one["project"] != project_id]

    def running(self, project_id=None, cycle_id=None):
        """Whether a scheduled run is going - of that project and cycle, if given."""
        if self.current is None or not self.process.is_running():
            return False
        return ((project_id is None or self.current["project"] == project_id)
                and (cycle_id is None or self.current["cycle"] == cycle_id))

    def start_next(self):
        """Start the first waiting entry the window is not running itself."""
        if self.current is not None:
            return
        for index, entry in enumerate(self.waiting):
            if self.window.scheduled_blocked(entry):
                continue
            del self.waiting[index]
            self._start(entry)
            return

    def _start(self, entry):
        try:
            argv, display, working_dir = self.window.scheduled_command(entry)
        except core_mod.CoreError as exc:
            self._record_unstarted(entry, str(exc))
            QTimer.singleShot(0, self.start_next)
            return
        self.current, self._log, self._run_dir, self._stopping = entry, [], "", False
        history = self._history(entry)
        self._entry_id = history.begin(history_mod.CYCLE, {
            "summary": "%s - scheduled" % entry["cycle"],
            "argv": argv, "display_command": display,
            "config": {"cycle": entry["cycle"], "trigger": trigger_value(entry)}}) \
            if history is not None else None
        self.process.start(argv, working_dir=working_dir)
        self.changed.emit()

    def _history(self, entry):
        try:
            return self.window.scheduled_history(entry["project"])
        except core_mod.CoreError:
            return None

    def _record_unstarted(self, entry, message):
        history = self._history(entry)
        if history is None:
            return
        entry_id = history.begin(history_mod.CYCLE, {
            "summary": "%s - scheduled - not started: %s" % (entry["cycle"], message),
            "config": {"cycle": entry["cycle"], "trigger": trigger_value(entry)}})
        history.finish(entry_id, status=history_mod.ERROR)

    # -- what the run says ----------------------------------------------------
    def _event(self, event):
        if event.get("kind") == "run.dir" and event.get("dir"):
            self._run_dir = event["dir"]

    def _line(self, record):
        text = record.get("text", "")
        if record.get("level"):
            text = "%s %s" % (record["level"], text)
        self._log.append(text)
        del self._log[:-LOG_LINES]

    def _failed(self, message):
        self._log.append(message)
        if not self.process.is_running():
            self._finished(-1, failed=True)

    def _finished(self, code, failed=False):
        entry, self.current = self.current, None
        if entry is None:
            return
        entry_id, self._entry_id = self._entry_id, None
        history = self._history(entry) if entry_id is not None else None
        if history is not None:
            log_path = history.log_path(entry_id)
            try:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                with open(log_path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(self._log) + ("\n" if self._log else ""))
            except OSError:
                log_path = ""
            history.finish(entry_id, status=history_mod.status_for(
                code, stopped=self._stopping, failed_to_start=failed),
                exit_code=code, run_dir=self._run_dir, log_file=log_path)
        self.finished.emit(entry, int(code))
        self.changed.emit()
        QTimer.singleShot(0, self.start_next)

    # -- what to show ----------------------------------------------------------
    def status(self):
        """One short line: running now, or how many wait; "" for neither."""
        if self.current is not None:
            return "running %s" % self.current["name"]
        if self.waiting:
            return "%d waiting" % len(self.waiting)
        return ""

    def shutdown(self, wait_ms=CLOSE_WAIT_MS):
        """The window is closing: nothing more starts, and a run going is
        stopped the launcher's own graceful way and waited for."""
        self.waiting = []
        if self.process.is_running():
            self._stopping = True
            proc = self.process._proc
            self.process.stop()
            if proc is not None and not proc.waitForFinished(wait_ms):
                self.process.kill()
                proc.waitForFinished(1000)
