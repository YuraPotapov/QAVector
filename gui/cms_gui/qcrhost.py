"""Service actions through the QCR host (ADR-002 2.3b), behind ``qcr_services_v1``.

With the setting on, the GUI starts one ``--qcr-host`` core process for the
project in use and the Services page's Start / Stop / Restart of a service that
has a QCR identity go through it: the host checks the grants and the dependency
closure, writes the execution to the project's journal, and only then asks this
GUI - ``dispatch`` - to act. A service without an identity stays on the page's
own path, exactly as before (the user's decision: no automatic migration).

**The GUI is the trust root**, and the only party on the host's pipe: ``hello``
names the one actor this window acts as and its grants.

**The work is still the supervisor's, through the ServiceBridge.** A dispatch
becomes the same requests a scenario's service step makes - ``service_start``,
then ``wait_for_service`` - on **the main window's own bridge** (answered here,
not on the launcher's pipe), so the restart race the bridge already handles (a
restart answered before the old run is down) is one piece of knowledge shared
with every scenario that waits on the same service afterwards.

**Runs ask too** (2.4). A scenario's or a cycle's ``service_start`` / ``stop`` /
``restart`` of a service with an identity is invoked here as the run's own
actor - ``cycle:<run id>`` or ``run:<history entry>`` - which the main window
names from the run it started, never from the request line. A run's actor holds
the service rights of the person who started it, within the project, until the
run ends; what it gains is attribution, the journal, one action per service at
a time and the dependency check. Its waits stay on the bridge: they change
nothing. What comes back is reported to the host as
``owner`` messages: ``accepted`` (the supervisor took it - not success),
``running``, then ``succeeded`` or ``failed``.

**Readiness is not derived from the criteria.** Criteria never gate anything
(``criteria.py``), so a start that waits ends when the service is RUNNING. And
they have no sign: "Serving" lit is good news, "Crashed" lit is bad, and nothing
in a criterion says which - "all lit" would call every healthy service not ready.
So ``readiness`` is ``not_checked``, and the criteria are reported beside it as
what they said at that moment (``observed_at``), early in the run as it is.

**A stop ends when the process is down** - STOPPED, or FAILED on its way down;
the status says which.

When the host stops answering it is started again (a few times, then left off
until the setting is touched); the executions it had open are reconciled by the
host itself as ``unknown``, and this side drops what it was waiting for.
"""

import hashlib
import itertools
import json
import logging
import os
import secrets
import tempfile
import time

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .runnertypes import FAILED, RUNNING, STOPPED
from .servicebridge import ServiceBridge

log = logging.getLogger(__name__)

PROTOCOL = 1
GUI_ACTOR = "gui-user"
GUI_GRANTS = [{"permission": p} for p in (
    "services.read", "services.start", "services.stop", "services.restart",
    "executions.read", "executions.cancel")]
#: What a run's actor may do: the services, as the person who started it.
RUN_GRANTS = [{"permission": p} for p in (
    "services.read", "services.start", "services.stop", "services.restart")]
#: What each "QAVector access" switch of an agent connection grants (ADR-003 D6).
AGENT_GRANTS = {
    "services": ("services.read", "services.start", "services.stop", "services.restart"),
    "scenarios": ("scenarios.read", "scenarios.run"),
    "cycles": ("cycles.read", "cycles.run"),
    "approvals": ("cycles.read", "cycles.approve"),
    "jira": ("integrations.jira.read", "integrations.jira.issue", "integrations.jira.comments",
             "integrations.jira.search", "integrations.jira.statuses"),
}
#: How long one turn's token is good for at most; it is revoked when the turn ends.
AGENT_TOKEN_SECONDS = 12 * 3600
#: Executions whose latest state is remembered, for a watcher that arrives late.
REMEMBERED = 500
#: Restarts in a row after the host ended by itself, before it is left off.
RESTARTS = 3
SNAPSHOT_DEBOUNCE_MS = 150
STOP_WAIT_MS = 3000
TERMINAL = ("succeeded", "failed", "timed_out", "canceled", "unknown")

