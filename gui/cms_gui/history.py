"""Every run the GUI started, kept so it can be read and started again.

The launcher itself keeps no record of an invocation, and the GUI used to keep
none either: ``RunState.reset()`` and ``LogPage.clear()`` wipe the previous run
the moment a new one starts. So a run's own account of itself is assembled here
instead, from what was asked for (the configuration) and what came back (the
exit code, the pass count, the run directory, an archived copy of the log).

An entry is therefore not a log line - it is enough to reproduce the run. That
is why both halves are stored: the user-facing configuration *and* the argv it
was translated into, so a Launch Sessions entry can go back to the Launch
Sessions page and a Command entry back to the Command page, each restoring the
form the user actually filled in.
"""

import datetime
import os

from PySide6.QtCore import QObject, Signal

from . import store

LAUNCH = "launch"
COMMAND = "command"
#: A cycle run. A third kind rather than a second record, so History lists all
#: three the same way and "what ran here yesterday" has one answer.
CYCLE = "cycle"

# Terminal statuses, plus the one an entry wears while its run is in flight. A
# run that never reached a terminal state (the GUI was killed mid-run) keeps
# "running" forever, which is honest: nothing knows how it ended.
RUNNING = "running"
OK = "ok"
FAILED = "failed"
STOPPED = "stopped"
ERROR = "error"

STATUS_LABELS = {RUNNING: "running", OK: "passed", FAILED: "failed",
                 STOPPED: "stopped", ERROR: "error"}

MAX_ENTRIES = 200


class History(QObject):
    """The run record: newest first, capped, backed by one JSON file."""

    changed = Signal()

    def __init__(self, directory=None, parent=None):
        super().__init__(parent)
        self.directory = directory or store.app_data_dir()
        self._store = store.JsonStore(os.path.join(self.directory, "history.json"),
                                      default=[])
        self._entries = [e for e in self._store.load() if isinstance(e, dict)]

    # -- reading --------------------------------------------------------------
    def entries(self, kind=None):
        """Newest first; optionally only ``launch`` or ``command`` ones."""
        if kind is None:
            return list(self._entries)
        return [e for e in self._entries if e.get("kind") == kind]

    def entry(self, entry_id):
        for candidate in self._entries:
            if candidate.get("id") == entry_id:
                return candidate
        return None

    def with_artifacts(self):
        """Entries whose report directory is still on disk, newest first.

        The Artifacts page has no memory of its own: the run directory used to
        arrive in an event and vanish with the process. This is where it comes
        back from after a restart.
        """
        return [e for e in self._entries
                if e.get("run_dir") and os.path.isdir(e["run_dir"])]

    def with_logs(self):
        """Entries whose archived log is still on disk, newest first."""
        return [e for e in self._entries
                if e.get("log_file") and os.path.isfile(e["log_file"])]

    def log_path(self, entry_id):
        """Where this entry's archived log belongs (whether or not it exists)."""
        return os.path.join(store.logs_dir(self.directory), "%s.log" % entry_id)

    # -- writing --------------------------------------------------------------
    def begin(self, kind, payload):
        """Open an entry for a run that is starting; returns its id."""
        entry = {"id": self._next_id(), "kind": kind,
                 "started_at": _now(), "finished_at": "", "duration_ms": None,
                 "status": RUNNING, "exit_code": None, "passed": 0, "total": 0,
                 "run_dir": "", "log_file": "",
                 "argv": [], "display_command": "", "summary": ""}
        entry.update(payload or {})
        entry["id"] = entry["id"] or self._next_id()
        self._entries.insert(0, entry)
        self._evict()
        self._flush()
        return entry["id"]

    def note(self, entry_id, **fields):
        """Record something about a run that is still going. Unknown ids ignored.

        Not :meth:`finish`: that stamps the entry as over and works out how
        long it took, so using it for "here is where this run is writing its
        files" would close an entry for a run still in progress and leave it
        with a duration measured to the wrong moment.
        """
        entry = self.entry(entry_id)
        if entry is None:
            return None
        entry.update(fields)
        self._flush()
        return entry

    def finish(self, entry_id, **fields):
        """Close an entry with how the run ended. Unknown ids are ignored."""
        entry = self.entry(entry_id)
        if entry is None:
            return None
        entry.update(fields)
        entry["finished_at"] = fields.get("finished_at") or _now()
        if entry.get("duration_ms") is None:
            entry["duration_ms"] = _elapsed_ms(entry.get("started_at"),
                                               entry["finished_at"])
        self._flush()
        return entry

    def remove(self, entry_id):
        before = len(self._entries)
        for entry in list(self._entries):
            if entry.get("id") == entry_id:
                self._drop_log(entry)
                self._entries.remove(entry)
        if len(self._entries) != before:
            self._flush()

    def clear(self):
        for entry in self._entries:
            self._drop_log(entry)
        self._entries = []
        self._flush()

    # -- housekeeping ---------------------------------------------------------
    def _next_id(self):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        taken = {e.get("id") for e in self._entries}
        candidate, suffix = stamp, 1
        while candidate in taken:
            suffix += 1
            candidate = "%s-%d" % (stamp, suffix)
        return candidate

    def _evict(self):
        """Drop the oldest entries past the cap, and the logs they own."""
        while len(self._entries) > MAX_ENTRIES:
            self._drop_log(self._entries.pop())

    def _drop_log(self, entry):
        path = entry.get("log_file") or ""
        # Only ever delete inside our own logs directory: an entry could carry a
        # path the user chose with "Save log…" and that file is theirs.
        if path and os.path.dirname(os.path.abspath(path)) == store.logs_dir(
                self.directory):
            store._unlink(path)

    def _flush(self):
        self._store.save(self._entries)
        self.changed.emit()


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _elapsed_ms(started, finished):
    try:
        start = datetime.datetime.fromisoformat(started)
        end = datetime.datetime.fromisoformat(finished)
    except (TypeError, ValueError):
        return None
    return int((end - start).total_seconds() * 1000)


