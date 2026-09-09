"""Doing what a scenario asked of a service, and telling it what happened.

A running scenario cannot start its own backend: the launcher is a different
process, and an attached service is a child of *this* one. So the engine asks -
a ``service.request`` line on the ``--events`` stream - and this turns that into
supervisor calls and one ``service.result`` back down ``--control``. The engine
side of the same pipe is :mod:`engine.services`; between them they are the whole
feature, and neither imports the other.

**Exactly one reply per request**, from whichever path gets there first: the
service reached the state asked for, it went FAILED instead, or the deadline
passed. A request that got no reply is a step blocked for its whole timeout, so
every exit from a pending request goes through :meth:`_reply`.

The one thing here that is not bookkeeping is the **run buffer**. ``wait_for_out``
is a regex over what the service has printed, and the honest reading of that is
what it has printed *this* run - the rule ``ServiceProcess._reset_criteria``
already applies to the configured criteria ("only what this run writes"). Its
console deque is not cleared when it starts, so feeding a matcher from that would
let the previous boot's ready line satisfy a wait that has just restarted the
service. Instead this keeps its own buffer per service, emptied the moment the
service enters STARTING. That also covers the other half of the race: a service
that prints its ready line in the moments between ``service_restart`` and the
``wait_for_out`` step has still printed it into the buffer, so the wait is
satisfied rather than hanging on a line it just missed.

The criteria need the same rule and cannot keep it themselves. They are cleared
by ``ServiceProcess.start()``, which for a service that is *already running*
happens only once the old process is down - so between ``service_restart`` and
the step behind it they still describe the boot being killed. Reading them there
answers a wait in microseconds with the previous run's answer, which is how a
scenario walks on into a backend that is still going down. So an op that begins a
new run marks the service in :attr:`ServiceBridge._awaiting_run` until STARTING
says the clearing has happened, and while that mark is on, nothing is answered
from the criteria or from the status - except the failure that means the run is
not coming at all, which ends the wait rather than making it sit out its whole
timeout. A service that was *down* is not marked at all: ``start()`` runs inside
the request and the criteria are already this run's by the time it is answered.
"""

import collections

from PySide6.QtCore import QObject, QTimer

from . import criteria as criteria_mod
from .runnertypes import FAILED, RUNNING, STARTING, STOPPED, STOPPING

#: Lines kept per service since it last started. The same order of magnitude as
#: the console's own deque: enough for any ready line, bounded for a service that
#: never stops talking.
BUFFER_LINES = 2000

#: Statuses after which what the service said before no longer describes what is
#: running now. STARTING is the obvious one; the other two matter because of when
#: a restart is answered. ``service_restart`` returns as soon as the supervisor
#: has taken the job, which is while the old process is still going down - so the
#: ``wait_for_out`` after it can arrive before STARTING ever does. Clearing only
#: on STARTING would hand that wait the *previous* boot's ready line and call the
#: restart finished before it had begun.
ENDS_A_RUN = (STARTING, STOPPING, STOPPED)

#: Ops that ask for something to happen and are answered as soon as it is asked.
IMPERATIVE = ("service_start", "service_stop", "service_restart")

#: The ones that ask for a *new run*. What the service has already said stops
#: describing it the moment one of these is accepted, however long the old
#: process then takes to go down.
STARTS_A_RUN = ("service_start", "service_restart")


class _Pending:
    """One request being waited on, and the timer that gives up on it."""

    def __init__(self, request_id, key, op, pattern, matcher=None):
        self.id = request_id
        self.key = key
        self.op = op
        self.pattern = pattern
        self.matcher = matcher
        self.timer = None


