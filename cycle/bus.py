"""Telling the outside world what a run is doing.

The executor calls one observer at every transition and knows nothing else about
who is listening. That is the same arrangement ``engine/runner.py`` already has
with ``engine/overlay.py``'s hook protocol: the runner has one call site per
transition, and whether that reaches an in-page HUD, the JSONL event stream,
both or neither is decided once, elsewhere.

Three implementations, mirroring the three there:

* :class:`NullObserver` - the protocol itself, written out as no-ops. Both the
  definition of the interface and the default, so the executor never has to
  check for None.
* :class:`EventObserver` - puts each transition on the ``--events`` stream as a
  ``cycle.*`` line. The GUI's ``RunState`` dispatches on event kind by name, so
  these arrive there with no new plumbing at all.
* :class:`Tee` - fans one call out to several, swallowing per-observer failures.
  An observer is a diagnostic and must never be the reason a run fails.

One difference from the scenario protocol is worth knowing. There, observers are
per-session and calls arrive from one thread at a time. Here several steps run
at once, so **every method may be called from several threads concurrently**.
Nothing in this module keeps mutable state, and ``engine.events.emit`` holds its
own lock, so that is safe - but anything added here has to stay that way.
"""

import json
import threading
import time

from cycle import workspace as workspace_mod
from engine import events


class NullObserver(object):
    """The protocol, and the default. Every method is deliberately empty."""

    def run_start(self, run, graph, jobs=1):
        """The run is about to begin. ``graph`` is ``model.to_graph(cycle)``."""

    def step_start(self, step_run, attempt=1):
        """A step is starting, or starting again."""

    def step_end(self, step_run):
        """A step has reached a terminal status."""

    def step_retry(self, step_run, attempt, delay, message=""):
        """A step failed and will be tried again after ``delay`` seconds."""

    def step_waiting(self, step_run, seconds):
        """A step is not finished and asked to be come back to in ``seconds``.

        Deliberately not :meth:`step_retry`. A retry is what happens *after* a
        failure and is the executor's own doing; this is the step saying there
        is nothing wrong and nothing to do yet. A reader who cannot tell the
        two apart sees a healthy cycle as one failing over and over.
        """

    def step_skipped(self, step_run, reason):
        """A step will not run, and why - a failed dependency, a false `if:`."""

    def step_log(self, step_id, stream, lines):
        """Output from a step. ``stream`` is "out" or "err"; ``lines`` a list."""

    def step_stage(self, step_id, stage):
        """One stage of a step's inner work - what an agent is doing now."""

    def artifact(self, step_id, artifact):
        """A file a step produced."""

    def run_mode(self, run, mode):
        """How this run began, said once, right after :meth:`run_start`.

        ``mode`` is ``{mode, resume_count, source_run, kept, rerun}``: whether
        the run is fresh, a resume or a partial run, which steps it took from
        an earlier attempt, and which it will do again and why. That is what
        makes a resume legible - without it the only account of "why is it
        starting from the approval?" is the checkpoint code.
        """

    def subject(self, run):
        """The run found out what it is working on - ``run.subject``."""

    def revision(self, run, record):
        """A person asked for the plan again; ``record`` is the round."""

    def run_end(self, run):
        """The run is over, whatever way it ended."""


