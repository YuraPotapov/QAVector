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
    # Each change waits for the person's Approve (ADR-007).
    "jira_write": ("integrations.jira.write", "integrations.jira.comment", "integrations.jira.transition",
                   "integrations.jira.assign", "integrations.jira.edit", "integrations.jira.comment_edit",
                   "integrations.jira.create", "integrations.jira.attach"),
    # Each channel says how far (ADR-015 D5): Off hides it, Ask waits for Approve.
    "teams": ("integrations.teams.read", "integrations.teams.describe", "integrations.teams.preview",
              "integrations.teams.history", "integrations.teams.post_of", "integrations.teams.write",
              "integrations.teams.post"),
    "usage": ("analytics.read",),
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
#: An integration change that waits for nobody any more, and how many such are kept.
INTEGRATION_ENDED = TERMINAL + ("denied",)
INTEGRATION_KEPT = 50

OFF, STARTING, READY, DOWN = "off", "starting", "ready", "down"
#: How a conversation goes on when the tasks it handed on end (ADR-009 D3).
WAKES = ("continue", "ask", "off")
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
    #: A delegation or a message waits for the person (ADR-006 D3): the host's card.
    agent_approval = Signal(dict)
    #: An agent's change to an integration (a Jira comment, a transition) arrived,
    #: was answered or ended: :attr:`integration_changes` is different (ADR-007).
    integration_changes_changed = Signal()
    #: What a conversation handed on to other agents has ended, as it asked
    #: to be woken for (ADR-009): ``{"session", "condition", "items", "running", "failed"}``.
    round_finished = Signal(dict)
    #: The agent read one of those results itself before its wake was delivered:
    #: ``{"session", "execution_id"}``.
    round_seen = Signal(dict)
    #: A turn waits at a budget for the person's word (ADR-013 D3).
    budget_reached = Signal(dict)
    #: An agent read an issue's details: its conversation is linked to it (ADR-014).
    conversation_linked = Signal(dict)

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
        self._agent_storage = None  # (sessions root, agents.json): delegation targets (ADR-006)
        #: execution id -> one agent change to an integration and how it stands
        #: (ADR-007 D7): the chat and the Integrations page both show these.
        self.integration_changes = {}
        self._agent_names = {}     # agent connection id -> its name, from the turns granted
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
        self._integration_host_gone()
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
            self._push_agent_storage()
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
            self._integration_execution(execution)
            self.execution_changed.emit(execution)
            if execution.get("actor_id") == GUI_ACTOR and execution.get("state") in TERMINAL \
                    and execution.get("state") != "succeeded":
                self.finished_badly.emit(execution)
        elif kind == "agent_approval":
            self.agent_approval.emit(dict(message))
        elif kind == "integration_approval":
            self._integration_waits(message)
        elif kind == "round_finished":
            self.round_finished.emit(dict(message))
        elif kind == "round_seen":
            self.round_seen.emit(dict(message))
        elif kind == "budget_reached":
            self.budget_reached.emit(dict(message))
        elif kind == "conversation_linked":
            self.conversation_linked.emit(dict(message))
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

    # -- agents working with each other (ADR-006) ----------------------------------
    def set_agent_storage(self, sessions_root, agents_path):
        """Where agent connections and this project's conversations are: every
        connection is then a target another agent's turn may be granted."""
        self._agent_storage = (str(sessions_root or ""), str(agents_path or ""))
        self._push_agent_storage()

    def _push_agent_storage(self):
        """Said again whenever the connections may have changed - a new one, one
        removed - so the host's targets are the connections there are."""
        if self.state == READY and self._agent_storage and all(self._agent_storage):
            self._send({"type": "agent_connections", "id": next(self._ids),
                        "sessions_root": self._agent_storage[0], "agents_path": self._agent_storage[1]})

    refresh_agents = _push_agent_storage

    def reply_task(self, task, answer, by="You"):
        """The person answers what a delegated task asks (ADR-011 D3): it goes on
        in its own conversation, as the conversation that delegated it."""
        self._send_when_ready({"type": "task_reply", "id": next(self._ids), "task": task,
                               "answer": answer, "by": by})

    def budget_answer(self, execution_id, go, by="you"):
        """Go on past a budget (raised by what it was first set to), or stop."""
        self._send_when_ready({"type": "budget_answer", "id": next(self._ids), "execution_id": execution_id,
                               "go": bool(go), "by": by})

    def budget_raise(self, session, done=None):
        """A conversation woken at its budget goes on: its budget goes up first."""
        self.ask("budget_raise", done or (lambda reply: None), session=session)

    def stop_task(self, task, by="you"):
        """The person stops a task that goes on through answers (ADR-011)."""
        self._send_when_ready({"type": "task_stop", "id": next(self._ids), "task": task, "by": by})

    def answer_agent(self, execution_id, approve, by="you"):
        """The person's word on a delegation or a message that waits for it."""
        self._send_when_ready({"type": "agent_answer", "id": next(self._ids),
                               "execution_id": execution_id, "approve": bool(approve), "by": by})

    # -- agents' changes to integrations (ADR-007) ----------------------------------
    def _integration_waits(self, message):
        execution_id = message.get("execution_id")
        if not execution_id:
            return
        card = dict(message.get("card") or {})
        actor = str(message.get("actor") or "")
        self.integration_changes[execution_id] = {
            "execution_id": execution_id, "actor": actor, "capability": message.get("capability") or "",
            "session": actor.split("/", 1)[1] if actor.startswith("agent:") and "/" in actor else "",
            "agent": self._agent_name(actor),
            "card": card, "editable": list(message.get("editable") or ()), "draft": dict(
                (key, str(((card.get("edit") or {}).get(key) or {}).get("value") or ""))
                for key in message.get("editable") or ()),
            "state": "waiting", "detail": "", "at": time.time()}
        self._trim_integration_changes()
        self.integration_changes_changed.emit()

    def _agent_name(self, actor):
        if not actor.startswith("agent:"):
            return actor
        connection_id = actor[len("agent:"):].split("/", 1)[0]
        return self._agent_names.get(connection_id, connection_id)

    def _trim_integration_changes(self):
        ended = [key for key, one in self.integration_changes.items() if one["state"] in INTEGRATION_ENDED]
        for key in ended[:max(0, len(ended) - INTEGRATION_KEPT)]:
            self.integration_changes.pop(key, None)

    def _integration_execution(self, execution):
        change = self.integration_changes.get(execution.get("execution_id"))
        if change is None:
            return
        state, error = execution.get("state"), execution.get("error") or {}
        if state in TERMINAL:
            change["state"] = "denied" if error.get("code") == "denied" else state
            change["detail"] = ((execution.get("output") or {}).get("summary") or "") if state == "succeeded" \
                else ("" if error.get("code") == "denied" else error.get("message") or error.get("code") or "")
        elif state == "running" and change["state"] in ("waiting", "approved"):
            change["state"] = "running"
        else:
            return
        self.integration_changes_changed.emit()

    def set_integration_draft(self, execution_id, field, value):
        """What the person typed into a waiting change, so a rebuilt card keeps it."""
        change = self.integration_changes.get(execution_id)
        if change is not None and field in change["editable"]:
            change["draft"][field] = value

    def answer_integration(self, execution_id, approve, by="you"):
        """Approve - with the draft as the person left it - or deny a waiting change."""
        change = self.integration_changes.get(execution_id)
        if change is None or change["state"] != "waiting":
            return
        offered = change["card"].get("edit") or {}
        changes = {key: value for key, value in change["draft"].items()
                   if value != str((offered.get(key) or {}).get("value") or "")} if approve else {}

        def answered(reply):
            if not reply.get("ok") and change["state"] in ("approved", "denied"):
                change["state"] = "failed"
                change["detail"] = "It no longer waited for an answer; nothing was changed by it."
                self.integration_changes_changed.emit()
        change["state"] = "approved" if approve else "denied"
        self.integration_changes_changed.emit()
        self.ask("integration_answer", answered, execution_id=execution_id, approve=bool(approve), by=by,
                 changes=changes)

    def dismiss_integration(self, execution_id):
        change = self.integration_changes.get(execution_id)
        if change is not None and change["state"] in INTEGRATION_ENDED:
            self.integration_changes.pop(execution_id, None)
            self.integration_changes_changed.emit()

    def _integration_host_gone(self):
        """The host ended: what waited for the person waits no more, and was not sent."""
        gone = False
        for change in self.integration_changes.values():
            if change["state"] in ("waiting", "approved", "running"):
                sent = change["state"] != "waiting"
                change["state"] = "unknown" if sent else "failed"
                change["detail"] = ("QAVector's core stopped while it was being made; read the issue to see "
                                    "whether it was." if sent else "QAVector's core stopped before you answered; "
                                    "nothing was changed.")
                gone = True
        if gone:
            self.integration_changes_changed.emit()

    def messages(self, done, **filters):
        """Messages from the host's store (ADR-006 D4): ``done(list)``."""
        self.ask("messages", lambda reply: done(reply.get("messages") or [] if reply.get("ok") else []),
                 **filters)

    def acknowledge_message(self, message_id, by="you"):
        self._send_when_ready({"type": "message_ack", "id": next(self._ids), "message_id": message_id, "by": by})

    @staticmethod
    def agent_grants(connection, session, mode):
        """``(grants, policy)`` a turn gets for other agents: the connection's
        allowed agents as targets when the conversation is not Deny - and a task
        for its own program - and what the conversation is; nothing at all for
        Deny."""
        connection, session = connection or {}, session or {}
        others = session.get("others")
        if others not in ("auto", "approve") or not session.get("folder"):
            return [], None
        own = connection.get("id")
        targets = [one for one in connection.get("allowed_agents") or [] if one and one != own]
        if not targets and not own:
            return [], None
        grants = [{"permission": permission, "resource_id": prefix + target}
                  for target in targets
                  for permission, prefix in (("agents.delegate", "agent-connection~"),
                                             ("agents.message", "agent-messages~"))]
        if own:
            # Its own program too, as another model, say (ADR-009) - a task only:
            # it does not message itself, and the child hands nothing on.
            grants.append({"permission": "agents.delegate", "resource_id": "agent-connection~" + own})
        grants.append({"permission": "agents.delegate", "resource_id": "agent-tasks"})
        # Every role the person made, whatever the Allowed agents list says (ADR-010 D2).
        grants.append({"permission": "agents.delegate", "resource_id": "agent-roles"})
        policy = {"session": session.get("id") or "", "connection": own or "",
                  "name": connection.get("name") or own or "", "folder": session["folder"],
                  "mode": mode if mode in ("read", "edit", "full") else "read", "others": others,
                  "restrictions": dict(session.get("restrictions") or {}),
                  # How it goes on when what it hands on ends (ADR-009 D3).
                  "wake": session.get("wake") if session.get("wake") in WAKES
                  else connection.get("wake_default") if connection.get("wake_default") in WAKES else "ask"}
        return grants, policy

    def grant_agent(self, connection, session_id, session=None, mode=None):
        """QAVector for one turn of ``connection``'s agent (ADR-003 D4/D6), or None.

        None when the host is not ready, has no socket, or the connection's
        "QAVector access" grants nothing and the conversation works with no
        other agent. Otherwise the actor is issued its grants (and, for other
        agents, what its conversation is), a fresh token's digest is
        registered, and the token itself is written to a 0600 file beside the
        socket - the turn gets its *path*.
        """
        if (connection or {}).get("id"):
            # What a change it proposes is said to come from (ADR-007).
            self._agent_names[connection["id"]] = connection.get("name") or connection["id"]
        access = (connection or {}).get("qavector_access") or {}
        permissions = sorted({p for key, on in access.items() if on is True for p in AGENT_GRANTS.get(key, ())})
        others, policy = self.agent_grants(connection, dict(session or {}, id=session_id), mode)
        if self.state != READY or self._proc is None or not self.socket_path or not (permissions or others):
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
        message = {"type": "actor", "id": next(self._ids), "actor": actor,
                   "grants": [{"permission": p} for p in permissions] + others}
        if policy is not None:
            message["agents"] = policy
        if os.path.isabs(str((session or {}).get("folder") or "")):
            # The only place a file it asks to attach may come from (ADR-007).
            message["folder"] = session["folder"]
        self._send(message)
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