def status_for(exit_code, stopped=False, failed_to_start=False):
    """How a finished run should be recorded."""
    if failed_to_start:
        return ERROR
    if stopped:
        return STOPPED
    return OK if exit_code == 0 else FAILED


def entry_label(entry):
    """One short line naming a recorded run, for a picker.

    Shared by the Artifacts and Log pages so a run is called the same thing
    wherever it is offered. Nothing marks the newest one: the lists are ordered
    newest first, so it is the one already sitting at the top.
    """
    from . import launch                       # local: launch imports nothing here

    # Kept short on purpose: this goes in a combo box, where anything longer is
    # elided away and stops being a label at all. Today's date is dropped for the
    # same reason - it is the same for most of the list and says nothing.
    when = (entry.get("started_at", "") or "")[:16]      # to the minute
    today = datetime.date.today().isoformat()
    if when.startswith(today):
        when = when[len(today):].strip()
    kind = {LAUNCH: "Launch", CYCLE: "Cycle"}.get(entry.get("kind"), "Command")
    if entry.get("kind") == LAUNCH:
        where = launch.env_label(entry.get("launch_config") or {}).split(" (")[0]
    elif entry.get("kind") == CYCLE:
        # Which cycle ran. Called "Command" with an environment it never had,
        # a cycle run was the one kind of entry the list could not name.
        where = (entry.get("config") or {}).get("cycle") or ""
    else:
        where = (entry.get("command_state") or {}).get("--env") or "all envs"
    status = STATUS_LABELS.get(entry.get("status", ""), entry.get("status", ""))
    total = entry.get("total") or 0
    if total:
        status = "%d/%d %s" % (entry.get("passed") or 0, total, status)
    parts = [when, kind, where, status]
    return " · ".join(p for p in parts if p)


def format_duration(ms):
    """A duration a person can read: 0.4s, 12s, 3m 07s, 1h 04m."""
    if ms is None:
        return ""
    seconds = ms / 1000.0
    if seconds < 1:
        return "%.1fs" % seconds
    if seconds < 60:
        return "%ds" % int(seconds)
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return "%dm %02ds" % (minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    return "%dh %02dm" % (hours, minutes)