class EventObserver(NullObserver):
    """Mirrors every transition onto the ``--events`` JSONL stream.

    The field names are the contract with anything reading the stream, so they
    are written out here rather than assembled from a record's ``asdict`` -
    renaming a dataclass field should not silently change the wire format.

    ``sink`` is where a line goes, and it is injectable for exactly one reason:
    :class:`JsonlObserver` writes the same events to the run's own directory.
    One implementation of the payloads means the file and the stream can never
    drift, which is what makes the file replayable by whatever reads the
    stream.
    """

    def __init__(self, run_id="", sink=None):
        self._run_id = run_id
        self._sink = sink or events.emit

    def _emit(self, kind, **fields):
        self._sink(kind, run_id=self._run_id, **fields)

    def run_start(self, run, graph, jobs=1):
        self._run_id = run.id
        # The graph goes out whole, and it is the same payload --cycle-show
        # returns, so a front-end draws identical nodes whether a file was
        # opened or a run was started. There is no second model to drift.
        self._emit("cycle.run.start", cycle=run.cycle_id, name=run.cycle_name,
                   workspace=run.workspace, trigger=run.trigger, jobs=jobs,
                   variables=dict(run.variables or {}), graph=graph)

    def step_start(self, step_run, attempt=1):
        self._emit("cycle.step.start", step=step_run.step_id,
                   plugin=step_run.plugin, attempt=attempt)

    def step_end(self, step_run):
        self._emit("cycle.step.end", step=step_run.step_id,
                   plugin=step_run.plugin, status=step_run.status,
                   duration_ms=round(step_run.duration_ms, 1),
                   attempts=step_run.attempts, message=step_run.message,
                   outputs=dict(step_run.outputs or {}))

    def step_retry(self, step_run, attempt, delay, message=""):
        self._emit("cycle.step.retry", step=step_run.step_id, attempt=attempt,
                   delay_ms=int(delay * 1000), message=message)

    def step_waiting(self, step_run, seconds):
        self._emit("cycle.step.waiting", step=step_run.step_id,
                   plugin=step_run.plugin, status=step_run.status,
                   resume_in_ms=int(seconds * 1000),
                   message=step_run.message,
                   outputs=dict(step_run.outputs or {}))

    def step_skipped(self, step_run, reason):
        self._emit("cycle.step.skipped", step=step_run.step_id,
                   status=step_run.status, reason=reason)

    def step_log(self, step_id, stream, lines):
        # Batched by the producer: one event per line would serialise every
        # other step behind this one on the stream's single lock.
        self._emit("cycle.step.log", step=step_id, stream=stream,
                   lines=list(lines))

    def step_stage(self, step_id, stage):
        # "phase", not "kind": every event on this stream already has a "kind"
        # and it is the event's own type, so a stage's kind sent under that
        # name would be overwritten by "cycle.step.stage" and never arrive.
        self._emit("cycle.step.stage", step=step_id,
                   phase=stage.get("kind", ""), title=stage.get("title", ""),
                   detail=stage.get("detail", ""),
                   status=stage.get("status", "done"),
                   body=stage.get("body") or {})

    def artifact(self, step_id, artifact):
        self._emit("cycle.artifact", step=step_id, type=artifact.type,
                   name=artifact.name, path=artifact.path, bytes=artifact.bytes)

    def run_mode(self, run, mode):
        self._emit("cycle.run.mode", mode=mode.get("mode", ""),
                   resume_count=mode.get("resume_count", 0),
                   source_run=mode.get("source_run", ""),
                   kept=list(mode.get("kept") or []),
                   rerun=[dict(one) for one in mode.get("rerun") or []])

    def subject(self, run):
        # Nested rather than spread: a subject has a "kind" of its own, and
        # every event already has one - it would be overwritten on the way.
        self._emit("cycle.subject", subject={
            name: (run.subject or {}).get(name, "")
            for name in ("kind", "key", "title", "memory", "pin", "step")})

    def revision(self, run, record):
        self._emit("cycle.revision", number=record.get("number", 0),
                   gate=record.get("gate", ""), target=record.get("target", ""),
                   feedback=record.get("feedback", ""), who=record.get("who", ""),
                   steps=list(record.get("steps") or []))

    def run_end(self, run):
        tally = run.tally()
        self._emit("cycle.run.end", status=run.status,
                   duration_ms=round(run.duration_ms, 1),
                   exit_code=run.exit_code, message=run.message,
                   passed=tally.get("success", 0), failed=tally.get("failed", 0),
                   skipped=tally.get("skipped", 0),
                   cancelled=tally.get("cancelled", 0),
                   timed_out=tally.get("timeout", 0))


class JsonlObserver(EventObserver):
    """The same events, into ``<workspace>/logs/cycle.jsonl``.

    Every run already leaves a directory that can be zipped and attached to a
    bug report; until now that directory held what a run *did* - the record and
    the graph - but not what it *said*. So a front-end that was not watching at
    the time had no way back to a run's stages and output: those existed only
    on a pipe, and the pipe ends with the process.

    That is the whole reason this is a file rather than a second copy of the
    record: the events are what a reader already knows how to turn back into a
    view of a run, so reading this file is replaying the run.

    Written whether or not ``--events`` was given - a cycle started from a
    terminal is as worth reading afterwards as one started from the GUI - and,
    like every other observer here, never a reason for a run to fail: a
    diagnostic that cannot be written is simply not written.
    """

    def __init__(self, workspace, run_id=""):
        super().__init__(run_id, sink=self._write)
        self._lock = threading.Lock()
        self._seq = 0
        try:
            # Line buffered, like the event stream's own file sink, so a reader
            # tailing this sees a run as it happens rather than in bursts.
            self._file = open(workspace_mod.log_path(workspace), "a",
                              encoding="utf-8", buffering=1)
        except OSError:
            self._file = None

    def _write(self, kind, **fields):
        """One line, with the envelope ``engine.events.emit`` puts on its own.

        Its own lock and its own sequence: several steps run at once, and a
        half-written line is a file a reader cannot parse past.
        """
        if self._file is None:
            return
        with self._lock:
            if self._file is None:           # closed between the check and here
                return
            self._seq += 1
            event = {"ts": round(time.time(), 3), "seq": self._seq, "kind": kind}
            event.update(fields)
            try:
                self._file.write(json.dumps(event, ensure_ascii=False,
                                            default=str) + "\n")
            except Exception:                # a full disk, an odd value
                pass

    def close(self):
        with self._lock:
            handle, self._file = self._file, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