class ServiceBridge(QObject):
    """Turns ``service.request`` events into supervisor work and replies."""

    def __init__(self, supervisor, send, parent=None):
        """``send`` is the callable that writes one control command (**kwargs)."""
        super().__init__(parent)
        self._supervisor = supervisor
        self._send = send
        self._pending = {}
        self._buffers = {}
        #: Keys asked to start again whose new run has not begun yet. What the
        #: criteria and the status say about them is the previous run's.
        self._awaiting_run = set()
        supervisor.status_changed.connect(self._status_changed)
        supervisor.output.connect(self._output)
        supervisor.criteria_changed.connect(self._criteria_changed)

    # -- the request ----------------------------------------------------------
    def handle(self, event):
        """Take one ``service.request`` event. True when it was one."""
        if event.get("kind") != "service.request":
            return False
        request_id = event.get("id")
        if request_id is None:
            return False
        op = event.get("op") or ""
        service, found = self._resolve(event.get("project") or "",
                                       event.get("service") or "")
        if service is None:
            self._answer(request_id, False, "", found)   # a message, not a key
            return True

        pending = _Pending(request_id, found, op, event.get("pattern"))
        if op in IMPERATIVE:
            self._do(pending, service)
            return True
        if not self._start_wait(pending, service):
            return True
        # Only now is it worth waiting: nothing above was already true.
        self._pending[request_id] = pending
        timeout_ms = int(event.get("timeout_ms") or 0)
        if timeout_ms > 0:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda rid=request_id: self._expired(rid))
            timer.start(timeout_ms)
            pending.timer = timer
        return True

    def cancel_all(self):
        """Drop every pending request. The launcher has gone; nobody is listening."""
        for pending in list(self._pending.values()):
            self._discard(pending)
        self._pending.clear()

    def _resolve(self, project, name):
        """``(service, key)`` for a reference that names one, else ``(None, why)``."""
        if not name:
            return None, "no service named in the step"
        if project:
            service = self._supervisor.service(project, name)
            if service is None:
                return None, "no service %s/%s in services.json" % (project, name)
            return service, (project, name)
        # A bare name: unambiguous across every project, or not usable at all.
        found = [key for key in self._supervisor.keys() if key[1] == name]
        if not found:
            return None, "no service named %r in services.json" % (name,)
        if len(found) > 1:
            return None, "%r is in %d projects - write it as Project/%s" % (
                name, len(found), name)
        return self._supervisor.service(*found[0]), found[0]

    # -- doing it -------------------------------------------------------------
    def _do(self, pending, service):
        """An imperative op: ask, then say whether the asking worked."""
        project, name = pending.key
        if pending.op == "service_start":
            acted = self._supervisor.start(project, name)
        elif pending.op == "service_stop":
            acted = self._supervisor.stop(project, name)
        else:
            acted = self._supervisor.restart(project, name)
        # A supervisor call returning False means "already in that state", which
        # is not a failure of the step. A configuration it cannot run reports
        # itself synchronously as FAILED, and that is.
        status = service.status
        if acted and pending.op in STARTS_A_RUN and status != STARTING:
            # A new run is coming but has not started, so the criteria have not
            # been cleared and still hold the last one's answer. STARTING means
            # the opposite: the service was down, start() ran inside the call
            # above, and what the criteria say is already about this run.
            self._awaiting_run.add(pending.key)
        ok = status != FAILED
        self._answer(pending.id, ok, status,
                     service.detail or status if not ok else status)

    def _start_wait(self, pending, service):
        """Set a wait up. False when it is already satisfied (or already lost)."""
        project, name = pending.key
        # A restart this scenario asked for that has not begun: "already" means
        # the run being killed, whatever it says. Wait for the new one instead.
        stale = pending.key in self._awaiting_run
        if pending.op == "wait_for_service":
            if self._lost(pending, service):
                return False
            if service.status == RUNNING and not stale:
                self._answer(pending.id, True, RUNNING, "already running")
                return False
            return True

        if pending.op == "wait_for_criterion":
            state = self._supervisor.criteria_state(project, name)
            named = [row for row in state if row[0] == pending.pattern]
            if not named:
                # Nothing configured by that name: waiting out the timeout would
                # only delay the same answer. A name is a name whatever the
                # service is doing, so this one is asked first of all.
                self._answer(pending.id, False, service.status,
                             "no criterion named %r on %s" % (pending.pattern, name))
                return False
            if self._lost(pending, service):
                return False
            if named[0][2] and not stale:
                self._answer(pending.id, True, service.status,
                             "%r is already lit" % (pending.pattern,))
                return False
            return True

        if pending.op == "wait_for_out":
            if self._lost(pending, service):
                return False
            rule = criteria_mod.Rule(mode=criteria_mod.MATCH,
                                     kind=criteria_mod.REGEX,
                                     pattern=pending.pattern or "")
            pending.matcher = criteria_mod.Matcher(
                criteria_mod.CriterionRow(name="wait_for_out", rules=[rule]))
            # What it has said since it started, before anything it says next.
            pending.matcher.feed_all(self._buffers.get(pending.key, ()))
            if pending.matcher.lit():
                self._answer(pending.id, True, service.status,
                             "already printed %r" % (pending.pattern,))
                return False
            return True

        self._answer(pending.id, False, "", "unknown service op %r" % (pending.op,))
        return False

    def _lost(self, pending, service):
        """The run this wait is for is not coming. True when it said so.

        Only for a service marked as owing this scenario a new run: the restart
        it asked for ended in a failure instead, and nothing after that will
        clear the mark. Answering now beats two minutes of waiting for a start
        that has already not happened - and the message is the failure's own.
        """
        if pending.key not in self._awaiting_run or service.status != FAILED:
            return False
        self._answer(pending.id, False, FAILED, self._why(pending, "it failed"))
        return True

    # -- what the supervisor says ---------------------------------------------
    def _status_changed(self, project, name, status):
        key = (project, name)
        if status in ENDS_A_RUN:
            self._new_run(key)
        if status == STARTING:
            # The moment _reset_criteria() has run: from here what the criteria
            # and the status say is about the run that was asked for. A service
            # that goes FAILED instead keeps the mark - the run is not coming,
            # and _lost() answers for that rather than pretending otherwise.
            self._awaiting_run.discard(key)
        for pending in self._for(key):
            if status == FAILED:
                self._answer(pending.id, False, status, self._why(pending, "it failed"))
                self._drop(pending)
            elif pending.op == "wait_for_service" and status == RUNNING:
                self._answer(pending.id, True, status, "running")
                self._drop(pending)

    def _new_run(self, key):
        """Forget what the last run said - and un-see it, for anything waiting.

        A pending matcher has to be reset as well as the buffer emptied. Its
        match rules are sticky by design, so one seeded from the previous run
        and then left alone would still be holding that run's ready line.
        """
        self._buffers[key] = collections.deque(maxlen=BUFFER_LINES)
        for pending in self._for(key):
            if pending.matcher is not None:
                pending.matcher.reset()

    def _output(self, project, name, line):
        key = (project, name)
        buffer = self._buffers.get(key)
        if buffer is None:
            buffer = self._buffers[key] = collections.deque(maxlen=BUFFER_LINES)
        buffer.append(line)
        for pending in self._for(key):
            if pending.matcher is not None and pending.matcher.feed(line):
                self._answer(pending.id, True, self._status_of(key),
                             "matched %r" % (pending.pattern,))
                self._drop(pending)

    def _criteria_changed(self, project, name):
        key = (project, name)
        if key in self._awaiting_run:
            # Still the old run's log talking - a service on its way down can
            # finish a match rule with a line it prints while shutting down.
            return
        waiting = [p for p in self._for(key) if p.op == "wait_for_criterion"]
        if not waiting:
            return
        lit = {row[0] for row in self._supervisor.criteria_state(project, name)
               if row[2]}
        for pending in waiting:
            if pending.pattern in lit:
                self._answer(pending.id, True, self._status_of(key),
                             "%r is lit" % (pending.pattern,))
                self._drop(pending)

    def _expired(self, request_id):
        pending = self._pending.get(request_id)
        if pending is None:
            return
        self._answer(pending.id, False, self._status_of(pending.key),
                     self._why(pending, "it timed out"))
        self._drop(pending)

    # -- replying -------------------------------------------------------------
    def _answer(self, request_id, ok, status, message):
        self._send(command="service.result", id=request_id, ok=bool(ok),
                   status=status or "", message=str(message))

    def _why(self, pending, what):
        """Why a wait ended badly, in the words of the thing it was watching."""
        status = self._status_of(pending.key)
        if pending.op == "wait_for_criterion":
            project, name = pending.key
            for row in self._supervisor.criteria_state(project, name):
                if row[0] == pending.pattern and row[3]:
                    return "%s (%s; status %s)" % (what, "; ".join(row[3]), status)
        detail = self._last_line(pending.key)
        if detail:
            return "%s (status %s; last line: %s)" % (what, status, detail[:200])
        return "%s (status %s)" % (what, status)

    def _last_line(self, key):
        for line in reversed(self._buffers.get(key) or ()):
            if line.strip():
                return line.strip()
        return ""

    def _status_of(self, key):
        service = self._supervisor.service(*key)
        return service.status if service is not None else STOPPED

    def _for(self, key):
        return [p for p in self._pending.values() if p.key == key]

    def _drop(self, pending):
        self._discard(pending)
        self._pending.pop(pending.id, None)

    def _discard(self, pending):
        if pending.timer is not None:
            pending.timer.stop()
            pending.timer.deleteLater()
            pending.timer = None
