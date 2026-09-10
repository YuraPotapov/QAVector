"""Driving the launcher as a child process, and turning its output into signals.

Two channels come back and they are deliberately different things:

* **stdout** carries the ``--events=-`` JSONL stream - structured, ordered,
  machine-made. It drives the Run and Artifacts pages.
* **stderr** carries human log lines. It drives the Log page.

Everything is signal-driven on the Qt event loop; nothing here ever blocks, so a
launcher that hangs leaves the GUI perfectly responsive (and stoppable).
"""

import collections
import json
import os
import re
import signal
import sys
import time

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

# "13:38:26  INFO    [dev-agent] message" - the launcher's console format, with
# the session prefix the runner adds during parallel runs.
LOG_LINE = re.compile(r"^(?P<ts>\d{2}:\d{2}:\d{2})\s+(?P<level>[A-Z]+)\s+"
                      r"(?:\[(?P<session>[^\]]+)\]\s*)?(?P<text>.*)$")

# Backend log lines kept per session (--server-log). A run against a busy server
# can produce a great many, and every one of them would otherwise live in the
# model for as long as the window is open.
SERVER_LOG_LINES = 2000

# A session in one of these has a window on screen that Stop can still act on.
# "stopping" is deliberately not among them: it has been told to go, and both
# the Stop menu and the rail's count are about what is still there to stop.
LIVE_STATES = ("launching", "launched", "attached", "running")


def parse_log_line(line):
    """Split a console line into its parts; unparseable lines stay whole."""
    match = LOG_LINE.match(line)
    if not match:
        return {"ts": "", "level": "", "session": "", "text": line}
    return {k: (match.group(k) or "") for k in ("ts", "level", "session", "text")}