class Tee(NullObserver):
    """Fans every call out to several observers, in order.

    One observer raising must not stop the others, and must never reach the
    executor: an observer writes to a pipe that can close, or to a file on a
    disk that can fill, and neither is a reason for a run to fail.
    """

    def __init__(self, observers):
        self._observers = [one for one in observers if one is not None]

    def _fan(self, name, *args, **kwargs):
        for observer in self._observers:
            try:
                getattr(observer, name)(*args, **kwargs)
            except Exception:                # diagnostics; never break a run
                pass

    def run_start(self, run, graph, jobs=1):
        self._fan("run_start", run, graph, jobs)

    def step_start(self, step_run, attempt=1):
        self._fan("step_start", step_run, attempt)

    def step_end(self, step_run):
        self._fan("step_end", step_run)

    def step_retry(self, step_run, attempt, delay, message=""):
        self._fan("step_retry", step_run, attempt, delay, message)

    def step_waiting(self, step_run, seconds):
        self._fan("step_waiting", step_run, seconds)

    def step_skipped(self, step_run, reason):
        self._fan("step_skipped", step_run, reason)

    def step_log(self, step_id, stream, lines):
        self._fan("step_log", step_id, stream, lines)

    def step_stage(self, step_id, stage):
        self._fan("step_stage", step_id, stage)

    def artifact(self, step_id, artifact):
        self._fan("artifact", step_id, artifact)

    def run_mode(self, run, mode):
        self._fan("run_mode", run, mode)

    def subject(self, run):
        self._fan("subject", run)

    def revision(self, run, record):
        self._fan("revision", run, record)

    def run_end(self, run):
        self._fan("run_end", run)


class Recorder(NullObserver):
    """Keeps every call, for tests and for replaying a run into a report.

    Lives here rather than in the test file because two suites want it and
    because "what did the run say" is a reasonable thing for a report plugin to
    ask as well.
    """

    def __init__(self):
        self.calls = []

    def _note(self, call, **fields):
        # `call` rather than `name`, because an artifact has a name of its own
        # and a keyword argument called that would collide with this one.
        self.calls.append((call, fields))

    def run_start(self, run, graph, jobs=1):
        self._note("run_start", run=run, graph=graph, jobs=jobs)

    def step_start(self, step_run, attempt=1):
        self._note("step_start", step=step_run.step_id, attempt=attempt)

    def step_end(self, step_run):
        self._note("step_end", step=step_run.step_id, status=step_run.status)

    def step_retry(self, step_run, attempt, delay, message=""):
        self._note("step_retry", step=step_run.step_id, attempt=attempt)

    def step_waiting(self, step_run, seconds):
        self._note("step_waiting", step=step_run.step_id, seconds=seconds,
                   message=step_run.message)

    def step_skipped(self, step_run, reason):
        self._note("step_skipped", step=step_run.step_id, reason=reason)

    def step_log(self, step_id, stream, lines):
        self._note("step_log", step=step_id, stream=stream, lines=list(lines))

    def step_stage(self, step_id, stage):
        self._note("step_stage", step=step_id, stage=dict(stage))

    def artifact(self, step_id, artifact):
        self._note("artifact", step=step_id, name=artifact.name)

    def run_mode(self, run, mode):
        self._note("run_mode", **dict(mode))

    def subject(self, run):
        self._note("subject", **dict(run.subject or {}))

    def revision(self, run, record):
        self._note("revision", number=record.get("number"),
                   steps=list(record.get("steps") or []))

    def run_end(self, run):
        self._note("run_end", status=run.status)

    def kinds(self):
        """Just the call names, in order - what most assertions actually want."""
        return [name for name, _fields in self.calls]

    def of(self, name):
        """The fields of every call of one kind."""
        return [fields for called, fields in self.calls if called == name]
