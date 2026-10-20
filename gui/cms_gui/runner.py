"""Driving the launcher as a child process, and turning its output into signals.

Two channels come back and they are deliberately different things:

* **stdout** carries the ``--events=-`` JSONL stream - structured, ordered,
  machine-made. It drives the Run and Artifacts pages.
* **stderr** carries human log lines. It drives the Log page.

Everything is signal-driven on the Qt event loop; nothing here ever blocks, so a
launcher that hangs leaves the GUI perfectly responsive (and stoppable).
"""

import collections
import copy
import json
import os
import re
import signal
import sys
import time

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from . import cycleresult

TIMING_FIELDS = ("active_elapsed_ms", "execution_generation", "execution_segments",
                 "last_activity_at", "progress", "timeout_remaining_ms")

# "13:38:26  INFO    [dev-agent] message" - the launcher's console format, with
# the session prefix the runner adds during parallel runs.
LOG_LINE = re.compile(r"^(?P<ts>\d{2}:\d{2}:\d{2})\s+(?P<level>[A-Z]+)\s+"
                      r"(?:\[(?P<session>[^\]]+)\]\s*)?(?P<text>.*)$")

# Backend log lines kept per session (--server-log). A run against a busy server
# can produce a great many, and every one of them would otherwise live in the
# model for as long as the window is open.
SERVER_LOG_LINES = 2000

# Output lines kept per cycle step. Lower than the server's, because a cycle
# keeps one of these per step rather than per window, and every one of them is
# also on disk under the run's own directory - this is what the page shows, not
# the record.
CYCLE_LOG_LINES = 500

#: And for the run as a whole. Larger than one step's cap because it holds every
#: step's output interleaved, and a run of a dozen steps would otherwise show
#: only the last one to have said anything.
CYCLE_RUN_LOG_LINES = 4000

#: How many stages are kept, per step and for the run. A stage is a row
#: somebody reads rather than a line of output, so far fewer are worth holding
#: than log lines - and an agent that ran away would otherwise fill the panel.
CYCLE_STAGES = 400

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


#: How a cycle run ends that the rail shows in red - what the Cycles explorer
#: marks as a run that went wrong. "cancelled" is not among them: Stop is a
#: person's decision, not a fault.
CYCLE_FAILED = ("failed", "timeout", "interrupted", "error")