class LauncherProcess(QObject):
    """One run of ``session_launcher.py``, watched live."""

    event = Signal(dict)          # one parsed JSONL event
    log = Signal(dict)            # one parsed stderr line
    started = Signal(list)        # the argv actually used
    finished = Signal(int)        # exit code
    failed = Signal(str)          # could not start at all

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._out_buffer = ""
        self._err_buffer = ""
        self._own_group = False

    # -- lifecycle ------------------------------------------------------------
    def is_running(self):
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    def start(self, argv, working_dir=None):
        if self.is_running():
            return False
        proc = QProcess(self)
        # Keep the channels apart: events must never be diluted with log text.
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._read_stdout)
        proc.readyReadStandardError.connect(self._read_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        if working_dir:
            proc.setWorkingDirectory(working_dir)
        self._own_group = False
        if os.name == "nt" and hasattr(proc, "setCreateProcessArgumentsModifier"):
            # Own process group, so a CTRL_BREAK can be delivered on stop -
            # Windows has no SIGINT to send to another process.
            #
            # Guarded because PySide6 does not bind this Qt method (6.11 has
            # only {set,}nativeArguments). Calling it unconditionally raised
            # AttributeError here, before proc.start() - so on Windows every
            # run died at "Launching...", with no process and no error. A
            # missing process group must cost the graceful stop below, never
            # the run itself.
            proc.setCreateProcessArgumentsModifier(_new_process_group)
            self._own_group = True
        self._proc = proc
        self._out_buffer = self._err_buffer = ""
        proc.start(argv[0], list(argv[1:]))
        self.started.emit(list(argv))
        return True

    def stop(self):
        """Ask for the launcher's own graceful shutdown (it closes the windows).

        SIGINT is what the core is written around: ``keep_open_until_closed``
        turns it into the ordered ``close_all`` that flushes each login session
        to disk. Killing outright would orphan or corrupt those profiles.
        """
        if not self.is_running():
            return
        pid = int(self._proc.processId())
        if os.name == "nt":
            if self._own_group:
                try:
                    import ctypes
                    # CTRL_BREAK_EVENT (1) - CTRL_C cannot be sent to another
                    # group. A zero return means it was not delivered, so fall
                    # through rather than report a stop that never happened.
                    if ctypes.windll.kernel32.GenerateConsoleCtrlEvent(1, pid):
                        return
                except Exception:
                    pass
            # No group to signal: terminate() only reaches a process with a
            # window, which a console core has none of, so the kill is what
            # actually ends it. The windows go with it - the launcher puts every
            # Chrome it starts in a kill-on-close job owned by that process.
            self._proc.terminate()
            QTimer.singleShot(2000, self._kill_if_still_running)
            return
        try:
            os.kill(pid, signal.SIGINT)
        except (ProcessLookupError, PermissionError, OSError):
            self._proc.terminate()

    def send_command(self, **command):
        """Write one JSON command line to the launcher's stdin (--control=-).

        The inbound half of the event stream. Returns False when there is no
        launcher to tell, so a caller acting on a stale menu is a no-op rather
        than an error.
        """
        if not self.is_running():
            return False
        line = json.dumps(command) + "\n"
        self._proc.write(line.encode("utf-8"))
        return True

    def _kill_if_still_running(self):
        """The second half of a Windows stop, once terminate() has had its go."""
        if self.is_running():
            self._proc.kill()

    def kill(self):
        if self.is_running():
            self._proc.kill()

    # -- output ---------------------------------------------------------------
    def _read_stdout(self):
        data = bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")
        if not data:
            return
        self._out_buffer += data
        # Only whole lines are events; a partial one waits for the rest.
        if "\n" in self._out_buffer:
            lines = self._out_buffer.split("\n")
            self._out_buffer = lines.pop()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    # Not an event: the core printed something else on stdout.
                    self.log.emit({"ts": "", "level": "", "session": "", "text": line})
                    continue
                if isinstance(payload, dict):
                    self.event.emit(payload)

    def _read_stderr(self):
        data = bytes(self._proc.readAllStandardError()).decode("utf-8", "replace")
        if not data:
            return
        self._err_buffer += data
        if "\n" in self._err_buffer:
            lines = self._err_buffer.split("\n")
            self._err_buffer = lines.pop()
            for line in lines:
                if line.strip():
                    self.log.emit(parse_log_line(line.rstrip()))

    def _flush(self):
        for buffer, emit in ((self._out_buffer, None), (self._err_buffer, self.log)):
            if buffer.strip() and emit is not None:
                emit.emit(parse_log_line(buffer.rstrip()))
        self._out_buffer = self._err_buffer = ""

    def _on_finished(self, code, _status):
        self._read_stdout()
        self._read_stderr()
        self._flush()
        self.finished.emit(int(code))

    def _on_error(self, error):
        if error == QProcess.FailedToStart:
            self.failed.emit("Could not start %s - check the interpreter and core "
                             "script in Settings." % (self._proc.program(),))


def _new_process_group(args):
    """QProcess modifier: give the child its own console group (Windows only)."""
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    args.flags |= CREATE_NEW_PROCESS_GROUP


class RunState(QObject):
    """Live model of a run, assembled from the event stream.

    The launcher reports facts one at a time (a window launched, a step ended);
    the pages want the current shape of the whole run. This is the one place
    that turns the former into the latter, so no page has to keep its own tally.
    """

    changed = Signal()
    run_dir_known = Signal(str)
    artifacts_written = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.reset()

    def reset(self):
        self.sessions = {}        # session name -> dict
        self.order = []           # session names, in launch order
        self.run_dir = ""
        self.summary = None
        self.exit_code = None
        self.started = False
        # The scenarios are finished, whether or not the launcher has exited:
        # without --close-after it stays up holding the windows open.
        self.flows_finished = False
        # Only --jobs=auto reports one: with a fixed number there is nothing to
        # watch, because nothing moves it.
        self.workers = None       # {"limit", "ceiling", "unit", "why"}
        self.changed.emit()

    # -- ingestion ------------------------------------------------------------
    def _session(self, name, login=None):
        if name not in self.sessions:
            self.sessions[name] = {"name": name, "login": login or name,
                                   "state": "launching", "pid": None,
                                   "scenarios": [], "tree": None, "steps": {},
                                   "current": None, "done": 0, "total": 0,
                                   "scenario": "", "flows": [],
                                   # What the current scenario's record in "runs"
                                   # is filed under: its id when the core says it.
                                   "scenario_key": "",
                                   # Backend log lines belonging to THIS window
                                   # (--server-log), newest last, capped so a
                                   # chatty server cannot grow the model without
                                   # bound over a long run. "server_logs" is the
                                   # names seen so far, which is what the panel's
                                   # filter offers.
                                   "server": collections.deque(maxlen=SERVER_LOG_LINES),
                                   "server_logs": [],
                                   # Every scenario this session has reached, in
                                   # the order it reached them. The fields above
                                   # are the CURRENT one; without this the rest
                                   # are gone the moment the next flow starts,
                                   # which is why the Run page could only ever
                                   # show the last scenario while the in-page
                                   # overlay showed the whole list.
                                   "runs": collections.OrderedDict()}
            self.order.append(name)
        return self.sessions[name]

    def handle(self, event):
        kind = event.get("kind", "")
        name = event.get("session")
        handler = getattr(self, "_on_" + kind.replace(".", "_"), None)
        if handler is not None:
            handler(event, name)
        self.changed.emit()

    def _on_launcher_start(self, event, _name):
        self.started = True
        for user in event.get("users", []):
            pass   # windows announce themselves individually with their profile

    def _on_window_launched(self, event, _name):
        session = self._session(event.get("session") or event.get("login", "?"),
                                event.get("login"))
        session["pid"] = event.get("pid")
        session["state"] = "launched"

    def _on_serverlog_lines(self, event, name):
        """One batch of backend log lines, already filtered to this window.

        The launcher decided which session each line belongs to (it knows when the
        window opened and what the line's own timestamp is); all that is left here
        is to keep them and remember the log's name for the panel's filter.
        """
        if not name:
            return
        session = self._session(name)
        log_name = event.get("log") or "server"
        if log_name not in session["server_logs"]:
            session["server_logs"].append(log_name)
        for line in event.get("lines", []):
            session["server"].append({"log": log_name,
                                      "ts": line.get("ts") or 0,
                                      "level": line.get("level") or "INFO",
                                      "text": line.get("text") or ""})

    def _on_governor_limit(self, event, _name):
        """How many sessions may run right now, and why it last changed.

        Kept apart from the launch page's own number: that one is what the user
        asked for and is theirs to change, this one is what is in force for this
        run and nobody typed it.
        """
        self.workers = {"limit": event.get("limit"),
                        "ceiling": event.get("ceiling"),
                        "unit": event.get("unit") or "sessions",
                        "why": event.get("why") or ""}

    def _on_windows_ready(self, _event, _name):
        for session in self.sessions.values():
            if session["state"] == "launching":
                session["state"] = "launched"

    def _on_session_attached(self, event, name):
        session = self._session(name, event.get("login"))
        session["state"] = "attached"

    def _on_session_attach_failed(self, event, name):
        session = self._session(name, event.get("login"))
        session["state"] = "failed"

    def _on_session_start(self, event, name):
        session = self._session(name)
        session["scenarios"] = list(event.get("scenarios", []))
        session["state"] = "attached"

    def _on_flow_start(self, event, name):
        session = self._session(name)
        session["state"] = "running"
        session["scenario"] = event.get("scenario", "")
        # Filed under the id, which is what session.start listed. Filed under the
        # name, a scenario with a name: of its own showed twice - once done, and
        # once more, by id, as still to come. A core older than the id sends the
        # name alone, and then the name is the key, as it always was.
        session["scenario_key"] = event.get("id") or session["scenario"]
        session["tree"] = event.get("tree")
        session["total"] = int(event.get("steps") or 0)
        session["done"] = 0
        session["steps"] = {}
        session["current"] = None
        # Its own record, kept for the rest of the run. The step handlers below
        # write through to it, so the finished scenarios keep their marks.
        session["runs"][session["scenario_key"]] = {
            "scenario": session["scenario"], "tree": session["tree"],
            "steps": session["steps"], "total": session["total"],
            "done": 0, "status": "running",
            # The launcher's clock, stamped on the event: how long a scenario
            # took is measured where it ran, not when this window got to it.
            "started": event.get("ts") or time.time(), "ended": None}

    def _on_step_start(self, event, name):
        session = self._session(name)
        index = event.get("index")
        session["current"] = index
        session["steps"][index] = {"status": "running", "ms": None}

    def _on_step_end(self, event, name):
        session = self._session(name)
        index = event.get("index")
        status = event.get("status", "")
        session["steps"][index] = {"status": status, "ms": None,
                                   "message": event.get("message", "")}
        if status == "pass":
            session["done"] += 1
        session["current"] = None
        run = session["runs"].get(session.get("scenario_key"))
        if run is not None:
            run["done"] = session["done"]

    def _on_step_retry(self, event, name):
        session = self._session(name)
        step = session["steps"].setdefault(event.get("index"), {})
        step["retry"] = event.get("attempt")

    def _on_flow_end(self, event, name):
        session = self._session(name)
        status = event.get("status", "")
        session["flows"].append({"scenario": session.get("scenario", ""),
                                 "status": status,
                                 "passed": event.get("passed"),
                                 "total": event.get("total")})
        run = session["runs"].get(session.get("scenario_key"))
        if run is not None:
            run["status"] = status
            run["done"] = event.get("passed", run["done"])
            run["total"] = event.get("total", run["total"])
            run["ended"] = event.get("ts") or time.time()
        # The WORST outcome so far, not the latest one. A session whose first
        # scenario failed and whose last one passed has not passed, and saying
        # PASS beside a tree with a red mark in it is worse than saying nothing.
        # Read from the per-scenario records rather than from session["state"]:
        # that one is set back to "running" by every flow.start, so it cannot
        # remember a failure across scenarios.
        failed = any(run.get("status") not in ("pass", "running")
                     for run in session["runs"].values())
        session["state"] = "failed" if failed else "passed"

    def _on_session_stopping(self, event, name):
        self.mark_stopping(name or event.get("session"))

    def mark_stopping(self, name):
        """Show a window as going down as soon as it is asked to.

        Called straight from the Stop menu as well as from the event: the core
        has to finish the step it is in before it can answer, and a menu entry
        that looks like it did nothing invites a second click.
        """
        if name in self.sessions:
            self.sessions[name]["state"] = "stopping"
            self.changed.emit()

    def _on_run_dir(self, event, _name):
        self.run_dir = event.get("dir", "")
        if self.run_dir:
            self.run_dir_known.emit(self.run_dir)

    def _on_artifacts_written(self, event, _name):
        self.artifacts_written.emit(event)

    def _on_run_summary(self, event, _name):
        self.summary = event

    def _on_run_finished(self, event, _name):
        """The scenarios are done - which is NOT the launcher being done.

        Without --close-after the launcher stays up holding the windows open for
        inspection, so waiting for the process to exit left the Run page saying
        RUNNING with a ticking clock long after the last step.
        """
        self.flows_finished = True
        self.exit_code = event.get("exit_code")

    def _on_window_exited(self, event, _name):
        for session in self.sessions.values():
            if session.get("pid") == event.get("pid"):
                if session["state"] in ("launching", "launched", "attached"):
                    session["state"] = "closed"

    # -- reading --------------------------------------------------------------
    def ordered(self):
        return [self.sessions[n] for n in self.order if n in self.sessions]

    def live(self):
        """The windows still up: what Stop offers, and what the rail counts.

        Not ``sessions`` - that keeps every window the run ever opened, finished
        and closed ones included, which is the right answer for the report and
        the wrong one for "how many are on screen right now".
        """
        return [s for s in self.ordered() if s["state"] in LIVE_STATES]

    def totals(self):
        done = sum(s["done"] for s in self.sessions.values())
        total = sum(s["total"] for s in self.sessions.values())
        return done, total