OFF, STARTING, READY, DOWN = "off", "starting", "ready", "down"
CRITERION_COLORS = ("green", "amber", "red", "blue", "grey")


def store_path(data_dir):
    """The project's execution journal: ``<project folder>/.qcr/executions.sqlite``."""
    return os.path.join(data_dir, ".qcr", "executions.sqlite")


class QcrHost(QObject):
    #: Every execution the host reports a change of.
    execution_changed = Signal(dict)
    #: An execution this window asked for ended any other way than succeeded.
    finished_badly = Signal(dict)
    #: off / starting / ready / down - down is "gave up restarting".
    state_changed = Signal(str)

    def __init__(self, supervisor, parent=None, bridge=None):
        """``bridge``: the main window's ServiceBridge, shared with scenarios;
        without one (tests) a bridge of its own answers nowhere else."""
        super().__init__(parent)
        self._supervisor = supervisor
        self._proc = None
        self._argv, self._project_id = None, None
        self._buffer = ""
        self._ids = itertools.count(1)
        self._replies = {}         # request id -> callback(reply)
        self._queue = []           # invokes waiting for "ready"
        self._restarts = 0
        self._stopping = False
        self.state = OFF
        self.problem = ""
        #: The host's agent socket, from ``ready``; "" without one.
        self.socket_path = ""
        self._bridge = bridge if bridge is not None else ServiceBridge(supervisor, lambda **_: None, self)
        self._bridge_ids = itertools.count(1)
        self._run_actors = set()   # actors of runs granted so far, re-granted to a relaunched host
        self._finishers = {}       # execution id -> callback(execution) at its end
        self._last = {}            # execution id -> latest execution seen (bounded)
        self._inventory = None     # scenarios/environments/accounts, as the host needs them
        self._handlers = {}        # dispatch op -> handler(execution_id, resource_id, input)
        self._readers = {}         # owner_read op -> reader(message, reply)
        self._waits = {}           # bridge request id -> (execution id, callback)
        self._jobs = {}            # execution id -> {"key", "op", "input", "waits", "watch"}
        self._snapshot_timer = QTimer(self)
        self._snapshot_timer.setSingleShot(True)
        self._snapshot_timer.setInterval(SNAPSHOT_DEBOUNCE_MS)
        self._snapshot_timer.timeout.connect(self._push_snapshot)
        supervisor.status_changed.connect(self._status_changed)
        # A criterion lit or went out: what an agent waiting on it reads is the next snapshot.
        supervisor.criteria_changed.connect(self.schedule_snapshot)

    # -- lifecycle --------------------------------------------------------------
    def start(self, argv, project_id):
        """Run the host for ``project_id``; a running one for another project is
        stopped first. The same arguments again change nothing."""
        if self._proc is not None and argv == self._argv:
            return
        self.stop()
        # What is known of another project is not this one's: its inventory would be
        # refused by the new host, and what was open in its cycle run is not open
        # here. The inventory of THIS project - loaded before QCR was switched on -
        # is kept: it is exactly what the new host needs.
        if self._inventory is not None and self._inventory.get("project_id") != project_id:
            self._inventory = None
        if self._project_id is not None and project_id != self._project_id:
            self._pending_sent = None
        self._argv, self._project_id = list(argv), project_id
        self._restarts = 0
        self._launch()

    def _launch(self):
        self._stopping = False
        self._buffer = ""
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._read)
        proc.readyReadStandardError.connect(self._read_errors)
        proc.finished.connect(lambda code, _status, p=proc: self._ended(p, code))
        proc.errorOccurred.connect(lambda error, p=proc: self._failed_to_start(p, error))
        proc.started.connect(lambda p=proc: self._greet(p))
        self._proc = proc
        self._set_state(STARTING)
        proc.start(self._argv[0], self._argv[1:])

    def stop(self):
        """Shut the host down: what it was asked is in its journal; anything
        this window was still waiting for is dropped."""
        proc, self._proc = self._proc, None
        self._stopping = True
        self._abandon("The QCR host was stopped.")
        if proc is not None and proc.state() != QProcess.NotRunning:
            try:
                proc.write(b'{"type":"shutdown"}\n')
                proc.closeWriteChannel()
                if not proc.waitForFinished(STOP_WAIT_MS):
                    proc.kill()
                    proc.waitForFinished(STOP_WAIT_MS)
            except RuntimeError:
                pass
        self._set_state(OFF)

    def _ended(self, proc, code):
        if proc is not self._proc:
            return
        self._proc = None
        log.warning("QCR host ended (exit %s)", code)
        self._abandon("The QCR host stopped unexpectedly.")
        if self._stopping:
            self._set_state(OFF)
            return
        if self._restarts < RESTARTS:
            self._restarts += 1
            QTimer.singleShot(500 * self._restarts, self._relaunch)
            self._set_state(STARTING)
        else:
            self.problem = "The QCR host keeps stopping; service actions are unavailable until the setting is changed."
            self._set_state(DOWN)

    def _greet(self, proc):
        """What the host needs before anything else: the services as they are
        now, then who this window is and what it may do."""
        if proc is not self._proc:
            return
        self._push_snapshot()
        self._push_inventory()
        if getattr(self, "_pending_sent", None) is not None:
            self._send({"type": "pending", "pending": self._pending_sent})
        actors = {GUI_ACTOR: GUI_GRANTS}
        actors.update({actor: RUN_GRANTS for actor in self._run_actors})
        self._send({"type": "hello", "protocol": PROTOCOL, "actors": actors})

    def _relaunch(self):
        if self._proc is None and not self._stopping and self._argv:
            self._launch()

    def _failed_to_start(self, proc, error):
        if error == QProcess.FailedToStart and proc is self._proc:
            self._proc = None
            self.problem = "The QCR host could not be started."
            self._abandon(self.problem)
            self._set_state(DOWN)

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            self.state_changed.emit(state)

    def _abandon(self, why):
        for request_id in list(self._replies):
            self._reply_failed(request_id, "host_unavailable", why)
        for message in self._queue:
            if message.get("type") == "invoke":
                self._reply_failed(message["id"], "host_unavailable", why)
        self._queue = []
        for execution_id, callback in list(self._finishers.items()):
            self._finishers.pop(execution_id, None)
            callback({"execution_id": execution_id, "state": "unknown",
                      "error": {"code": "host_unavailable", "message": why}})
        for execution_id in list(self._jobs):
            self._forget(execution_id)

    # -- the pipe -----------------------------------------------------------------
    def _send(self, message):
        if self._proc is None:
            return False
        self._proc.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        return True

    def _read(self):
        proc = self.sender()
        if proc is not self._proc:
            return
        self._buffer += bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                log.warning("QCR host said something that is not JSON")
                continue
            if isinstance(message, dict):
                self._take(message)

    def _read_errors(self):
        proc = self.sender()
        text = bytes(proc.readAllStandardError()).decode("utf-8", "replace").strip()
        if text:
            log.info("qcr-host: %s", text[-2000:])

    def _take(self, message):
        kind = message.get("type")
        if kind == "ready":
            self.socket_path = message.get("socket") or ""
            self._restarts = 0
            self.problem = ""
            self._set_state(READY)
            # What waited for "ready" is checked against the services as they are now,
            # not as they were when the host was greeted (the page may have loaded since).
            self._push_snapshot()
            self._push_inventory()
            queued, self._queue = self._queue, []
            for one in queued:
                self._send(one)
        elif kind == "reply":
            callback = self._replies.pop(message.get("id"), None)
            if callback is not None:
                callback(message)
        elif kind == "dispatch":
            self._dispatch(message)
        elif kind == "owner_read":
            self._owner_read(message)
        elif kind == "stop_waiting":
            self._stop_waiting(message.get("execution_id"))
        elif kind == "execution":
            execution = message.get("execution") or {}
            self._remember(execution)
            self.execution_changed.emit(execution)
            if execution.get("actor_id") == GUI_ACTOR and execution.get("state") in TERMINAL \
                    and execution.get("state") != "succeeded":
                self.finished_badly.emit(execution)
        elif kind == "error":
            log.warning("QCR host refused a line: %s", (message.get("error") or {}).get("code"))

    def _owner_read(self, message):
        """What only this window holds: a service's console - the last lines, at
        most 200 - or what a reader set with :meth:`set_reader` answers."""
        request_id = message.get("id")

        def reply(result=None, error=None):
            self._send(dict({"type": "owner_read_reply", "id": request_id},
                            **({"error": error} if error else {"result": result})))
        reader = self._readers.get(message.get("op"))
        if reader is not None:
            try:
                reader(message, reply)
            except Exception as exc:
                log.exception("QCR reader for %s failed", message.get("op"))
                reply(error={"code": "owner_failed", "message": str(exc)[:300]})
            return
        key = self._key_of(message.get("resource_id"))
        service = self._supervisor.service(*key) if key is not None else None
        if message.get("op") != "service.output" or service is None:
            reply(error={"code": "not_found", "message": "No such service here."})
            return
        count = max(1, min(int(message.get("lines") or 50), 200))
        lines = [str(line)[:4000] for line in service.console()[-count:]]
        reply({"lines": lines, "service_status": _status(service.status)})

    def set_reader(self, op, reader):
        """Who answers an ``owner_read`` of ``op`` (the main window, for the
        launcher's status and output): ``reader(message, reply)`` calls
        ``reply(result)`` or ``reply(error={...})`` once - now, or later for a
        read that waits."""
        self._readers[op] = reader

    def _push_snapshot(self):
        if self._proc is None or not self._project_id:
            return
        try:
            snapshot = self._supervisor.qcr_snapshot(self._project_id)
        except ValueError as exc:
            # Duplicate or malformed identities: nothing is routed until they are fixed.
            self.problem = str(exc)
            return
        self._send({"type": "snapshot", "snapshot": snapshot})

    def _remember(self, execution):
        execution_id = execution.get("execution_id")
        if not execution_id:
            return
        self._last.pop(execution_id, None)
        self._last[execution_id] = execution
        while len(self._last) > REMEMBERED:
            self._last.pop(next(iter(self._last)))
        if execution.get("state") in TERMINAL:
            callback = self._finishers.pop(execution_id, None)
            if callback is not None:
                callback(execution)

    def grant_run(self, actor):
        """Let a run's actor act on the project's services until :meth:`release_runs`."""
        if actor in self._run_actors:
            return
        self._run_actors.add(actor)
        self._send_when_ready({"type": "actor", "id": next(self._ids), "actor": actor, "grants": RUN_GRANTS})

    def release_runs(self):
        """The run is over: its actors may do nothing more (what they did stays in the journal)."""
        for actor in sorted(self._run_actors):
            self._send_when_ready({"type": "revoke", "id": next(self._ids), "actor": actor})
        self._run_actors.clear()

    def _send_when_ready(self, message):
        if self.state == READY and self._proc is not None:
            self._send(message)
        elif self.state == STARTING:
            self._queue.append(message)

    def set_inventory(self, inventory):
        """What the core's inventory says - scenarios, environments, accounts -
        in the host's ``inventory`` shape (``qcr.adapters.scenarios``)."""
        self._inventory = inventory
        self._push_inventory()

    def _push_inventory(self):
        # Never another project's: the host would refuse it (source_unavailable).
        if self._proc is not None and self._inventory is not None \
                and self._inventory.get("project_id") == self._project_id:
            self._send({"type": "inventory", "inventory": self._inventory})

    def set_handler(self, op, handler):
        """Who carries out a dispatched ``op`` that is not a service's - the main
        window, for ``scenario.run``. ``handler(execution_id, resource_id, input)``
        answers through :meth:`owner` and may register :meth:`on_stop`."""
        self._handlers[op] = handler

    def owner(self, execution_id, event, **fields):
        """A handler's report on what it was dispatched (accepted, running, ...)."""
        self._owner(execution_id, event, **fields)

    def actor_of(self, execution_id):
        """Who asked for a dispatched execution - the host's word (an approval writes it down)."""
        return (self._jobs.get(execution_id) or {}).get("actor", "")

    def set_pending(self, pending):
        """What is open in the cycle run on screen, for ``cycles.<id>.pending``;
        sent only when it changed."""
        if pending == getattr(self, "_pending_sent", object()):
            return
        self._pending_sent = pending
        if self._proc is not None:
            self._send({"type": "pending", "pending": pending})

    def on_stop(self, execution_id, stop):
        """``stop()`` runs if the host ends the execution first (cancel, deadline)."""
        job = self._jobs.get(execution_id)
        if job is not None:
            job["stop"] = stop

    def grant_agent(self, connection, session_id):
        """QAVector for one turn of ``connection``'s agent (ADR-003 D4/D6), or None.

        None when the host is not ready, has no socket, or the connection's
        "QAVector access" grants nothing. Otherwise the actor is issued its
        grants, a fresh token's digest is registered, and the token itself is
        written to a 0600 file beside the socket - the turn gets its *path*.
        """
        access = (connection or {}).get("qavector_access") or {}
        permissions = sorted({p for key, on in access.items() if on is True for p in AGENT_GRANTS.get(key, ())})
        if self.state != READY or self._proc is None or not self.socket_path or not permissions:
            return None
        connection_id = str((connection or {}).get("id") or "agent")
        actor = "agent:%s/%s" % (connection_id[:64], str(session_id)[:64])
        try:
            folder = os.path.join(os.path.dirname(self.socket_path), "tokens")
            os.makedirs(folder, mode=0o700, exist_ok=True)
            os.chmod(folder, 0o700)
            token = secrets.token_hex(32)
            descriptor, token_file = tempfile.mkstemp(prefix="t-", dir=folder)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(token)
            os.chmod(token_file, 0o600)
        except OSError as exc:
            log.warning("QAVector access for an agent turn not given: %s", exc)
            return None
        self._send({"type": "actor", "id": next(self._ids), "actor": actor,
                    "grants": [{"permission": p} for p in permissions]})
        self._send({"type": "agent_token", "id": next(self._ids), "actor": actor,
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "expires_at": time.time() + AGENT_TOKEN_SECONDS})
        return {"actor": actor, "socket": self.socket_path, "token_file": token_file}

    def end_agent(self, access):
        """The turn is over: its actor may do nothing more, and its token file goes."""
        if not access:
            return
        if self._proc is not None:
            self._send({"type": "revoke", "id": next(self._ids), "actor": access["actor"]})
        try:
            os.unlink(access["token_file"])
        except OSError:
            pass

    def schedule_snapshot(self, *_):
        if self._proc is not None:
            self._snapshot_timer.start()

    # -- asking the host ------------------------------------------------------------
    def handles(self, project, name):
        """Whether this service's actions go through the host: the host is on and
        the service has a QCR identity."""
        if self.state == OFF:
            return False
        service = self._supervisor.service(project, name)
        return service is not None and bool(service.runner.extra.get("qcr_id"))

    def invoke(self, op, project, name, done=None, request=None, actor=GUI_ACTOR, finished=None):
        """Ask the host for ``services.<id>.<op>`` as ``actor``.

        ``done(reply)`` gets the host's answer to the asking; ``finished(execution)``
        the execution once it has ended (also when the host goes away first).
        """
        service = self._supervisor.service(project, name)
        resource_id = service.runner.extra.get("qcr_id") if service is not None else None
        request_id = next(self._ids)
        message = {"type": "invoke", "id": request_id, "actor": actor,
                   "capability": "services.%s.%s" % (resource_id, op), "input": dict(request or {})}

        def answered(reply):
            if finished is not None:
                if reply.get("ok"):
                    execution = reply.get("execution") or {}
                    execution_id = execution.get("execution_id")
                    seen = self._last.get(execution_id) or execution
                    if seen.get("state") in TERMINAL:
                        finished(seen)
                    else:
                        self._finishers[execution_id] = finished
            if done is not None:
                done(reply)
        self._replies[request_id] = answered
        if not resource_id:
            # Not the host's fault: this service (or the page's list) has no identity for it.
            self._reply_failed(request_id, "not_routed", "This service has no QCR identity.")
        elif self.state in (DOWN, OFF):
            self._reply_failed(request_id, "host_unavailable", self.problem or "The QCR host is not running.")
        elif self.state == READY and self._proc is not None:
            self._push_snapshot()           # what the host checks is what is on screen now
            self._send(message)
        else:
            self._queue.append(message)
        return request_id

    def ask(self, kind, done, **fields):
        """One read-or-cancel request as this window's user - ``list``, ``get``,
        ``events``, ``cancel`` - answered by ``done(reply)``; refused at once
        when the host is not ready."""
        request_id = next(self._ids)
        self._replies[request_id] = done
        if self.state != READY or self._proc is None:
            self._reply_failed(request_id, "host_unavailable", self.problem or "The QCR host is not running.")
            return request_id
        self._send(dict(fields, type=kind, id=request_id, actor=GUI_ACTOR))
        return request_id

    def _reply_failed(self, request_id, code, why):
        callback = self._replies.pop(request_id, None)
        if callback is not None:
            callback({"type": "reply", "id": request_id, "ok": False, "error": {"code": code, "message": why}})

    # -- doing what the host dispatched ------------------------------------------------
    def _key_of(self, resource_id):
        for key in self._supervisor.keys():
            service = self._supervisor.service(*key)
            if service is not None and service.runner.extra.get("qcr_id") == resource_id:
                return key
        return None

    def _owner(self, execution_id, event, **fields):
        self._send(dict({"type": "owner", "execution_id": execution_id, "event": event}, **fields))
        if event in ("succeeded", "failed"):
            self._forget(execution_id, tell_bridge=True)

    def _dispatch(self, message):
        execution_id, op = message.get("execution_id"), message.get("op")
        handler = self._handlers.get(op)
        if handler is not None:
            self._jobs[execution_id] = {"key": None, "op": op, "input": message.get("input") or {},
                                        "waits": set(), "watch": False, "stop": None,
                                        "actor": str(message.get("actor") or "")}
            try:
                handler(execution_id, message.get("resource_id"), message.get("input") or {})
            except Exception as exc:
                log.exception("QCR handler for %s failed", op)
                self._owner(execution_id, "failed", error={"code": "owner_failed", "message": str(exc)[:300]})
            return
        key = self._key_of(message.get("resource_id"))
        if key is None:
            self._send({"type": "owner", "execution_id": execution_id, "event": "failed",
                        "error": {"code": "not_found", "message": "The service is not on this page any more."}})
            return
        self._jobs[execution_id] = {"key": key, "op": op, "input": message.get("input") or {},
                                    "waits": set(), "watch": False, "stop": None}
        imperative = {"start": "service_start", "restart": "service_restart", "stop": "service_stop"}.get(op)
        if imperative is None:
            self._owner(execution_id, "failed", error={"code": "unsupported", "message": "Unknown operation."})
            return
        self._ask(execution_id, imperative, key,
                  self._stopped_asked if op == "stop" else self._started_asked)

    def _ask(self, execution_id, op, key, callback):
        # A string: never one of the launcher's own (integer) request ids.
        request_id = "qcr-%d" % next(self._bridge_ids)
        self._waits[request_id] = (execution_id, callback)
        self._jobs[execution_id]["waits"].add(request_id)
        # timeout_ms 0: the host owns the deadline and says stop_waiting.
        self._bridge.handle({"kind": "service.request", "id": request_id, "op": op,
                             "project": key[0], "service": key[1], "timeout_ms": 0},
                            reply=self._bridge_answer)

    def _bridge_answer(self, command=None, id=None, ok=False, status="", message="", **_):  # noqa: A002
        entry = self._waits.pop(id, None)
        if entry is None:
            return
        execution_id, callback = entry
        job = self._jobs.get(execution_id)
        if job is None:
            return
        job["waits"].discard(id)
        callback(execution_id, bool(ok), status, message)

    def _started_asked(self, execution_id, ok, status, message):
        if not ok:
            self._owner(execution_id, "failed", error={"code": "service_failed", "message": message[:500]},
                        output=self._start_output(execution_id, status or FAILED))
            return
        self._owner(execution_id, "accepted", payload={"service_status": status})
        job = self._jobs.get(execution_id)
        if job is None:
            return
        if job["input"].get("wait_ready") is False:
            self._owner(execution_id, "succeeded", output=self._start_output(execution_id, status))
            return
        self._owner(execution_id, "running")
        self._ask(execution_id, "wait_for_service", job["key"], self._ready_asked)

    def _ready_asked(self, execution_id, ok, status, message):
        if ok:
            self._owner(execution_id, "succeeded", output=self._start_output(execution_id, status or RUNNING))
        else:
            self._owner(execution_id, "failed", error={"code": "service_failed", "message": message[:500]},
                        output=self._start_output(execution_id, status or FAILED))

    def _start_output(self, execution_id, status):
        job = self._jobs.get(execution_id) or {}
        criteria = [{"name": str(name)[:256], "color": colour if colour in CRITERION_COLORS else "green",
                     "met": bool(lit)}
                    for name, colour, lit, _why in self._supervisor.criteria_state(*job["key"])][:100] \
            if job.get("key") else []
        # Not waited for here (a migration can take minutes): said, with their colours
        # (green = ready, red = failed), and the next step named outright - whoever
        # asked need not remember which reads exist: criteria to wait on, else output.
        service = self._supervisor.service(*job["key"]) if job.get("key") else None
        resource_id = service.runner.extra.get("qcr_id") if service is not None else ""
        output = {"service_status": _status(status), "readiness": "not_checked", "criteria": criteria,
                  "has_criteria": bool(criteria), "observed_at": time.time()}
        if resource_id:
            output["next"] = "services.%s.%s" % (resource_id, "criteria" if criteria else "output")
        return output

    def _stopped_asked(self, execution_id, ok, status, message):
        if not ok:
            self._owner(execution_id, "failed", error={"code": "service_failed", "message": message[:500]},
                        output={"service_status": _status(status or FAILED), "observed_at": time.time()})
            return
        self._owner(execution_id, "accepted", payload={"service_status": status})
        job = self._jobs.get(execution_id)
        if job is None:
            return
        service = self._supervisor.service(*job["key"])
        now = service.status if service is not None else STOPPED
        if now in (STOPPED, FAILED):
            self._owner(execution_id, "succeeded", output={"service_status": _status(now), "observed_at": time.time()})
        else:
            job["watch"] = True
            self._owner(execution_id, "running")

    def _status_changed(self, project, name, status):
        self.schedule_snapshot()
        if status not in (STOPPED, FAILED):
            return
        for execution_id, job in list(self._jobs.items()):
            if job["watch"] and job["key"] == (project, name):
                self._owner(execution_id, "succeeded",
                            output={"service_status": _status(status), "observed_at": time.time()})

    def _stop_waiting(self, execution_id):
        """The host ended the execution (cancel, deadline, no answer): drop the
        waits, and stop what a handler said to stop - a run, not a service. Only
        here: a run that ends by itself, or a host that dies, stops nothing."""
        job = self._jobs.get(execution_id)
        stop = job.get("stop") if job is not None else None
        self._forget(execution_id)
        if stop is not None:
            try:
                stop()
            except Exception:
                log.exception("stopping %s failed", execution_id)

    def _forget(self, execution_id, tell_bridge=True):
        job = self._jobs.pop(execution_id, None)
        if job is None:
            return
        for request_id in job["waits"]:
            self._waits.pop(request_id, None)
            if tell_bridge:
                self._bridge.cancel(request_id)


def _status(status):
    """The supervisor's status in QCR's words (its enum has no other spelling)."""
    allowed = ("stopped", "waiting", "starting", "running", "stopping", "failed", "unknown")
    return status if status in allowed else "unknown"


#: What a QCR refusal means, in the page's words. Anything else shows its own message.
WORDS = {
    "busy": "another action on this service is still running",
    "dependency_unmigrated": "a service it depends on has no QCR identity, so QCR cannot check it",
    "authorization_denied": "not permitted",
    "host_unavailable": "the QCR host is not running",
    "not_routed": "this service has no QCR identity",
    "owner_unavailable": "QAVector's window did not take the job in time",
    "launcher_busy": "another run is using QAVector's launcher",
}


def words(error):
    error = error or {}
    return WORDS.get(error.get("code"), error.get("message") or error.get("code") or "failed")