class RunState(QObject):
    """Live model of a run, assembled from the event stream.

    The launcher reports facts one at a time (a window launched, a step ended);
    the pages want the current shape of the whole run. This is the one place
    that turns the former into the latter, so no page has to keep its own tally.
    """

    changed = Signal()
    run_dir_known = Signal(str)
    artifacts_written = Signal(dict)
    #: A cycle run announced itself, with the graph its page should draw.
    cycle_graph_known = Signal(dict)
    #: One step of a cycle changed. Carries the step id, so the page repaints
    #: one node instead of re-reading the whole model.
    cycle_step_changed = Signal(str)
    cycle_timing_changed = Signal()
    #: The cycle run found out what it works on, or said how it began. The
    #: page's Subjects tab repaints on it; nothing else has to.
    cycle_subject_changed = Signal()
    cycle_interactions_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.reset_cycle()
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
        # Deliberately NOT self.cycle. A scenario run calls this too, and
        # wiping the cycle record there left the Cycles page with a coloured
        # graph beside an empty Output and Stages - a run half forgotten.
        # A cycle is forgotten by reset_cycle, which only a cycle run calls,
        # and _on_cycle_run_start replaces the record wholesale anyway.
        self.changed.emit()

    def reset_cycle(self):
        """Forget the cycle run. Its own call, because its own lifetime.

        The cycle a page is showing outlives any number of scenario runs - it
        is what the last cycle run did, and nothing about starting a browser
        session makes that untrue.
        """
        #: A cycle run, when this run is one. None for an ordinary scenario run,
        #: so a page can tell the two apart by asking rather than by guessing
        #: from which fields happen to be filled in.
        self.cycle = None         # see _on_cycle_run_start for its shape
        #: Whether a cycle is going *now*. Separate from ``started`` and
        #: ``flows_finished``, which belong to the scenario run: with one flag
        #: for both, starting a scenario lit the Cycles page's Stop button for
        #: a cycle that had finished an hour ago.
        self.cycle_running = False
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

    # -- cycles ---------------------------------------------------------------
    # Found by the dispatch above with no change to it: "cycle.step.end" becomes
    # "_on_cycle_step_end". A kind this version does not know is simply ignored,
    # so a newer core talking to an older GUI loses a detail rather than a run.
    #
    # A cycle has no sessions - its steps are not windows - so none of these
    # touch ``self.sessions``. They keep one dict, and the page redraws from it.

    def _on_cycle_run_start(self, event, _name):
        self.started = True
        self.cycle_running = True
        graph = event.get("graph") or {}
        self.cycle = {
            "run_id": event.get("run_id", ""),
            "cycle": event.get("cycle", ""),
            "name": event.get("name", ""),
            "workspace": event.get("workspace", ""),
            # Where this run's files are, kept with the run rather than read off
            # ``self.run_dir``: that one is the last run of any kind, and a
            # scenario started afterwards would point a cycle's artifacts at
            # its own folder. The launcher says run.dir before the cycle
            # starts, so it is already known here.
            "run_dir": self.run_dir or event.get("workspace", ""),
            "jobs": event.get("jobs"),
            "variables": dict(event.get("variables") or {}),
            "graph": graph,
            "status": "running",
            # step id -> what is known about it now. Seeded from the graph so
            # every node has a record before anything has run, which is what
            # lets a page render the whole cycle on the first event.
            "steps": {node.get("id"): {"status": "pending", "plugin":
                                       node.get("plugin", ""), "attempt": 0,
                                       "duration_ms": 0, "message": "",
                                       "outputs": {}, "artifacts": [],
                                       "log": [], "stages": []}
                      for node in (graph.get("nodes") or [])},
            # Everything every step said, in the order it was said, as
            # (step id, stream, line). A cycle runs several steps at once and
            # the interesting thing is usually what the run as a whole is
            # doing; one step's output is the narrower view, not the only one.
            "log": [],
            # And the stages of every step that has any, as (step id, stage).
            # Most plugins emit none; an agent emits one per turn.
            "stages": [],
            "interactions": {},
            "interaction_offers": {},
            # What the run works on, once it has found out - see
            # _on_cycle_subject. Empty until then, and for a cycle that
            # declares none.
            "subject": {},
            # How it began: fresh, a resume, a partial run - and what it kept
            # and will do again. See _on_cycle_run_mode.
            "mode": {},
            # Every time a person sent the plan back, in order.
            "revisions": [],
            "timing_version": event.get("timing_version", 0),
            "control_version": event.get("control_version", 0),
            "plugin_names": dict(event.get("plugin_names") or {}),
        }
        if event.get("selected_steps") is not None:
            self.cycle["selected_steps"] = list(event["selected_steps"])
        cycleresult.update(self.cycle, event)
        self.cycle_graph_known.emit(graph)

    def _on_cycle_run_mode(self, event, _name):
        if self.cycle is None:
            return
        self.cycle["mode"] = {
            "mode": event.get("mode", ""),
            "resume_count": event.get("resume_count", 0),
            "source_run": event.get("source_run", ""),
            "kept": list(event.get("kept") or []),
            "rerun": [dict(one) for one in event.get("rerun") or []
                      if isinstance(one, dict)]}
        self.cycle["scope"] = {"fresh": "full", "action": "partial"}.get(
            event.get("mode"), event.get("mode")) or "full"
        cycleresult.update(self.cycle)
        self.cycle_subject_changed.emit()

    def _on_cycle_subject(self, event, _name):
        if self.cycle is None:
            return
        self.cycle["subject"] = dict(event.get("subject") or {})
        self.cycle_subject_changed.emit()

    def _on_cycle_revision(self, event, _name):
        if self.cycle is None:
            return
        self.cycle.setdefault("revisions", []).append({
            "number": event.get("number", 0), "gate": event.get("gate", ""),
            "target": event.get("target", ""),
            "feedback": event.get("feedback", ""), "who": event.get("who", ""),
            "steps": list(event.get("steps") or [])})
        self.cycle_subject_changed.emit()

    def _cycle_step(self, step_id):
        """The record for one step, made if the graph did not mention it."""
        if self.cycle is None or not step_id:
            return None
        return self.cycle["steps"].setdefault(
            step_id, {"status": "pending", "plugin": "", "attempt": 0,
                      "duration_ms": 0, "message": "", "outputs": {},
                      "artifacts": [], "log": [], "stages": []})

    def _on_cycle_step_start(self, event, _name):
        step = self._cycle_step(event.get("step"))
        if step is None:
            return
        step["status"] = "running"
        step["reason_code"], step["blocked_by"] = "", ""
        step["started_at"] = event.get("started_at") or event.get("ts") or 0
        step["attempt"] = event.get("attempt", 1)
        step["plugin"] = event.get("plugin") or step["plugin"]
        self._timing_fields(step, event)
        cycleresult.update(self.cycle)
        self.cycle_step_changed.emit(event.get("step", ""))

    def _on_cycle_step_end(self, event, _name):
        step = self._cycle_step(event.get("step"))
        if step is None:
            return
        step["status"] = event.get("status", "")
        step["duration_ms"] = event.get("duration_ms", 0)
        step["attempt"] = event.get("attempts", step["attempt"])
        step["message"] = event.get("message", "")
        step["outputs"] = dict(event.get("outputs") or {})
        if event.get("runtime"):
            step["runtime"] = dict(event["runtime"])
        step["started_at"] = event.get("started_at") or step.get("started_at", 0)
        step["ended_at"] = event.get("ended_at") or event.get("ts") or 0
        for key in ("source_run", "reason_code", "blocked_by"):
            step[key] = event.get(key, "")
        self._timing_fields(step, event)
        cycleresult.update(self.cycle)
        self.cycle_step_changed.emit(event.get("step", ""))

    def _on_cycle_step_retry(self, event, _name):
        step = self._cycle_step(event.get("step"))
        if step is None:
            return
        step["attempt"] = event.get("attempt", step["attempt"])
        self.cycle_step_changed.emit(event.get("step", ""))

    def _on_cycle_step_waiting(self, event, _name):
        """The step is not finished and asked to be come back to.

        Not an end: the status is kept as waiting and the record is not closed,
        because the step is still the run's business. What it produced while
        looking is kept too - a plugin that found nothing ready may still have
        learned something worth reading.
        """
        step = self._cycle_step(event.get("step"))
        if step is None:
            return
        step["status"] = "waiting"
        step["message"] = event.get("message", "")
        step["resume_in_ms"] = event.get("resume_in_ms", 0)
        step["outputs"] = dict(event.get("outputs") or {})
        self._timing_fields(step, event)
        cycleresult.update(self.cycle)
        self.cycle_step_changed.emit(event.get("step", ""))

    def _on_cycle_step_skipped(self, event, _name):
        step = self._cycle_step(event.get("step"))
        if step is None:
            return
        step["status"] = event.get("status", "skipped")
        step["message"] = event.get("reason", "")
        step["reason_code"] = event.get("reason_code", "")
        step["blocked_by"] = event.get("blocked_by", "")
        cycleresult.update(self.cycle)
        self.cycle_step_changed.emit(event.get("step", ""))

    def _on_cycle_step_log(self, event, _name):
        """One batch of a step's output. Capped, like the server log is."""
        step = self._cycle_step(event.get("step"))
        if step is None:
            return
        stream = event.get("stream", "out")
        step_id = event.get("step", "")
        step["last_activity_at"] = event.get("ts") or time.time()
        for line in event.get("lines") or []:
            step["log"].append((stream, line))
            self.cycle["log"].append((step_id, stream, line))
        del step["log"][:-CYCLE_LOG_LINES]
        del self.cycle["log"][:-CYCLE_RUN_LOG_LINES]
        self.cycle_step_changed.emit(step_id)

    def _on_cycle_step_stage(self, event, _name):
        """One stage of a step's inner work - what an agent is doing now.

        Kept apart from the log because it is not output: these are rows to
        read, not text a process printed. A plugin that emits none simply has
        an empty list, which is what every plugin but the agent has.
        """
        step = self._cycle_step(event.get("step"))
        if step is None:
            return
        step_id = event.get("step", "")
        if event.get("phase") == "runtime" and isinstance((event.get("body") or {}).get("runtime"), dict):
            step["runtime"] = dict(event["body"]["runtime"])
        step["last_activity_at"] = event.get("ts") or time.time()
        step.setdefault("progress", {})["title"] = event.get("title", "")
        stage = {"kind": event.get("phase", ""), "title": event.get("title", ""),
                 "detail": event.get("detail", ""),
                 "status": event.get("status", "done"),
                 # Whatever structure this row has beyond its text - a parsed
                 # review, today. Absent for every other stage.
                 "body": dict(event.get("body") or {})}
        if event.get("ts"):
            stage["ts"] = event["ts"]
        step["stages"].append(stage)
        del step["stages"][:-CYCLE_STAGES]
        self.cycle["stages"].append((step_id, stage))
        del self.cycle["stages"][:-CYCLE_STAGES]
        self.cycle_step_changed.emit(step_id)

    @staticmethod
    def _timing_fields(record, event):
        generation = event.get("execution_generation")
        if generation is not None and generation > record.get("execution_generation", 1):
            # These are the current execution's deliverables and detail view.
            # The run-wide journal still keeps earlier executions' diagnostics.
            for key in ("artifacts", "log", "stages"):
                record[key] = []
        # A worker may emit progress between the scheduler sampling it and
        # publishing the timing receipt. Keep the newer diagnostic evidence.
        stamp, previous = event.get("last_activity_at"), record.get("last_activity_at")
        newer_activity = stamp is not None and previous is not None and previous > stamp
        for key in TIMING_FIELDS:
            if key in event and not (newer_activity and key in ("last_activity_at", "progress")):
                record[key] = copy.deepcopy(event[key])
        if event.get("active_elapsed_ms") is not None:
            record["_timing_received"] = time.monotonic()
            record["_timing_wall"] = time.time()
            record["_timing_ttl_ms"] = event.get("ttl_ms", 5000)

    def _on_cycle_run_timing(self, event, _name):
        if not self._interaction_run(event):
            return
        for key in ("active_elapsed_ms", "execution_segments"):
            if key in event:
                self.cycle[key] = copy.deepcopy(event[key])
        for name, timing in (event.get("steps") or {}).items():
            step = self._cycle_step(name)
            self._timing_fields(step, dict(timing, ttl_ms=event.get("ttl_ms", 5000)))
        self.cycle_timing_changed.emit()

    def _on_cycle_run_state(self, event, _name):
        if not self._interaction_run(event):
            return
        was_paused = self.cycle.get("paused", False)
        self.cycle["status"] = event.get("status", self.cycle.get("status"))
        self.cycle.pop("control_error", None)
        cycleresult.update(self.cycle, event)
        resumed = was_paused and not self.cycle.get("paused", False)
        received, wall = time.monotonic(), time.time()
        for step in self.cycle.get("steps", {}).values():
            step["_timing_paused"] = self.cycle.get("paused", False)
            if resumed and step.get("active_elapsed_ms") is not None:
                # Continue starts a new live interpolation interval. The last
                # receipt's clocks include the pause and cannot be reused.
                step["_timing_received"], step["_timing_wall"] = received, wall
        self.cycle_subject_changed.emit()
        self.cycle_timing_changed.emit()

    def _on_cycle_phase(self, event, _name):
        if not self._interaction_run(event):
            return
        cycleresult.update(self.cycle, event)
        receipt = event.get("receipt") or {}
        if receipt.get("phase"):
            self.cycle.setdefault("phase_receipts", {})[receipt["phase"]] = copy.deepcopy(receipt)
        self.cycle_subject_changed.emit()
        self.cycle_timing_changed.emit()

    def _on_cycle_control_refused(self, event, _name):
        if self._interaction_run(event):
            self.cycle["control_error"] = event.get("reason", "Control action was refused.")
            self.cycle_timing_changed.emit()

    def _on_cycle_node_approval(self, event, _name):
        if not self._interaction_run(event):
            return
        receipt = event.get("receipt") or {}
        if receipt.get("id"):
            self.cycle.setdefault("node_approvals", {})[receipt["id"]] = copy.deepcopy(receipt)
        cycleresult.update(self.cycle, event)
        self.cycle_subject_changed.emit()
        self.cycle_timing_changed.emit()

    def _on_cycle_policy_changed(self, event, _name):
        if not self._interaction_run(event):
            return
        cycleresult.update(self.cycle, event)
        receipt = event.get("receipt") or {}
        if receipt and "policy_changes" not in event:
            self.cycle.setdefault("policy_changes", []).append(copy.deepcopy(receipt))
        self.cycle_subject_changed.emit()
        self.cycle_timing_changed.emit()

    def _on_cycle_step_progress(self, event, _name):
        step = self._cycle_step(event.get("step"))
        if step is None or step.get("status") not in ("running", "waiting"):
            return
        step["last_activity_at"] = event.get("last_activity_at") or event.get("ts") or time.time()
        step["progress"] = {key: event[key] for key in
                            ("title", "current", "total", "unit", "heartbeat", "state") if key in event}
        self.cycle_step_changed.emit(event.get("step", ""))

    def _interaction_run(self, event):
        return bool(self.cycle is not None and
                    event.get("run_id", self.cycle.get("run_id")) ==
                    self.cycle.get("run_id"))

    def _on_cycle_interaction_offers(self, event, _name):
        if self._interaction_run(event):
            self.cycle["interaction_offers"] = copy.deepcopy({
                key: event.get(key) for key in ("version", "run", "steps")})
            self.cycle_interactions_changed.emit()

    def _on_cycle_interaction(self, event, _name):
        message = event.get("message") or {}
        if (self._interaction_run(event) and isinstance(message, dict)
                and message.get("id")
                and message.get("run_id") == self.cycle.get("run_id")):
            self._put_interaction(message)
            self.cycle_interactions_changed.emit()

    def _put_interaction(self, message):
        """One identity in the model and timeline, however many receipts arrive."""
        messages = self.cycle.setdefault("interactions", {})
        message_id = message["id"]
        first = message_id not in messages
        messages[message_id] = copy.deepcopy(message)
        for _step, stage in self.cycle.setdefault("stages", []):
            if stage.get("interaction_id") == message_id:
                stage["body"] = messages[message_id]
                return
        if first:
            self.cycle["stages"].append(("", {
                "kind": "interaction", "interaction_id": message_id,
                "ts": message.get("accepted_at", 0), "body": messages[message_id]}))
            del self.cycle["stages"][:-CYCLE_STAGES]

    def interaction_sending(self, request):
        """A pipe write is not an acknowledgement of durable acceptance."""
        if not self._interaction_run(request):
            return
        previous = self.cycle.get("interactions", {}).get(request.get("id"))
        if previous and previous.get("state") != "unconfirmed":
            return
        self._put_interaction(dict(request, state="sending", accepted_at=time.time(),
                                   deliveries=[]))
        self.cycle_interactions_changed.emit()
        self.changed.emit()

    def _on_cycle_interaction_refused(self, event, _name):
        if not self._interaction_run(event):
            return
        message = self.cycle.get("interactions", {}).get(event.get("id"))
        if message is not None:
            # A conflicting retry must not overwrite an accepted original.
            if message.get("state") == "sending":
                self._put_interaction(dict(message, state="rejected",
                                           reason=event.get("reason", "")))
            else:
                self._put_interaction(dict(message, request_error=event.get("reason", "")))
            self.cycle_interactions_changed.emit()

    def restore_interactions(self, messages):
        """Saved messages supersede diagnostic events, including missing events."""
        if self.cycle is None:
            return
        self.cycle["stages"] = [(step, stage) for step, stage in
                                self.cycle.get("stages", [])
                                if stage.get("kind") != "interaction"]
        self.cycle["interactions"] = {}
        for message in messages:
            if message.get("run_id") != self.cycle.get("run_id"):
                continue
            self.cycle["interactions"][message["id"]] = copy.deepcopy(message)
            self.cycle["stages"].append(("", {
                "kind": "interaction", "interaction_id": message["id"],
                "ts": message.get("accepted_at", 0), "body": copy.deepcopy(message)}))
        self.cycle["stages"].sort(key=lambda entry: entry[1].get("ts") or 0)
        del self.cycle["stages"][:-CYCLE_STAGES]

    def _stop_interaction_input(self):
        if self.cycle is None:
            return
        self.cycle["interaction_offers"] = {}
        for message in list(self.cycle.get("interactions", {}).values()):
            if message.get("state") == "sending":
                self._put_interaction(dict(message, state="unconfirmed", reason=(
                    "The run ended before acceptance was confirmed. "
                    "The saved interaction history may have more information.")))
        self.cycle_interactions_changed.emit()

    def _on_cycle_artifact(self, event, _name):
        step = self._cycle_step(event.get("step"))
        if step is None:
            return
        step["artifacts"].append({"name": event.get("name", ""),
                                  "path": event.get("path", ""),
                                  "type": event.get("type", ""),
                                  "bytes": event.get("bytes", 0)})
        self.cycle_step_changed.emit(event.get("step", ""))

    def _on_cycle_run_end(self, event, _name):
        self.cycle_running = False
        self._stop_interaction_input()
        if self.cycle is None:
            return
        self.cycle["status"] = event.get("status", "")
        self.cycle["message"] = event.get("message", "")
        self.cycle["duration_ms"] = event.get("duration_ms", 0)
        self.cycle["passed"] = event.get("passed", 0)
        self.cycle["failed"] = event.get("failed", 0)
        self.cycle["skipped"] = event.get("skipped", 0)
        cycleresult.update(self.cycle, event)
        self.exit_code = event.get("exit_code", self.exit_code)
        self.flows_finished = True
        self.cycle_subject_changed.emit()

    def cycle_interrupted(self):
        """The cycle stopped without saying so - the core died, or the app did.

        Not :meth:`reset_cycle`: what the run managed to do is still the answer
        to "what happened", and throwing it away because the process went is
        how a reader loses the very record they came back for. Only the claim
        that it is still going is withdrawn, and the steps that were mid-flight
        are marked stopped rather than left spinning forever.
        """
        was_running = self.cycle_running
        self.cycle_running = False
        self._stop_interaction_input()
        if self.cycle is None:
            return
        # QProcess.finished also follows a normal cycle.run.end. That event
        # already supplied the authoritative outcome and coverage; recomputing
        # a legacy fallback here would discard milestones such as Task prepared.
        if not was_running or self.cycle.get("paused"):
            self.changed.emit()
            return
        if self.cycle.get("status") in ("pending", "running", "waiting"):
            self.cycle["status"] = "interrupted"
        for step in (self.cycle.get("steps") or {}).values():
            if step.get("status") in ("running", "waiting"):
                step["status"] = "cancelled"
                step["interrupted"] = True
        cycleresult.update(self.cycle)
        self.changed.emit()

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

    def cycle_counts(self):
        """``(running, failed)`` for the rail: the cycle going now, or the one
        that went wrong.

        The same two the Services entry counts, and the same meaning: failed
        is a run that ended badly and stays so until the next run starts - not
        one somebody stopped, which is a decision rather than a fault. This
        window runs one cycle at a time, so each is 0 or 1.
        """
        if self.cycle_running:
            return 1, 0
        status = (self.cycle or {}).get("status")
        return 0, int(status in CYCLE_FAILED)

    def totals(self):
        done = sum(s["done"] for s in self.sessions.values())
        total = sum(s["total"] for s in self.sessions.values())
        return done, total
