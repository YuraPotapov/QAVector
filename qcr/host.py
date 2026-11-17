"""The QCR host (ADR-002 D1/D2): one core process the GUI starts and talks to.

``session_launcher.py --qcr-host --qcr-project=ID --qcr-store=PATH``. It holds
the registry, the policy, the engine and the execution journal of one project.
Its only channel is its own stdin/stdout - JSON, one object per line - so the
only party that can send it anything is the GUI that started it. There is no
socket and no listener. It ends on ``shutdown``, at the end of stdin, or (on
Linux) when its parent dies.

**The GUI is the trust root.** It names the actors and their grants (``hello``,
``actor``, ``revoke``); nothing in an ``invoke`` can claim an actor or a grant
that was not given that way.

GUI -> host::

    {"type": "snapshot", "snapshot": {...}}            services as the supervisor sees them
    {"type": "inventory", "inventory": {...}}          scenarios, environments, accounts (the GUI's --describe)
    {"type": "pending", "pending": {...} | null}       what is open in the cycle run on screen
    {"type": "hello", "protocol": 1, "actors": {"gui-user": [{"permission": "services.start"}, ...]}}
    {"type": "actor", "actor": "cycle:r1", "grants": [...], "folder": "/abs"?} / {"type": "revoke", "actor": ...}
    {"type": "invoke", "id": 7, "actor": "gui-user", "capability": "services.<id>.start",
     "input": {...}, "idempotency_key": "...", "correlation_id": "..."}
    {"type": "cancel" | "get" | "events" | "list", "id": 8, "actor": ..., ...}
    {"type": "owner", "execution_id": ..., "event": "accepted" | "running" | "progress"
                                                     | "succeeded" | "failed", ...}
    {"type": "owner_read_reply", "id": 4, "result": {...}} / {..., "error": {...}}   to an owner_read
    {"type": "agent_token", "id": 9, "actor": "agent:<connection>/<session>",
     "token_sha256": "...", "expires_at": 1731500000}    an issued actor's token, by digest
    {"type": "capabilities" | "describe" | "read", "id": 10, "actor": ..., ...}
    {"type": "integration_answer", "id": 11, "execution_id": ..., "approve": true,
     "by": "you", "changes": {"body": "..."}}      the person on a waiting integration write
    {"type": "shutdown"}

**Agents** reach the host on its agent socket (:mod:`qcr.agent_socket`, ADR-003
D4) with a token the GUI registered here. Their requests are the same kinds -
``capabilities``, ``describe``, ``read``, ``invoke``, ``get``, ``events``,
``wait`` (at most 60 s), ``cancel``, ``list`` - answered for the token's actor
only. The socket's path is in ``ready``.

host -> GUI::

    {"type": "ready", "protocol": 1, "recovered": [...]}
    {"type": "reply", "id": 7, "ok": true, ...} / {"type": "reply", "id": 7, "ok": false, "error": {...}}
    {"type": "dispatch", "execution_id": ..., "op": "start", "resource_id": ..., "input": {...}}
    {"type": "stop_waiting", "execution_id": ...}
    {"type": "integration_approval", "execution_id": ..., "actor": ..., "capability": ...,
     "card": {...}, "editable": ["body"]}               an integration write waits for the person (ADR-007)
    {"type": "owner_read", "id": 4, "op": "service.output", "resource_id": ..., "lines": 50}
    {"type": "owner_read", "id": 5, "op": "launcher.status" | "launcher.output", "resource_id": "main", ...}
    {"type": "execution", "execution": {...}}           after every change
    {"type": "error", "error": {...}}                   a line that could not be taken

Nothing is printed on stdout but these lines; logging goes to stderr.
"""

import json
import os
import logging
import signal
import sys
import threading
import time

from .adapters.cycles import CycleAdapter, CycleAnswerInvoker, CycleInvoker
from .adapters.launcher import WAIT_SECONDS as LAUNCHER_WAIT_SECONDS, LauncherAdapter, LauncherInvoker
from .agent_socket import AgentGateway, SocketError, TokenBook
from .adapters.scenarios import ScenarioAdapter, ScenarioInvoker
from .adapters.services import ServiceAdapter, ServiceInvoker
from .contracts import document, text
from .engine import Engine
from .errors import QCRError
from .executions import TERMINAL, ExecutionStore
from .policy import Authority, Grant
from .registry import Registry

PROTOCOL = 1
#: Longest line taken; a longer one is refused, not read into memory whole.
LINE_BYTES = 1024 * 1024
_PR_SET_PDEATHSIG = 1
#: What an agent may ask on the socket; everything else is the GUI's alone.
AGENT_KINDS = ("capabilities", "describe", "read", "invoke", "get", "events", "wait", "cancel", "list")
WAIT_SECONDS = 60
#: What an agent is told after handing work on, by its conversation's "When tasks finish" (ADR-009 D7).
#: Executions that are part of the task their input names (ADR-012 D3).
PART_OF_TASK = ("agents.tasks.reply", "agents.tasks.verify")

NEXT = {
    "continue": "This conversation continues by itself with the results: when all you handed on in this "
                "turn has ended, or as agents.tasks.wake_when says. Hand on the rest, tell the person what "
                "you started, and end your turn - do not wait in it.",
    "ask": "When what you handed on has ended, the person is asked to continue this conversation with the "
           "results. Tell them what you started and end your turn, or wait here with qavector_wait for "
           "something short.",
    "off": "This conversation is not continued by itself: wait with qavector_wait if you need the results "
           "in this turn.",
}
#: How long an owner read (a service's output) may take the GUI to answer.
OWNER_READ_SECONDS = 5

log = logging.getLogger("qcr.host")


def _error(code, message, **extra):
    return dict({"code": code, "message": message}, **extra)


class Host:
    def __init__(self, project_id, store_path, write, *, schedule=None):
        text(project_id, "project id")
        self.project_id = project_id
        self._write_line = write
        self._write_lock = threading.Lock()
        self.authority = Authority()
        self.registry = Registry(self.authority)
        self.store = ExecutionStore(store_path)
        #: Context packages live beside the journal, in the project folder (ADR-006 D7).
        self.context_dir = os.path.join(os.path.dirname(os.path.abspath(store_path)), "context")
        self._messages_path = os.path.join(os.path.dirname(os.path.abspath(store_path)), "messages.sqlite")
        self.messages = None
        #: Integration reads and writes, once the GUI names the project's connections file.
        self.integrations = None
        #: Agent usage reads, once the GUI says where the conversations are.
        self.analytics = None
        self._secrets_path = None
        extra = {} if schedule is None else {"schedule": schedule}
        self.engine = Engine(self.registry, self.store, on_change=self._changed, **extra)
        self.invoker = ServiceInvoker(self.send, **extra)
        self._snapshot = None
        self.services = ServiceAdapter(self.registry, project_id, lambda: self._snapshot, self.invoker)
        self._inventory = None
        self.scenario_invoker = ScenarioInvoker(self.send, **extra)
        self.scenarios = ScenarioAdapter(self.registry, project_id, lambda: self._inventory, self.scenario_invoker)
        self.cycle_invoker = CycleInvoker(self.send, **extra)
        self.answer_invoker = CycleAnswerInvoker(self.send, **extra)
        self._pending = None
        self.cycles = CycleAdapter(self.registry, project_id, lambda: self._inventory, self.cycle_invoker,
                                   pending_reader=lambda: self._pending, answer_invoker=self.answer_invoker)
        self.launcher_invoker = LauncherInvoker(self.send, **extra)
        self.launcher = LauncherAdapter(self.registry, project_id, self.launcher_invoker)
        #: Every invoker an ``owner`` report may belong to.
        self._invokers = (self.invoker, self.scenario_invoker, self.cycle_invoker, self.answer_invoker,
                          self.launcher_invoker)
        self._callers = {}
        self._ready = False
        self.done = False
        #: One request at a time through the registry - the GUI's pipe and the agents' socket alike.
        self._lock = threading.RLock()
        self.tokens = TokenBook()
        self.gateway = None
        self.agent_runtime = None
        #: Delegation between agent connections (ADR-006), once the GUI says where
        #: they are; and what each agent actor's conversation is, as the GUI said.
        self.delegation = None
        self._agent_policies = {}
        self._folders = {}                # agent actor -> its conversation's folder, as the GUI said
        #: Told when a snapshot arrives - what a criteria wait wakes on.
        self._snapshot_seen = threading.Condition(threading.Lock())
        self._owner_reads = {}            # id -> [Event, reply]
        self._owner_read_ids = iter(range(1, 1 << 62))

    # -- output -------------------------------------------------------------------
    def send(self, message):
        line = json.dumps(message, separators=(",", ":"), allow_nan=False)
        with self._write_lock:
            self._write_line(line)

    def _changed(self, execution_id):
        try:
            execution = self.store.get(execution_id)
            self.send({"type": "execution", "execution": execution})
        except Exception:
            log.debug("could not push execution %s", execution_id)
            return
        if self.delegation is not None:
            # A task or question that ended may start what was after it, and wake its conversation.
            self.delegation.execution_changed(execution)

    def open_integrations(self, integrations_path="", secrets_path="", environments_path=""):
        """Register the project's integration reads (Jira) - the 1B adapter -
        from its connections file. Reads configuration only; nothing is asked
        of a service until somebody reads (lazy, as everywhere)."""
        from cycle import secrets as secret_store
        from integrations import scope, store
        from integrations.reader import ProjectReader
        from .adapters.integrations import IntegrationAdapter
        integrations_path = store.resolve_path(integrations_path)
        self._secrets_path = secret_store.resolve_path(secrets_path)
        reader = ProjectReader(integrations_path, secret_store.resolve_path(secrets_path),
                               environments_path or scope.default_environments_path(integrations_path))
        self.integrations = IntegrationAdapter(self.registry, self.project_id, reader,
                                               notify=self.send, label=self._actor_label,
                                               folder=self._folders.get)
        self.integrations.refresh()

    def _actor_label(self, actor):
        """Who asked for a change, as the activity log names them: an agent by
        its connection's name, anybody else by their actor id."""
        actor = str(actor or "")
        if not actor.startswith("agent:"):
            return actor
        connection_id = actor[len("agent:"):].split("/", 1)[0]
        name = connection_id
        if self.delegation is not None:
            try:
                from integrations.agents import store as agent_store
                found = agent_store.get(connection_id, self.delegation.agents_path)
                name = (found or {}).get("name") or connection_id
            except Exception:
                pass
        return "Agent " + name

    def open_agent_socket(self, directory=None):
        """Start listening for agents (ADR-003 D4). Returns the socket's path."""
        self.gateway = AgentGateway(self.handle_agent, self.tokens, directory)
        return self.gateway.path

    def close(self):
        self.done = True
        if self.gateway is not None:
            self.gateway.close()
        if self.agent_runtime is not None:
            self.agent_runtime.close()
        if self.delegation is not None:
            self.delegation.close()
        if self.integrations is not None:
            self.integrations.writer.close()
        self.engine.close()
        if self.messages is not None:
            self.messages.close()
        self.store.close()

    # -- input --------------------------------------------------------------------
    def feed(self, line):
        """Take one line. Never raises: a bad line is answered, not fatal."""
        if len(line.encode("utf-8", "replace")) > LINE_BYTES:
            self.send({"type": "error", "error": _error("too_large", "Line is too long.")})
            return
        try:
            message = json.loads(line)
        except ValueError:
            self.send({"type": "error", "error": _error("invalid_message", "Line is not JSON.")})
            return
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            self.send({"type": "error", "error": _error("invalid_message", "Expected an object with a type.")})
            return
        kind = message["type"]
        handler = getattr(self, "_on_" + kind, None) if kind.isidentifier() else None
        request_id = message.get("id")
        try:
            if handler is None:
                raise QCRError("invalid_message", "Unknown message type.")
            if not self._ready and kind not in ("snapshot", "inventory", "pending", "hello", "shutdown"):
                raise QCRError("not_ready", "Say hello first.")
            with self._lock:
                answer = handler(message)
            if answer is not None:
                self.send(dict({"type": "reply", "id": request_id, "ok": True}, **answer))
        except QCRError as exc:
            error = _error(exc.code, str(exc))
            if getattr(exc, "execution_id", None):
                error["execution_id"] = exc.execution_id
            self.send({"type": "reply", "id": request_id, "ok": False, "error": error} if request_id is not None
                      else {"type": "error", "error": error})
        except Exception:
            log.exception("message %r failed", kind)
            self.send({"type": "reply", "id": request_id, "ok": False,
                       "error": _error("host_error", "The host could not handle the message.")})

    def _caller(self, message):
        caller = self._callers.get(message.get("actor"))
        if caller is None:
            raise QCRError("authorization_denied", "Unknown actor.")
        return caller

    def _grants(self, grants):
        if not isinstance(grants, list):
            raise QCRError("invalid_message", "Grants are a list.")
        made = []
        for grant in grants:
            if not isinstance(grant, dict) or set(grant) - {"permission", "resource_id"}:
                raise QCRError("invalid_message", "A grant is {permission, resource_id}.")
            made.append(Grant(self.project_id, grant.get("permission"), grant.get("resource_id")))
        return made

    def _issue(self, actor, grants):
        text(actor, "actor")
        previous = self._callers.pop(actor, None)
        if previous is not None:
            self.authority.revoke(previous)
        self._callers[actor] = self.authority.issue(actor, self._grants(grants))

    # -- handlers -----------------------------------------------------------------
    def _on_agent_profiles(self, message):
        """Trusted owner pipe only; never accepted from the agent socket.

        Profiles bind logical agents to their connection/workspace. No actor
        receives agents.execute automatically, including the agents themselves.
        """
        from integrations.agents.contracts import AgentProfile
        from .adapters.agents import AgentRuntime
        profiles = message.get("profiles")
        if not isinstance(profiles, list) or len(profiles) > 200:
            raise QCRError("invalid_message", "Agent profiles must be a bounded list.")
        try:
            made = [AgentProfile(**row) for row in profiles]
        except (TypeError, ValueError):
            raise QCRError("invalid_message", "Invalid agent profile fields.") from None
        root, path = message.get("sessions_root"), message.get("agents_path")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 4000
               for value in (root, path)):
            raise QCRError("invalid_message", "Agent storage paths are required.")
        if self.agent_runtime is None:
            runtime = AgentRuntime(self.registry, self.engine, self.project_id, root, path)
            runtime.configure(made)
            self.agent_runtime = runtime
        else:
            if (root, path) != (self.agent_runtime.root, self.agent_runtime.agents_path):
                raise QCRError("conflict", "Agent storage cannot change during this host's lifetime.")
            self.agent_runtime.configure(made)
        return {}

    def _on_snapshot(self, message):
        self._snapshot = message.get("snapshot")
        self.services.refresh()
        with self._snapshot_seen:
            self._snapshot_seen.notify_all()
        return None

    def _on_owner_read_reply(self, message):
        slot = self._owner_reads.get(message.get("id"))
        if slot is not None:
            slot[1] = message
            slot[0].set()
        return None

    def _on_inventory(self, message):
        self._inventory = message.get("inventory")
        self.scenarios.refresh()
        self.cycles.refresh()
        return None

    def _on_pending(self, message):
        self._pending = message.get("pending")
        return None

    def _on_hello(self, message):
        if self._ready:
            raise QCRError("invalid_message", "Hello was already said.")
        if message.get("protocol") != PROTOCOL:
            raise QCRError("unsupported_protocol", "Protocol version is not supported.")
        actors = message.get("actors") or {}
        if not isinstance(actors, dict):
            raise QCRError("invalid_message", "Actors are an object.")
        for actor, grants in actors.items():
            self._issue(actor, grants)
        recovered = self.engine.recover(self._observe)
        self._ready = True
        self.send({"type": "ready", "protocol": PROTOCOL, "recovered": recovered,
                   "socket": self.gateway.path if self.gateway is not None else None})
        return None

    def _observe(self, execution):
        rows = (self._snapshot or {}).get("resources", ())
        row = next((row for row in rows if row.get("resource_id") == execution["resource_id"]), None)
        return {"service_status": row["properties"]["status"]} if row else None

    def _on_actor(self, message):
        """An actor and its grants; for an agent turn, also what its conversation
        is (``agents``: ADR-006 policy) - kept after the turn, for its tasks."""
        from .adapters.delegation import policy
        rule = policy(message.get("agents"))
        folder = message.get("folder")
        if folder is not None and (not isinstance(folder, str) or not os.path.isabs(folder) or len(folder) > 4000):
            raise QCRError("invalid_message", "An actor's folder is an absolute path.")
        self._issue(message.get("actor"), message.get("grants"))
        # Where the conversation works: the one place its files may be taken from (ADR-007 7.3).
        if folder:
            self._folders[message.get("actor")] = folder
        else:
            self._folders.pop(message.get("actor"), None)
        if rule is not None:
            # What the conversation itself was given: a task it hands on gets
            # at most this (ADR-006 D2), even after its own turn has ended.
            rule["grants"] = [dict(one) for one in message.get("grants") or [] if isinstance(one, dict)]
            self._agent_policies[message.get("actor")] = rule
            if self.delegation is not None:
                # A turn of a delegating conversation begins: what it hands on is its round (ADR-009).
                self.delegation.rounds.turn_started(message.get("actor"), rule["session"])
        else:
            self._agent_policies.pop(message.get("actor"), None)
        return {}

    def _on_agent_connections(self, message):
        """Trusted owner pipe only: where agent connections and conversations
        are, so every connection becomes a delegation target (ADR-006). Sent
        again when the connections change; the paths cannot."""
        from .adapters.delegation import Delegation
        root, path = message.get("sessions_root"), message.get("agents_path")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 4000
               for value in (root, path)):
            raise QCRError("invalid_message", "Agent storage paths are required.")
        if self.delegation is None:
            from .messages import MessageStore
            self.messages = MessageStore(self._messages_path)
            self.delegation = Delegation(self.registry, self.engine, self.store, self.project_id,
                                         root, path, self._agent_policies, self.send,
                                         self.context_dir, self._known_secrets, self.messages,
                                         grant=self._grant_child, revoke=self._end_child)
        elif (root, path) != (self.delegation.root, self.delegation.agents_path):
            raise QCRError("conflict", "Agent storage cannot change during this host's lifetime.")
        self.delegation.configure()
        if self.analytics is None:
            # What agent work used and cost, read from the same record (ADR-008 U.2).
            from .adapters.analytics import AnalyticsAdapter
            self.analytics = AnalyticsAdapter(self.registry, self.project_id, root)
            self.analytics.refresh()
        return {}

    def _on_messages(self, message):
        """Owner pipe: messages for a connection's inbox, a conversation, or a thread."""
        from .messages import summary
        if self.messages is None:
            return {"messages": []}
        filters = {}
        for key in ("recipient_connection", "recipient_session", "sender_session", "conversation_id", "state"):
            value = message.get(key)
            if value is not None:
                if not isinstance(value, str) or not value or len(value) > 400:
                    raise QCRError("invalid_message", "A message filter is a string.")
                filters[key] = value
        limit = message.get("limit", 50)
        if type(limit) is not int or not 0 < limit <= 200:
            raise QCRError("invalid_message", "A limit is 1 to 200.")
        return {"messages": [summary(one) for one in self.messages.list(self.project_id, limit=limit, **filters)]}

    def _on_message_ack(self, message):
        """Owner pipe: the person passed a delivered message into a chat."""
        if self.delegation is None or self.messages is None:
            raise QCRError("not_found", "No delivered message has that id.")
        return {"message": self.delegation.acknowledge(message.get("message_id"), message.get("by", ""))}

    def _grant_child(self, actor, grants):
        """QAVector for one delegated turn (ADR-006 D2): its actor issued with
        ``grants``, a fresh token by digest, the token in a 0600 file beside the
        socket. None without a socket or without grants."""
        import hashlib
        import secrets
        import tempfile
        if self.gateway is None or not grants:
            return None
        folder = os.path.join(os.path.dirname(self.gateway.path), "tokens")
        os.makedirs(folder, mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
        token = secrets.token_hex(32)
        descriptor, token_file = tempfile.mkstemp(prefix="t-", dir=folder)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(token)
        os.chmod(token_file, 0o600)
        with self._lock:
            self._issue(actor, grants)
            self.tokens.add(actor, hashlib.sha256(token.encode()).hexdigest(), time.time() + 12 * 3600)
        return {"actor": actor, "socket": self.gateway.path, "token_file": token_file}

    def _end_child(self, access):
        if not access:
            return
        with self._lock:
            caller = self._callers.pop(access["actor"], None)
            if caller is not None:
                self.authority.revoke(caller)
            self.tokens.forget_actor(access["actor"])
        try:
            os.unlink(access["token_file"])
        except OSError:
            pass

    def _known_secrets(self):
        """Every value in the secrets store, for a context package to be checked
        against (ADR-006 D7). Nothing when no store was opened."""
        if not self._secrets_path:
            return []
        from cycle import secrets as secret_store
        found = []
        for values in (secret_store.load(self._secrets_path) or {}).values():
            if isinstance(values, dict):
                found += [one for one in values.values() if isinstance(one, str)]
        return found

    def _on_agent_answer(self, message):
        """The person approved or denied a delegation that waits (ADR-006 D3)."""
        approved = message.get("approve")
        if type(approved) is not bool:
            raise QCRError("invalid_message", "An answer approves or not.")
        if self.delegation is None or not self.delegation.answer(
                message.get("execution_id"), approved, str(message.get("by") or "")):
            raise QCRError("not_found", "No delegation waits for that answer.")
        return {}

    def _on_task_reply(self, message):
        """The person answers what a delegated task asks (ADR-011 D3). It runs as
        the conversation that delegated the task - its turn may have ended - so
        that conversation follows it as its own, and is woken by it."""
        from .adapters.delegation import PERMISSION, TASKS
        task, answer = message.get("task"), message.get("answer")
        if not isinstance(task, str) or not task or not isinstance(answer, str) or not answer.strip():
            raise QCRError("invalid_message", "A reply names its task and says something.")
        if self.delegation is None:
            raise QCRError("not_found", "No task waits for an answer.")
        found = self.delegation.store.get(task)
        actor = str(found.get("actor_id") or "")
        if found.get("project_id") != self.project_id or not actor.startswith("agent:"):
            raise QCRError("not_found", "No task waits for an answer.")
        caller, temporary = self._callers.get(actor), None
        if caller is None:
            temporary = caller = self.authority.issue(actor, self._grants(
                [{"permission": PERMISSION, "resource_id": TASKS}]))
        self.delegation.person_says(actor, task, message.get("by"))
        try:
            execution = self.engine.invoke(caller, self.project_id, "agents.tasks.reply",
                                           {"task": task, "answer": answer}, parent_id=task)
        except Exception:
            self.delegation.person_says(actor, task, None, forget=True)
            raise
        finally:
            if temporary is not None:
                self.authority.revoke(temporary)
        return {"execution": execution}

    def _on_task_stop(self, message):
        """The person stops a delegated task that goes on through answers (ADR-011):
        it takes no more answers, and what of it still runs is canceled."""
        task = message.get("task")
        if not isinstance(task, str) or not task or self.delegation is None:
            raise QCRError("invalid_message", "A stop names its task.")
        latest = self.delegation.stop(task, str(message.get("by") or ""))
        execution = self.delegation.store.get(latest)
        if execution.get("project_id") != self.project_id:
            raise QCRError("not_found", "No such task.")
        if execution["state"] not in TERMINAL:
            actor = execution["actor_id"]
            caller, temporary = self._callers.get(actor), None
            if caller is None:
                temporary = caller = self.authority.issue(actor, ())
            try:
                execution = self.engine.cancel(caller, latest, "Stopped by the person.")
            finally:
                if temporary is not None:
                    self.authority.revoke(temporary)
        return {"execution": execution}

    def _on_tasks(self, message):
        """Every task and question agents handed on in the project (ADR-012 D4)."""
        return {"tasks": self.delegation.board() if self.delegation is not None else []}

    def _on_task_history(self, message):
        """One task as it went (ADR-012 D5)."""
        task = message.get("task")
        if not isinstance(task, str) or not task or self.delegation is None:
            raise QCRError("invalid_message", "A history names its task.")
        return self.delegation.history(task)

    def _on_budget_answer(self, message):
        """The person's word on a turn held at a budget (ADR-013 D3): go on, with
        the budget raised by what it was first set to, or stop."""
        go = message.get("go")
        if type(go) is not bool:
            raise QCRError("invalid_message", "A budget answer goes on or not.")
        if self.delegation is None or not self.delegation.budget_answer(
                message.get("execution_id"), go, str(message.get("by") or "")):
            raise QCRError("not_found", "Nothing waits at a budget.")
        return {}

    def _on_budget_raise(self, message):
        """Continue on a conversation woken at its budget: the budget goes up by
        what it was first set to, and the conversation goes on."""
        from integrations.agents import budgets, store
        session = message.get("session")
        if not isinstance(session, str) or not session or self.delegation is None:
            raise QCRError("invalid_message", "A raise names its conversation.")
        policy = next((one for one in self._agent_policies.values() if one.get("session") == session), {})
        connection = store.get(policy.get("connection") or "", self.delegation.agents_path)
        try:
            budget = budgets.raised(budgets.load(self.delegation.root, session, connection))
            return {"budget": budgets.save(self.delegation.root, session, budget)}
        except Exception as exc:          # no record for that conversation, or an unwritable one
            raise QCRError("not_found", str(exc)) from None

    def _on_integration_answer(self, message):
        """The person approved - perhaps with the comment changed - or denied an
        integration write that waits (ADR-007 D3)."""
        approved, changes = message.get("approve"), message.get("changes") or {}
        if type(approved) is not bool:
            raise QCRError("invalid_message", "An answer approves or not.")
        if not isinstance(changes, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                                for key, value in changes.items()):
            raise QCRError("invalid_message", "Changes are text fields.")
        writer = self.integrations.writer if self.integrations is not None else None
        if writer is None or not writer.answer(message.get("execution_id"), approved,
                                               str(message.get("by") or ""), changes):
            raise QCRError("not_found", "No change waits for that answer.")
        return {}

    def _on_revoke(self, message):
        if self.delegation is not None:
            # The turn is over: what it asked to be woken for may fire now.
            self.delegation.rounds.turn_ended(message.get("actor"))
        caller = self._callers.pop(message.get("actor"), None)
        if caller is not None:
            self.authority.revoke(caller)
        self.tokens.forget_actor(message.get("actor"))
        return {}

    def _on_agent_token(self, message):
        """The GUI registers one agent turn's token, by its digest, for an actor it already issued."""
        actor = message.get("actor")
        if actor not in self._callers or not str(actor).startswith("agent:"):
            raise QCRError("invalid_message", "A token is for an agent actor issued first.")
        expires_at = message.get("expires_at")
        if type(expires_at) not in (int, float):
            raise QCRError("invalid_message", "A token says when it expires.")
        try:
            self.tokens.add(actor, message.get("token_sha256"), expires_at)
        except ValueError:
            raise QCRError("invalid_message", "A token is given by its SHA-256, in hex.") from None
        return {}

    def _on_capabilities(self, message):
        namespace = message.get("namespace")
        caller = self._caller(message)
        caps = self.registry.list_capabilities(caller, self.project_id,
                                               namespace=namespace if isinstance(namespace, str) else None)
        # Each with what it acts on, by name: an id alone ("services.56c8...start")
        # does not tell an agent which service is Odoo.
        resources = {one.ref: one for one in self.registry.list_resources(caller, self.project_id)}

        def target(cap):
            found = resources.get(cap.resource)
            return {"id": cap.resource.resource_id, "type": cap.resource.resource_type,
                    "name": found.display_name if found else "", "aliases": list(found.aliases) if found else []}
        return {"capabilities": [{"capability_id": cap.capability_id, "description": cap.description,
                                  "risk_level": cap.risk_level, "read_only": cap.read_only, "mode": cap.mode,
                                  "version": cap.version, "resource": target(cap)} for cap in caps]}

    def _on_describe(self, message):
        return {"capability": document(self.registry.describe_capability(
            self._caller(message), self.project_id, message.get("capability")))}

    def _on_read(self, message):
        return {"result": self.registry.read(self._caller(message), self.project_id, message.get("capability"),
                                             message.get("input") or {})}

    # -- reads that wait: outside the lock --------------------------------------------
    def _service_read(self, request):
        """``services.<id>.criteria`` with ``wait_seconds`` and ``services.<id>.output``:
        they wait - on the next snapshot, on the owner - so never while holding the
        lock the GUI's messages need. Others: None (the ordinary read)."""
        capability_id = str(request.get("capability") or "")
        op = capability_id.rsplit(".", 1)[-1]
        given = request.get("input") or {}
        if op not in ("criteria", "output") or (op == "criteria" and not given.get("wait_seconds")):
            return None
        with self._lock:
            if not self._ready:
                raise QCRError("not_ready", "QAVector is not ready yet.")
            caller = self._caller(request)
            capability = self.registry.describe_capability(caller, self.project_id, capability_id)
            self.registry.validate_input(capability, given)
        if op == "output":
            return {"result": self._owner_read(capability, "service.output",
                                               {"lines": int(given.get("lines", 50))})}
        end = time.monotonic() + float(given["wait_seconds"])
        while True:
            with self._lock:
                result = self.registry.read(caller, self.project_id, capability_id, given)
            left = end - time.monotonic()
            if result["settled"] != "waiting" or left <= 0:
                return {"result": result}
            with self._snapshot_seen:
                self._snapshot_seen.wait(min(left, 2.0))

    def _launcher_read(self, request):
        """``launcher.main.status`` / ``.output``: the window's to answer, and an
        output read may wait - so outside the lock, like a service's output."""
        capability_id = str(request.get("capability") or "")
        op = LauncherAdapter.owner_op(capability_id)
        if op is None:
            return None
        given = request.get("input") or {}
        with self._lock:
            if not self._ready:
                raise QCRError("not_ready", "QAVector is not ready yet.")
            capability = self.registry.describe_capability(self._caller(request), self.project_id, capability_id)
            self.registry.validate_input(capability, given)
        fields, wait = {}, 0.0
        if op == "launcher.output":
            wait = min(float(given.get("wait_seconds") or 0), LAUNCHER_WAIT_SECONDS)
            fields = {"after": int(given.get("after") or 0), "limit": int(given.get("limit") or 200),
                      "wait_seconds": wait}
        return {"result": self._owner_read(capability, op, fields, OWNER_READ_SECONDS + wait)}

    def _owner_read(self, capability, op, fields, seconds=None):
        """Ask the GUI for what only it holds (a service's console, the launcher's
        output), and wait for its answer - a little, or as long as the read waits."""
        request_id = next(self._owner_read_ids)
        slot = [threading.Event(), None]
        self._owner_reads[request_id] = slot
        try:
            self.send(dict(fields, type="owner_read", id=request_id, op=op,
                           resource_id=capability.resource.resource_id))
            if not slot[0].wait(OWNER_READ_SECONDS if seconds is None else seconds):
                raise QCRError("owner_unavailable", "QAVector's window did not answer in time.")
        finally:
            self._owner_reads.pop(request_id, None)
        reply = slot[1] or {}
        if reply.get("error"):
            raise QCRError(str(reply["error"].get("code") or "owner_failed"), "The window could not read it.")
        result = reply.get("result")
        self.registry.validate_output(capability, result)
        return result

    # -- the agent socket ---------------------------------------------------------
    def handle_agent(self, actor, message):
        """One request from an agent's connection, answered as ``actor`` (the token's)."""
        kind = message.get("type")
        if kind not in AGENT_KINDS:
            return {"ok": False, "error": _error("invalid_message", "Not something an agent may ask.")}
        request = dict(message, actor=actor)
        try:
            if kind == "wait":
                # Outside the lock: a wait must not hold the GUI's requests up.
                timeout = message.get("timeout", 30)
                if type(timeout) not in (int, float) or timeout < 0:
                    raise QCRError("invalid_message", "Timeout is a number of seconds.")
                with self._lock:
                    caller = self._caller(request)
                execution = self.engine.wait(caller, message.get("execution_id"), min(float(timeout), WAIT_SECONDS))
                self._saw(execution)
                return {"ok": True, "execution": execution}
            if kind == "read":
                capability_id = str(message.get("capability") or "")
                parts = capability_id.split(".")
                if (len(parts) == 3 and parts[0] == "agents" and parts[2] == "describe"
                        and self.delegation is not None):
                    # The target's models come from its program: read them here,
                    # outside the lock the GUI's messages need.
                    self.delegation.warm(parts[1])
                special = (self._service_read(request) if capability_id.startswith("services.")
                           else self._launcher_read(request) if capability_id.startswith("launcher.") else None)
                if special is not None:
                    return dict({"ok": True}, **special)
            with self._lock:
                if not self._ready:
                    raise QCRError("not_ready", "QAVector is not ready yet.")
                answer = getattr(self, "_on_" + kind)(request)
            return dict({"ok": True}, **answer)
        except QCRError as exc:
            error = _error(exc.code, str(exc))
            if getattr(exc, "execution_id", None):
                error["execution_id"] = exc.execution_id
            return {"ok": False, "error": error}

    def _on_invoke(self, message):
        caller = self._caller(message)
        parent_id = message.get("parent_id")
        given = message.get("input") if isinstance(message.get("input"), dict) else {}
        if message.get("capability") in PART_OF_TASK and isinstance(given.get("task"), str):
            # An answer to a task, or a verdict on it, is part of it (ADR-012 D3) -
            # said by the host, whatever the caller sent.
            parent_id = given["task"]
        execution = self.engine.invoke(
            caller, self.project_id, message.get("capability"), message.get("input", {}),
            idempotency_key=message.get("idempotency_key"), correlation_id=message.get("correlation_id"),
            parent_id=parent_id)
        answer = {"execution": execution}
        capability = str(message.get("capability") or "")
        handed = capability == "agents.tasks.reply" or capability.startswith("agents.") and (
            capability.endswith(".delegate") or (
            capability.endswith(".message") and (message.get("input") or {}).get("expects_reply")))
        if handed:
            # What comes next, said outright (ADR-009 D7): an agent that knows it
            # will be continued ends its turn instead of waiting in it.
            wake = (self._agent_policies.get(message.get("actor")) or {}).get("wake", "ask")
            answer["next"] = NEXT[wake]
        return answer

    def _on_cancel(self, message):
        return {"execution": self.engine.cancel(self._caller(message), message.get("execution_id"),
                                                message.get("reason"))}

    def _on_get(self, message):
        execution = self.engine.get(self._caller(message), message.get("execution_id"))
        if str(message.get("actor") or "").startswith("agent:"):
            self._saw(execution)
        return {"execution": execution}

    def _saw(self, execution):
        """The delegator got an ended task's result itself: no need to wake it for that (ADR-009 D2)."""
        if self.delegation is not None and execution.get("state") in ("succeeded", "failed", "canceled",
                                                                      "timed_out", "unknown"):
            self.delegation.rounds.seen(execution["execution_id"])

    def _on_events(self, message):
        after = message.get("after", 0)
        if type(after) is not int or after < 0:
            raise QCRError("invalid_message", "After is a cursor.")
        return self.engine.subscribe(self._caller(message), message.get("execution_id"), after)

    def _on_list(self, message):
        filters = message.get("filters") or {}
        allowed = {"state", "capability_id", "actor_id", "resource_id", "limit", "before"}
        if not isinstance(filters, dict) or set(filters) - allowed:
            raise QCRError("invalid_message", "Unknown filter.")
        return {"executions": self.engine.list(self._caller(message), self.project_id, **filters)}

    def _on_owner(self, message):
        for invoker in self._invokers:
            if invoker.knows(message.get("execution_id")):
                invoker.report(message)
                break
        return None

    def _on_shutdown(self, message):
        self.done = True
        return None


def _die_with_parent():
    """On Linux, end when the GUI does - even if it is killed outright."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass


def main(argv):
    """``--qcr-host --qcr-project=ID --qcr-store=PATH``. Returns the exit code."""
    options = dict(arg.split("=", 1) for arg in argv if "=" in arg)
    project_id, store_path = options.get("--qcr-project"), options.get("--qcr-store")
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s qcr-host: %(message)s")
    if not project_id or not store_path:
        log.error("--qcr-host needs --qcr-project=ID and --qcr-store=PATH")
        return 2
    _die_with_parent()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    out = sys.stdout

    def write(line):
        out.write(line + "\n")
        out.flush()
    try:
        host = Host(project_id, store_path, write)
    except QCRError as exc:
        write(json.dumps({"type": "error", "error": _error(exc.code, str(exc))}))
        return 1
    try:
        host.open_integrations(options.get("--integrations-file", ""), options.get("--cycle-secrets-file", ""),
                               options.get("--environments-file", ""))
    except Exception as exc:
        # No integration reads then; services, scenarios and cycles work as before.
        log.warning("integration reads unavailable: %s", exc)
    if options.get("--qcr-socket") != "off":
        try:
            host.open_agent_socket(options.get("--qcr-socket-dir"))
        except (SocketError, OSError) as exc:
            # The GUI's own path still works; only agents cannot reach this host.
            log.warning("agent socket unavailable: %s", exc)
    try:
        for line in sys.stdin:
            line = line.strip()
            if line:
                host.feed(line)
            if host.done:
                break
    finally:
        host.close()
    return 0
