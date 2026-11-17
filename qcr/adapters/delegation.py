"""One agent handing a task to another (ADR-006, Phase 5.1).

Every agent connection of this QAVector is a target with its own resource,
``agent-connection~<id>``, and capability ``agents.<id>.delegate``. Which
targets an agent turn may use is the grant the GUI gave its actor: the
permission ``agents.delegate`` on each target's resource. That grant is the
connection's "Allowed agents", and only when the conversation's "Other agents"
mode is not Deny. What the delegating conversation is (its folder, mode,
restrictions and mode for other agents) is the actor's **policy**. It comes
over the owner pipe with the actor, never from the agent.

A delegation is an execution like any other on the shared Engine. While the
person is asked (Approve), it waits ``dispatched`` with an
``approval.requested`` event. Then it runs one turn of a new conversation of
the target - a record (read-only in the chat) in the delegator's folder, under
its restrictions, at most in its mode, with **no QAVector access**, so it cannot
delegate further (depth 1). The output is evidence: the answer, what changed,
the artifacts, and each acceptance criterion ``unverified``.
``agents.tasks.verify`` lets the delegator record a verdict per criterion, with
the execution that proves it.

A child that cannot go on without an answer ends its turn with a question
line (ADR-011). The task ends asking; ``agents.tasks.reply`` - from the agent
that delegated it, or from the person - runs one more turn of the same child
conversation, so the child keeps everything it has read. A task may ask as
often as it needs (user, 2026-11-17): the questions are counted and shown, and
the person stops it when they decide to.

The delegating agent may choose the child's ``model`` and ``effort`` for the
task: it knows how hard the task is. ``agents.<id>.describe`` says what the
target offers - its role, models, efforts and defaults - read from its program
when asked and kept a few minutes. A choice the target does not offer is
refused before anything is written; none leaves the connection's defaults.
"""

import os
import queue
import threading
import time

import json

from integrations.agents import adapters, roles, runner, sessions, store
from qcr import context as packages
from qcr.contracts import CapabilityDescriptor, ResourceDescriptor, ResourceRef
from qcr.errors import QCRError
from qcr.executions import TERMINAL
from qcr.registry import Registration

from .agents import run_turn, turn_artifacts
from .rounds import NAME, RoundError, Rounds

PERMISSION = "agents.delegate"
MESSAGE_PERMISSION = "agents.message"
TASKS = "agent-tasks"
MODES = ("read", "edit", "full")            # narrowest first: a child never goes higher

#: ADR-006 D6: unfinished tasks and questions one conversation has handed on -
#: running, waiting for the person, or waiting for others to end (ADR-009).
MAX_CHILDREN = 3
#: Tasks one agent connection works on at once; more wait their turn (ADR-009).
MAX_PER_TARGET = 2
DEFAULT_SECONDS, MAX_SECONDS = 1800, 21600
GOAL_CHARS, SUMMARY_CHARS, CRITERION_CHARS, MAX_CRITERIA = 2000, 20000, 500, 10
ANSWER_CHARS = 20000
#: ADR-006 D6: messages one conversation may send in an hour.
MAX_MESSAGES_PER_HOUR = 20
#: How long a target's model list, read from its program, is believed.
CATALOG_SECONDS = 600
QUESTION_CHARS = 2000
#: What a task that asks is, for rounds and dependencies: not failed, not done.
ASKS = "asks"
#: The workbench (ADR-012): how many agent executions it reads, newest first,
#: and how many events of one execution a task's history reads.
BOARD_LIMIT = 1000
EVENT_PAGE = 500

_EXECUTION = {"type": "string", "minLength": 1, "maxLength": 200}
_MODEL = {"type": "string", "minLength": 1, "maxLength": 200}
_NAME = {"type": "string", "pattern": NAME.pattern}
_EFFORT = {"type": "string", "minLength": 1, "maxLength": 32}
DELEGATE_INPUT = {
    "type": "object", "additionalProperties": False, "required": ["goal"],
    "properties": {
        "goal": {"type": "string", "minLength": 1, "maxLength": GOAL_CHARS},
        "acceptance_criteria": {"type": "array", "maxItems": MAX_CRITERIA,
                                "items": {"type": "string", "minLength": 1, "maxLength": CRITERION_CHARS}},
        "summary": {"type": "string", "maxLength": SUMMARY_CHARS},
        "mode": {"enum": list(MODES)},
        "files": {"type": "array", "maxItems": 50,
                  "items": {"type": "string", "minLength": 1, "maxLength": 1000}},
        "artifacts": {"type": "array", "maxItems": 20, "items": _EXECUTION},
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_SECONDS},
        "model": _MODEL, "effort": _EFFORT, "name": _NAME,
        "after": {"type": "array", "maxItems": 5, "uniqueItems": True, "items": _NAME}}}
DELEGATE_OUTPUT = {"type": "object"}
MESSAGE_INPUT = {
    "type": "object", "additionalProperties": False, "required": ["body"],
    "properties": {"body": {"type": "string", "minLength": 1, "maxLength": 20000},
                   "expects_reply": {"type": "boolean"}, "reply_to": _EXECUTION,
                   "model": _MODEL, "effort": _EFFORT, "name": _NAME}}
WAKE_INPUT = {"type": "object", "additionalProperties": False, "properties": {
    "all": {"type": "array", "minItems": 1, "maxItems": 10, "uniqueItems": True, "items": _NAME},
    "any": {"type": "array", "minItems": 1, "maxItems": 10, "uniqueItems": True, "items": _NAME},
    "on_failure": {"type": "boolean"}}}
#: How a conversation goes on when its tasks end (ADR-009 D3).
WAKES = ("continue", "ask", "off")
_TEXT = {"type": "string", "maxLength": 4000}
DESCRIBE_OUTPUT = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "name", "provider", "role", "default_model", "default_effort", "efforts", "models",
                 "models_known"],
    "properties": {
        "id": _TEXT, "name": _TEXT, "provider": _TEXT, "role": _TEXT,
        "default_model": _TEXT, "default_effort": _TEXT,
        "efforts": {"type": "array", "items": _TEXT, "maxItems": 20},
        "models": {"type": "array", "maxItems": 200, "items": {
            "type": "object", "additionalProperties": False, "required": ["value", "label"],
            "properties": {"value": _TEXT, "label": _TEXT, "description": _TEXT,
                           "efforts": {"type": "array", "items": _TEXT, "maxItems": 20}}}},
        "models_known": {"type": "boolean"}}}
REPLY_INPUT = {
    "type": "object", "additionalProperties": False, "required": ["task", "answer"],
    "properties": {"task": _EXECUTION, "answer": {"type": "string", "minLength": 1, "maxLength": ANSWER_CHARS}}}
VERIFY_INPUT = {
    "type": "object", "additionalProperties": False, "required": ["task", "criterion", "verdict"],
    "properties": {"task": _EXECUTION, "criterion": {"type": "integer", "minimum": 0},
                   "verdict": {"enum": ["passed", "failed"]}, "evidence": _EXECUTION,
                   "note": {"type": "string", "maxLength": 2000}}}

#: What the child is told after the task: the turn is all it has.
CLOSING = ("Work only in this folder. If you cannot go on without an answer, stop and end with one "
           "last line of JSON: " + '{"needs": "answer", "question": "..."}' + " - the one who gave you "
           "this task answers, and you go on here with all you have read. Otherwise end with what you "
           "changed and how you checked it.")
#: What the child is told with an answer to its question (ADR-011).
ANSWERED = "[QAVector: %s answers your question]"
GO_ON = ("Go on with the task. If you cannot go on without another answer, end with the same line of "
         "JSON as before; otherwise end with what you changed and how you checked it.")


def child_grants(parent, connection):
    """What a delegated turn may do in QAVector (ADR-006 D2): the delegating
    conversation's own grants that the target connection's switches also allow -
    never ``agents.*``, so a child cannot hand anything on (depth 1)."""
    allowed = store.access_permissions(connection)
    found, seen = [], set()
    for one in parent or []:
        permission = one.get("permission")
        if not isinstance(permission, str) or permission.startswith("agents.") or permission not in allowed:
            continue
        key = (permission, one.get("resource_id"))
        if key not in seen:
            seen.add(key)
            found.append({key_: value for key_, value in (("permission", permission),
                                                         ("resource_id", one.get("resource_id"))) if value})
    return found


#: Every role is a target on this one resource (ADR-010): a grant does not change
#: when the person adds or removes a role.
ROLES = "agent-roles"


def split_target(capability_id):
    """``(connection or role id, is a role)`` of ``agents.<id>.<op>`` or ``agents.roles.<id>.<op>``."""
    parts = capability_id.split(".")
    if len(parts) == 4 and parts[1] == "roles":
        return parts[2], True
    return parts[1], False


def verdict_of(answer):
    """``(verdict, issues)`` from the last line of JSON a verdict role ends with;
    ``("no_verdict", [])`` when it gave none (ADR-010 D4: treated as rework)."""
    for line in reversed(str(answer or "").strip().splitlines()):
        line = line.strip().strip("`").strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            found = json.loads(line)
        except ValueError:
            continue
        if isinstance(found, dict) and found.get("verdict") in ("approve", "rework"):
            issues = [str(one)[:1000] for one in found.get("issues") or [] if str(one).strip()][:30]
            return found["verdict"], issues
    return "no_verdict", []


def question_of(answer):
    """The question a child ended its turn with (ADR-011 D1), or "" for none."""
    for line in reversed(str(answer or "").strip().splitlines()):
        line = line.strip().strip("`").strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            found = json.loads(line)
        except ValueError:
            continue
        if isinstance(found, dict) and found.get("needs") == "answer" and str(found.get("question") or "").strip():
            return str(found["question"]).strip()[:QUESTION_CHARS]
    return ""


def _count_files(files):
    """How many files a turn changed: its record keeps the number, a diff the list."""
    if isinstance(files, int) and not isinstance(files, bool):
        return files
    return len(files) if isinstance(files, list) else 0


def target_resource(connection_id):
    return "agent-connection~" + connection_id


def message_resource(connection_id):
    """Apart from the delegation resource: a message must not wait behind a task."""
    return "agent-messages~" + connection_id


def policy(document):
    """The delegating conversation, as the owner states it with an actor; None
    when there is none. Raises :class:`QCRError` for one that cannot hold."""
    if document is None:
        return None
    if not isinstance(document, dict):
        raise QCRError("invalid_message", "An agent policy is an object.")
    made = {}
    for key in ("session", "connection", "folder", "mode", "others"):
        value = document.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise QCRError("invalid_message", "An agent policy names its %s." % key)
        made[key] = value
    if made["mode"] not in MODES or made["others"] not in ("auto", "approve", "deny"):
        raise QCRError("invalid_message", "An agent policy's mode is not one QAVector knows.")
    if not os.path.isabs(made["folder"]):
        raise QCRError("invalid_message", "An agent policy's folder is an absolute path.")
    restrictions = document.get("restrictions") or {}
    if not isinstance(restrictions, dict):
        raise QCRError("invalid_message", "An agent policy's restrictions are an object.")
    made["restrictions"] = restrictions
    made["name"] = str(document.get("name") or made["connection"])[:200]
    made["wake"] = document.get("wake") if document.get("wake") in WAKES else "ask"
    return made


class Delegation:
    """The trusted invoker behind ``agents.<target>.delegate`` and ``agents.tasks.verify``."""

    def __init__(self, registry, engine, executions, project_id, sessions_root, agents_path,
                 policies, notify, context_dir, known_secrets=lambda: (), messages=None,
                 grant=None, revoke=None):
        self.registry, self.engine, self.store = registry, engine, executions
        self.project_id, self.root, self.agents_path = project_id, sessions_root, agents_path
        self._policies = policies          # actor -> policy, kept by the host
        self._notify = notify
        self.context_dir = context_dir
        self._known_secrets = known_secrets
        self.messages = messages
        self._grant = grant                # (actor, grants) -> access for a child turn, or None
        self._revoke = revoke
        self._owner = object()
        self._lock = threading.RLock()
        self._pending = {}                 # execution id -> (job, context): asked, not answered
        self._running = {}                 # execution id -> (stop, thread, parent session)
        self._closed = False
        self._catalogs = {}                # connection id -> (when read, [model rows]) from its program
        self._catalog_lock = threading.Lock()
        self._blocked = {}                 # execution id -> (job, context): approved, waiting to start
        self._follow = {}                  # a task that asked -> the execution its answer runs as (ADR-011)
        self._by_person = {}               # (actor, task) -> who: the next reply is the person's
        self._stopped = {}                 # a task the person stopped -> by whom: no more answers
        #: What the delegating conversations handed on, and when to wake them (ADR-009).
        self.rounds = Rounds(notify, self._summary)
        # Ends are taken off the Engine's own notification, one at a time, in order.
        self._ends = queue.Queue()
        threading.Thread(target=self._take_ends, name="qcr-delegation-ends", daemon=True).start()

    # -- what is offered -------------------------------------------------------
    def configure(self):
        """Register every connection as a target, and the task tools. Again
        whenever the connections change; nothing running is touched."""
        registrations = []
        for connection in store.load(self.agents_path):
            ref = ResourceRef(self.project_id, target_resource(connection["id"]), "agent_connection")
            name = "%s (%s)" % (connection["name"], adapters.get(connection["kind"]).NAME) \
                if connection["kind"] in adapters.KINDS else connection["name"]
            capability = CapabilityDescriptor(
                "agents.%s.delegate" % connection["id"], ref, 1,
                "Hand this agent one task in your folder; it runs once and answers. Give goal, "
                "acceptance_criteria, summary, files, runs; model, effort to suit it (see describe); "
                "a name, and after: names it must wait for.",
                DELEGATE_INPUT, DELEGATE_OUTPUT, PERMISSION, risk_level="high", mode="long_running",
                supports_cancel=True, supports_idempotency=True, timeout_seconds=MAX_SECONDS, exclusive=False)
            describe = CapabilityDescriptor(
                "agents.%s.describe" % connection["id"], ref, 1,
                "What this agent is for, and the models and efforts it offers with its defaults: read it "
                "before delegating, to choose a model and effort that fit the task.",
                {"type": "object", "additionalProperties": False, "properties": {}}, DESCRIBE_OUTPUT,
                PERMISSION, read_only=True)
            registrations.append(Registration(ResourceDescriptor(ref, "agents", name, PERMISSION),
                                              (capability, describe), lambda ref, names: {},
                                              read_capability=self._describe(connection["id"]), invoker=self))
            if self.messages is not None:
                ref = ResourceRef(self.project_id, message_resource(connection["id"]), "agent_messages")
                message = CapabilityDescriptor(
                    "agents.%s.message" % connection["id"], ref, 1,
                    "Send this agent a message; it does not start work unless expects_reply asks for an "
                    "answer (one read-only turn). reply_to answers a message it sent you.",
                    MESSAGE_INPUT, {"type": "object"}, MESSAGE_PERMISSION, risk_level="medium",
                    mode="long_running", supports_cancel=True, supports_idempotency=True,
                    timeout_seconds=MAX_SECONDS)
                registrations.append(Registration(
                    ResourceDescriptor(ref, "agents", "Messages to " + name, MESSAGE_PERMISSION),
                    (message,), lambda ref, names: {}, invoker=self))
        try:
            usable = roles.usable(self.agents_path)
        except roles.RoleError:
            usable = []
        if usable:
            ref = ResourceRef(self.project_id, ROLES, "agent_roles")
            caps = []
            for role, connection in usable:
                caps.append(CapabilityDescriptor(
                    "agents.roles.%s.delegate" % role["id"], ref, 1,
                    ("Role %s: %s" % (role["name"], role["does"]))[:200] + " - same task fields as a connection.",
                    DELEGATE_INPUT, DELEGATE_OUTPUT, PERMISSION, risk_level="high", mode="long_running",
                    supports_cancel=True, supports_idempotency=True, timeout_seconds=MAX_SECONDS, exclusive=False))
                caps.append(CapabilityDescriptor(
                    "agents.roles.%s.describe" % role["id"], ref, 1,
                    "Role %s in full: what it does, its program, model, effort, mode, instructions, verdict."
                    % role["name"][:60],
                    {"type": "object", "additionalProperties": False, "properties": {}}, {"type": "object"},
                    PERMISSION, read_only=True))
            registrations.append(Registration(ResourceDescriptor(ref, "agents", "Roles", PERMISSION), tuple(caps),
                                              lambda ref, names: {}, read_capability=self._describe_role,
                                              invoker=self))
        ref = ResourceRef(self.project_id, TASKS, "agent_tasks")
        verify = CapabilityDescriptor(
            "agents.tasks.verify", ref, 1,
            "Record whether one acceptance criterion of a task you delegated passed, with the "
            "execution that shows it. A task is verified when every criterion passed.",
            VERIFY_INPUT, {"type": "object"}, PERMISSION, risk_level="low", timeout_seconds=30,
            exclusive=False)
        wake = CapabilityDescriptor(
            "agents.tasks.wake_when", ref, 1,
            "Say when this conversation is to be woken with results: all or any of the named tasks and "
            "questions ended; on_failure also at once when one fails. Without it: when all of a turn's end.",
            WAKE_INPUT, {"type": "object"}, PERMISSION, risk_level="low", timeout_seconds=30, exclusive=False)
        reply = CapabilityDescriptor(
            "agents.tasks.reply", ref, 1,
            "Answer the question a task you delegated asked: it goes on in its own conversation, keeping what "
            "it has read. Do not delegate it again.",
            REPLY_INPUT, DELEGATE_OUTPUT, PERMISSION, risk_level="medium", mode="long_running",
            supports_cancel=True, supports_idempotency=True, timeout_seconds=MAX_SECONDS, exclusive=False)
        registrations.append(Registration(ResourceDescriptor(ref, "agents", "Delegated tasks", PERMISSION),
                                          (verify, wake, reply), lambda ref, names: {}, invoker=self))
        self.registry.replace(self._owner, registrations)

    # -- the Engine's invoker interface ----------------------------------------
    def requirements(self, capability, request):
        if self._closed:
            raise QCRError("source_unavailable", "Delegation is closed.")
        if capability.capability_id.endswith(".delegate"):
            if not request["goal"].strip():
                raise QCRError("invalid_input", "A task needs a goal.")
            role, connection_id = self._role(capability.capability_id)
            self._chosen(connection_id, dict(request, model=request.get("model") or (role or {}).get("model"),
                                             effort=request.get("effort") or (role or {}).get("effort")))
            # Before the Engine writes the request down: a secret the agent put in
            # its task must not land in the journal either (ADR-006 D7).
            packages.refuse_secrets([("goal", request["goal"]),
                                     ("acceptance criteria", request.get("acceptance_criteria") or []),
                                     ("summary", request.get("summary") or ""),
                                     ("file list", request.get("files") or [])], self._secrets())
        if capability.capability_id == "agents.tasks.reply":
            if not request["answer"].strip():
                raise QCRError("invalid_input", "An answer says something.")
            packages.refuse_secrets([("answer", request["answer"])], self._secrets())
        if capability.capability_id.endswith(".message"):
            if not request["body"].strip():
                raise QCRError("invalid_input", "A message needs a body.")
            self._chosen(capability.capability_id.split(".")[1], request)
            packages.refuse_secrets([("message", request["body"])], self._secrets())
        return ()

    # -- what a target offers: models and efforts ---------------------------------
    def warm(self, connection_id, fetch=None):
        """Read the target's model list from its program unless it was read
        lately. Slow (it runs the program): never on the host's lock."""
        with self._catalog_lock:
            known = self._catalogs.get(connection_id)
        if known is not None and time.time() - known[0] < CATALOG_SECONDS:
            return known[1]
        connection = store.get(connection_id, self.agents_path)
        if connection is None or connection["kind"] not in adapters.KINDS:
            return None
        try:
            found = (fetch or adapters.get(connection["kind"]).models)(connection.get("binary"))
        except Exception:
            found = {"ok": False}
        if not found.get("ok"):
            return None
        rows = [{key: one[key] for key in ("value", "label", "description", "efforts") if key in one}
                for one in found.get("models") or [] if isinstance(one, dict) and one.get("value")]
        with self._catalog_lock:
            self._catalogs[connection_id] = (time.time(), rows)
        return rows

    def catalog(self, connection_id):
        """The target's models as last read and still believed, or None."""
        with self._catalog_lock:
            known = self._catalogs.get(connection_id)
        return known[1] if known is not None and time.time() - known[0] < CATALOG_SECONDS else None

    def _describe(self, connection_id):
        def read(capability_id, version, request):
            connection = store.get(connection_id, self.agents_path)
            if connection is None:
                raise QCRError("not_found", "That agent connection is gone.")
            adapter = adapters.get(connection["kind"])
            models = self.catalog(connection_id)
            return {"id": connection_id, "name": connection["name"], "provider": adapter.NAME,
                    "role": connection.get("role") or "", "default_model": connection.get("model") or "",
                    "default_effort": connection.get("effort") or "", "efforts": list(adapter.EFFORTS),
                    "models": [{key: (list(value) if key == "efforts" else str(value)[:4000])
                                for key, value in one.items()} for one in models or []][:200],
                    "models_known": models is not None}
        return read

    def _describe_role(self, capability_id, version, request):
        role_id, _ = split_target(capability_id)
        found = next(((role, connection) for role, connection in roles.usable(self.agents_path)
                      if role["id"] == role_id), None)
        if found is None:
            raise QCRError("not_found", "That role is gone.")
        role, connection = found
        described = self._describe(connection["id"])(capability_id, version, request)
        return dict(described, role={key: role[key] for key in ("id", "name", "does", "model", "effort", "mode",
                                                                "instructions", "verdict")})

    def _role(self, capability_id):
        """``(role or None, connection id)`` a task's capability names."""
        target, is_role = split_target(capability_id)
        if not is_role:
            return None, target
        role = roles.get(target, self.agents_path)
        if role is None:
            raise QCRError("not_found", "That role is gone.")
        return role, role["connection"]

    def _chosen(self, target, request):
        """A model and effort the target offers - checked before anything is
        written; one it does not is refused with what it does offer."""
        model, effort = request.get("model") or "", request.get("effort") or ""
        if not model and not effort:
            return
        connection = store.get(target, self.agents_path)
        if connection is None:
            raise QCRError("not_found", "That agent connection is gone.")
        adapter = adapters.get(connection["kind"])
        if effort and effort not in adapter.EFFORTS:
            raise QCRError("invalid_input", "%s offers the efforts %s." % (adapter.NAME, ", ".join(adapter.EFFORTS)))
        models = self.catalog(target)
        if model and models is not None:
            row = next((one for one in models if one["value"] == model), None)
            if row is None:
                raise QCRError("invalid_input", "%s offers the models %s." % (
                    connection["name"], ", ".join(one["value"] for one in models[:40])))
            if effort and row.get("efforts") and effort not in row["efforts"]:
                raise QCRError("invalid_input", "%s takes the efforts %s." % (model, ", ".join(row["efforts"])))

    def _checked_late(self, context):
        """A model chosen while the target's list was not read yet: read now -
        off the host's lock, before the child starts - and checked."""
        if context.get("model") and self.catalog(context["target"]) is None:
            self.warm(context["target"])
            self._chosen(context["target"], {"model": context["model"], "effort": context.get("effort")})

    def timeout(self, capability, request):
        if capability.capability_id == "agents.tasks.verify":
            return None
        if capability.capability_id == "agents.tasks.reply":
            # The task's own time, again for each turn it is answered.
            try:
                return (self.store.get(request["task"]).get("input") or {}).get("timeout_seconds", DEFAULT_SECONDS)
            except Exception:
                return DEFAULT_SECONDS
        return request.get("timeout_seconds", DEFAULT_SECONDS)

    def start(self, job):
        if job.capability.capability_id == "agents.tasks.verify":
            self._verify(job)
            return
        if job.capability.capability_id == "agents.tasks.wake_when":
            self._wake_when(job)
            return
        if job.capability.capability_id == "agents.tasks.reply":
            self._reply(job)
            return
        if job.capability.capability_id.endswith(".message"):
            self._message(job)
            return
        try:
            context = self._context(job)
            after = list(job.request.get("after") or [])
            context["after"] = list(zip(after, self.rounds.resolve(context["parent"], after)))
        except (QCRError, RoundError) as exc:
            job.fail(exc.code, str(exc))
            return
        with self._lock:
            if self._closed:
                job.fail("source_unavailable", "Delegation is closed.")
                return
            busy = self._unfinished(context["parent"])
            if busy >= MAX_CHILDREN:
                job.fail("limit_exceeded", "This conversation already has %d tasks with other agents."
                         % MAX_CHILDREN)
                return
            try:
                self.rounds.add(context["parent"], job.execution_id, job.request.get("name") or "", "task")
            except RoundError as exc:
                job.fail(exc.code, str(exc))
                return
            if context["others"] == "approve":
                self._pending[job.execution_id] = (job, context)
        card = {"target": context["target"], "target_name": context["target_name"],
                "goal": context["goal"], "mode": context["mode"], "session": context["parent"],
                "context_package": context["package"], "files": len(context["document"]["files"]),
                "artifacts": len(context["document"]["artifacts"]),
                "model": context["model"], "effort": context["effort"],
                "name": job.request.get("name") or "", "after": [name for name, _ in context["after"]],
                "role": (context.get("role") or {}).get("name") or ""}
        if context["others"] == "approve":
            if not job.dispatched({"approval": "requested"}) or not job.progress("approval.requested", card):
                with self._lock:
                    self._pending.pop(job.execution_id, None)
                return
            self._notify(dict(card, type="agent_approval", execution_id=job.execution_id,
                              actor=job.actor_id))
            return
        if job.dispatched({"approval": "not_needed"}):
            self._start_when_ready(job, context)

    def answer(self, execution_id, approved, by=""):
        """The person's word on a delegation that waits for it. False when none waits."""
        with self._lock:
            slot = self._pending.pop(execution_id, None)
        if slot is None:
            return False
        job, context = slot
        by = str(by or "the person")[:200]
        if not approved:
            if context.get("kind") == "message":
                self._message_state(context["message_id"], "denied", {"by": by})
            job.fail("denied", "Denied by %s." % by)
            return True
        if job.progress("approval.answered", {"approved": True, "by": by}):
            if context.get("kind") == "message":
                self._deliver(job, context)
            else:
                self._start_when_ready(job, context)
        return True

    def cancel(self, job):
        with self._lock:
            waiting = self._pending.pop(job.execution_id, None)
            self._blocked.pop(job.execution_id, None)
            slot = self._running.get(job.execution_id)
        if waiting and waiting[1].get("kind") == "message":
            self._message_state(waiting[1]["message_id"], "failed", {"reason": "canceled"})
        if slot:
            slot[0].set()

    def close(self):
        with self._lock:
            self._closed = True
            pending = list(self._pending.values()) + list(self._blocked.values())
            self._pending.clear()
            self._blocked.clear()
            running = list(self._running.values())
        for job, context in pending:
            if context.get("kind") == "message":
                self._message_state(context["message_id"], "failed", {"reason": "closed"})
            job.fail("source_unavailable", "QAVector closed before the person answered.")
        self._ends.put(None)
        for stop, _thread, _parent, _target in running:
            stop.set()
        for _stop, thread, _parent, _target in running:
            thread.join(runner.STOP_GRACE + 2)

    # -- order: what waits for others, and for a free agent (ADR-009) -------------
    def _unfinished(self, parent):
        return (sum(1 for slot in self._running.values() if slot[2] == parent)
                + sum(1 for _job, other in self._pending.values() if other["parent"] == parent)
                + sum(1 for _job, other in self._blocked.values() if other["parent"] == parent))

    def _start_when_ready(self, job, context):
        """Start it now, or keep it until the tasks it is ``after`` succeed and its
        agent has a free place; one of those failing skips it."""
        states = {name: self.rounds.state_of(self.latest(execution_id))
                  for name, execution_id in context.get("after") or ()}
        failed = [name for name, state in states.items() if state not in ("", "succeeded", ASKS)]
        if failed:
            job.fail("skipped", "Not run: %s did not succeed." % ", ".join(failed))
            return
        # One that asks is not done: what is after it waits for its answer and the rest.
        waiting = [name for name, state in states.items() if state != "succeeded"]
        with self._lock:
            if self._closed:
                job.fail("source_unavailable", "Delegation is closed.")
                return
            busy = sum(1 for slot in self._running.values() if slot[3] == context["target"])
            if waiting or busy >= MAX_PER_TARGET:
                first = job.execution_id not in self._blocked
                self._blocked[job.execution_id] = (job, context)
            else:
                self._blocked.pop(job.execution_id, None)
                first = None
        if first is None:
            self._launch(job, context)
        elif first:
            job.progress("waiting", {"after": waiting, "agent_busy": not waiting})

    def _unblock(self):
        with self._lock:
            blocked = list(self._blocked.values())
        for job, context in blocked:
            if job.finished:
                with self._lock:
                    self._blocked.pop(job.execution_id, None)
                continue
            self._start_when_ready(job, context)

    def execution_changed(self, execution):
        """Every execution the Engine reports: a task or question that ended is
        taken in order, off the Engine's own notification."""
        if execution.get("state") in TERMINAL and str(execution.get("capability_id") or "").startswith("agents."):
            state = execution["state"]
            output = execution.get("output") or {}
            if state == "succeeded" and output.get("needs_input"):
                state = ASKS                # waits for an answer (ADR-011)
            elif state == "succeeded" and output.get("verdict") in ("rework", "no_verdict"):
                # It ran fine, and said no: what is after it does not start (ADR-010 D4).
                state = "rework"
            self._ends.put(("ended", execution["execution_id"], state))

    def _take_ends(self):
        while True:
            item = self._ends.get()
            if item is None:
                return
            try:
                if item[0] == "ended":
                    self.rounds.ended(item[1], item[2])
                self._unblock()
            except Exception:              # one bad end must not stop the rest
                import logging
                logging.getLogger("qcr.delegation").exception("taking an end failed")

    def _wake_when(self, job):
        rule = self._policies.get(job.actor_id)
        if rule is None or rule["others"] == "deny":
            job.fail("authorization_denied", "This conversation does not work with other agents.")
            return
        request = job.request
        if ("all" in request) == ("any" in request):
            job.fail("invalid_input", "Say either all or any.")
            return
        kind = "all" if "all" in request else "any"
        try:
            made = self.rounds.wake_when(rule["session"], kind, list(request[kind]), request.get("on_failure", False))
        except RoundError as exc:
            job.fail(exc.code, str(exc))
            return
        job.succeed({"condition": made, kind: list(request[kind]), "on_failure": bool(request.get("on_failure")),
                     "wake": rule.get("wake", "ask")})

    def _summary(self, execution_id):
        """One ended task or question, as the woken agent is told it (ADR-009 D4)."""
        try:
            execution = self.store.get(execution_id)
        except Exception:
            return {"execution_id": execution_id, "state": "unknown"}
        request, output = execution.get("input") or {}, execution.get("output") or {}
        error = execution.get("error") or {}
        target_id, is_role = split_target(str(execution.get("capability_id") or "agents.?.?"))
        role = roles.get(target_id, self.agents_path) if is_role else None
        target = [role["connection"]] if role else ([target_id] if not is_role else [])
        connection = store.get(target[0], self.agents_path) if target else None
        if execution.get("capability_id") == "agents.tasks.reply":
            # An answered task going on: told as the task it is (ADR-011).
            root = self._root_input(output.get("root") or "")
            request = dict(root, name=root.get("name") or "")
            target = [output["target"]] if output.get("target") else []
            connection = store.get(target[0], self.agents_path) if target else None
            role = roles.get(output["role"], self.agents_path) if output.get("role") else None
        question = str(execution.get("capability_id") or "").endswith(".message")
        answer = ((output.get("reply") or {}).get("body") if question else output.get("answer")) or ""
        state = error.get("code") if error.get("code") in ("skipped", "denied") else execution["state"]
        if state == "succeeded" and output.get("needs_input"):
            state = ASKS
        elif state == "succeeded" and output.get("verdict") in ("rework", "no_verdict"):
            state = "rework"
        return {"execution_id": execution_id, "name": request.get("name") or "",
                "kind": "question" if question else "task",
                "target": target[0] if target else "", "target_name": (connection or {}).get("name") or "",
                "goal": str(request.get("goal") or request.get("body") or "")[:500],
                "question": output.get("question") or "", "asked": int(output.get("asked") or 0),
                "root": output.get("root") or "",
                "model": request.get("model") or "", "effort": request.get("effort") or "",
                "state": state, "answer": str(answer)[:2000], "error": str(error.get("message") or "")[:500],
                "acceptance": output.get("acceptance") or [],
                "files_changed": _count_files((output.get("changes") or {}).get("files")),
                "session_id": output.get("session_id") or "",
                "role": (role or {}).get("name") or "", "verdict": output.get("verdict") or "",
                "issues": list(output.get("issues") or [])}

    # -- one delegation ----------------------------------------------------------
    def _context(self, job):
        """Everything the child needs, checked before anything is asked or run."""
        rule = self._policies.get(job.actor_id)
        role, target = self._role(job.capability.capability_id)
        if rule is None or rule["others"] == "deny":
            raise QCRError("authorization_denied", "This conversation does not work with other agents.")
        # Its own connection too - another model of the same program, say (ADR-009):
        # the child gets no agents.*, so it cannot hand anything on in turn.
        connection = store.get(target, self.agents_path)
        if connection is None:
            raise QCRError("not_found", "That agent connection is gone.")
        adapter = adapters.get(connection["kind"])
        mode = job.request.get("mode") or (role or {}).get("mode") or rule["mode"]
        if role is not None and MODES.index(mode) > MODES.index(role["mode"]):
            raise QCRError("mode_exceeds_role", "The role %s works at most in %s." % (role["id"], role["mode"]))
        if role is not None and MODES.index(mode) > MODES.index(rule["mode"]):
            mode = rule["mode"]                 # a role never gets more than the conversation has
        if MODES.index(mode) > MODES.index(rule["mode"]):
            raise QCRError("mode_exceeds_parent", "A task cannot be given more than %s; this "
                           "conversation has %s." % (mode, rule["mode"]))
        if mode not in adapter.MODES:
            raise QCRError("unsupported", "%s has no %s mode." % (adapter.NAME, mode))
        if not adapter.resolve(connection.get("binary")):
            raise QCRError("not_installed", "%s is not installed." % adapter.NAME)
        if not os.path.isdir(rule["folder"]):
            raise QCRError("invalid_input", "The conversation's folder is gone.")
        try:
            runner.effective_restrictions({"folder": rule["folder"], "restrictions": rule["restrictions"]})
        except runner.Refused as exc:
            raise QCRError(exc.kind, str(exc)) from None
        artifacts = [self._own(job, one) for one in dict.fromkeys(job.request.get("artifacts") or [])]
        handed = child_grants(rule.get("grants"), connection)
        document = packages.build(
            goal=job.request["goal"].strip(), criteria=list(job.request.get("acceptance_criteria") or []),
            summary=str(job.request.get("summary") or "").strip(),
            constraints={"mode": mode, "timeout_seconds": job.request.get("timeout_seconds", DEFAULT_SECONDS),
                         "depth": 1, "qavector_access": sorted({one["permission"] for one in handed})},
            folder=rule["folder"], files=job.request.get("files") or (), artifacts=artifacts,
            provenance={"actor": job.actor_id, "session": rule["session"], "connection": rule["connection"],
                        "execution_id": job.execution_id},
            known_secrets=self._secrets())
        return {"target": target, "target_name": connection["name"], "provider": connection["kind"],
                "model": job.request.get("model") or (role or {}).get("model") or "",
                "effort": job.request.get("effort") or (role or {}).get("effort") or "",
                "role": role,
                "parent": rule["session"], "parent_name": rule["name"], "folder": rule["folder"],
                "restrictions": rule["restrictions"], "mode": mode, "others": rule["others"],
                "goal": document["goal"], "criteria": document["acceptance_criteria"],
                "package": packages.keep(document, self.context_dir), "document": document,
                "grants": handed}

    def _secrets(self):
        try:
            return list(self._known_secrets() or ())
        except Exception:
            # Unreadable secrets must not let a package through unchecked.
            raise QCRError("source_unavailable", "The secrets store could not be read to check the task.")

    def _launch(self, job, context, work=None):
        stop = threading.Event()
        thread = threading.Thread(target=work or self._run, args=(job, context, stop), daemon=True)
        with self._lock:
            if self._closed:
                job.fail("source_unavailable", "Delegation is closed.")
                return
            self._running[job.execution_id] = (stop, thread, context["parent"], context["target"])
        thread.start()

    def _run(self, job, context, stop):
        try:
            if job.finished or stop.is_set():
                return
            connection = store.get(context["target"], self.agents_path)
            if connection is None or connection["kind"] != context["provider"]:
                raise QCRError("source_unavailable", "That agent connection has changed.")
            self._checked_late(context)
            if context.get("session_id"):
                # An answer: the same conversation goes on (ADR-011 D2).
                session = sessions.load(self.root, context["session_id"])
            else:
                session = sessions.create(
                    self.root, connection, context["folder"], context["mode"],
                    title=context["goal"], restrictions=context["restrictions"], others="deny",
                    source=dict({"label": "Delegated", "ref": "qcr-task:" + job.execution_id, "record": True,
                                 "detail": "%s: %s" % (context["parent_name"], context["goal"])[:300],
                                 "parent": context["parent"], "by": context["parent_name"]},
                                **({"role": context["role"]["id"]} if context.get("role") else {})))
            task_id = job.execution_id
            metadata = {"task_id": task_id, "session_id": session["id"], "provider": context["provider"],
                        "target": context["target"], "parent_session": context["parent"]}
            if not job.running(metadata) or stop.is_set():
                return
            role = context.get("role")
            if context.get("answer") is not None:
                stale = []
                message = "%s\n\n%s\n\n%s" % (ANSWERED % context["by"], context["answer"], GO_ON)
                if role and role["verdict"]:
                    message += "\n\n" + roles.VERDICT_ASK
            else:
                # Read again from where it was kept: the bytes the person approved.
                document = packages.load(context["package"], self.context_dir)
                stale = packages.freshness(document, context["folder"])
                message = packages.render(document, sender=context["parent_name"], mode=context["mode"],
                                          changed=stale, closing=CLOSING)
            if role and context.get("answer") is None:
                # How this role works, before the task; a verdict asked for at its end (ADR-010).
                lines = ["Your role: %s - %s" % (role["name"], role["does"])]
                if role["instructions"]:
                    lines.append(role["instructions"])
                message = "\n".join(lines) + "\n\n" + message
                if role["verdict"]:
                    message += "\n\n" + roles.VERDICT_ASK
            access = self._grant("agent:%s/%s" % (context["target"], session["id"]), context["grants"]) \
                if self._grant is not None and context["grants"] else None
            try:
                answer, text = run_turn(job, self.root, self.agents_path, session, context["provider"],
                                        task_id, metadata, message, context["mode"], context["model"],
                                        context["effort"], stop,
                                        qavector={"socket": access["socket"], "token_file": access["token_file"]}
                                        if access else None)
            finally:
                if access and self._revoke is not None:
                    self._revoke(access)
            turn = answer["turn"]
            output = {"task_id": task_id, "target": context["target"], "provider": context["provider"],
                      "session_id": session["id"],
                      "provider_session_id": answer["session"].get("provider_session", ""),
                      "mode": context["mode"], "turn_state": turn["state"],
                      "context_package": context["package"], "stale_files": stale,
                      "answer": text[:ANSWER_CHARS], "changes": turn.get("changes") or {},
                      "artifacts": turn_artifacts(self.root, session["id"], turn),
                      "acceptance": [{"criterion": one, "status": "unverified"} for one in context["criteria"]],
                      "verified": False, "root": context.get("root") or job.execution_id,
                      "asked": int(context.get("asked") or 0)}
            if context.get("answer") is not None:
                output.update(follow_up_of=context["follow_up_of"], by=context["by"], goal=context["goal"])
            asked = question_of(text) if turn["state"] == sessions.COMPLETED else ""
            if context.get("role"):
                output["role"] = context["role"]["id"]
                if context["role"]["verdict"] and not asked:
                    output["verdict"], output["issues"] = verdict_of(text)
            if asked:
                # Counted, never cut off: the person sees how many and stops it (ADR-011 D5).
                output.update(needs_input=True, question=asked, asked=output["asked"] + 1)
            if turn["state"] == sessions.COMPLETED:
                job.succeed(output)
            else:
                job.fail("agent_failed", "The delegated task did not complete.", output=output)
        except (runner.Refused, QCRError, sessions.SessionError) as exc:
            job.fail(getattr(exc, "code", getattr(exc, "kind", "agent_failed")), str(exc))
        except Exception:
            job.fail("agent_failed", "The delegated task could not finish; inspect its conversation.")
        finally:
            with self._lock:
                self._running.pop(job.execution_id, None)
            self._ends.put(("free",))       # its agent has a place again

    # -- answers to what a task asks (ADR-011) --------------------------------------
    def person_says(self, actor, task, by, forget=False):
        """The next reply to ``task`` from ``actor`` is the person's (the owner pipe only)."""
        with self._lock:
            if forget:
                self._by_person.pop((actor, task), None)
            else:
                self._by_person[(actor, task)] = str(by or "You")[:200]

    def stop(self, task_id, by=""):
        """The person stops a task that goes on through answers: no answer is
        taken any more. Returns the execution it is at now, for the host to
        cancel if it still runs."""
        try:
            output = self.store.get(task_id).get("output") or {}
        except QCRError:
            raise QCRError("not_found", "No such task.") from None
        root = output.get("root") or task_id
        with self._lock:
            self._stopped[root] = str(by or "the person")[:200]
        return self.latest(root)

    def latest(self, execution_id):
        """A task as it goes on: the last answer's execution, or itself."""
        seen = set()
        with self._lock:
            while execution_id in self._follow and execution_id not in seen:
                seen.add(execution_id)
                execution_id = self._follow[execution_id]
        return execution_id

    def _root_input(self, root):
        try:
            return dict(self.store.get(root).get("input") or {}) if root else {}
        except Exception:
            return {}

    def _reply(self, job):
        task_id = job.request["task"]
        with self._lock:
            by_person = self._by_person.pop((job.actor_id, task_id), None)
        try:
            rule = self._policies.get(job.actor_id)
            if rule is None:
                raise QCRError("authorization_denied", "This conversation does not work with other agents.")
            asked = self._own(job, task_id)
            output = asked.get("output") or {}
            if asked["capability_id"] != "agents.tasks.reply" and not asked["capability_id"].endswith(".delegate"):
                raise QCRError("invalid_input", "That execution is not a delegated task.")
            root_id = output.get("root") or task_id
            with self._lock:
                answered, stopped = task_id in self._follow, self._stopped.get(root_id)
            if stopped:
                raise QCRError("stopped", "%s stopped this task: it takes no more answers." % stopped.capitalize())
            if answered:
                raise QCRError("not_waiting", "That question was answered already.")
            if asked["state"] != "succeeded" or not output.get("needs_input"):
                raise QCRError("not_waiting", "That task does not wait for an answer.")
            root = self._root_input(root_id)
            connection = store.get(output.get("target") or "", self.agents_path)
            if connection is None or connection["kind"] != output.get("provider"):
                raise QCRError("source_unavailable", "That agent connection has changed.")
            session = sessions.load(self.root, output["session_id"])
            role = roles.get(output["role"], self.agents_path) if output.get("role") else None
        except (QCRError, sessions.SessionError) as exc:
            job.fail(getattr(exc, "code", "not_found"), str(exc))
            return
        context = {"target": output["target"], "target_name": connection["name"], "provider": connection["kind"],
                   "model": root.get("model") or (role or {}).get("model") or "",
                   "effort": root.get("effort") or (role or {}).get("effort") or "", "role": role,
                   "parent": rule["session"], "parent_name": rule["name"], "folder": session["folder"],
                   "restrictions": session.get("restrictions") or {}, "mode": output.get("mode") or session["mode"],
                   "others": rule["others"], "goal": str(root.get("goal") or "")[:GOAL_CHARS],
                   "criteria": list(root.get("acceptance_criteria") or []),
                   "package": output.get("context_package") or "", "session_id": session["id"],
                   "grants": child_grants(rule.get("grants"), connection),
                   "answer": job.request["answer"].strip(), "by": by_person or rule["name"],
                   "follow_up_of": task_id, "root": root_id, "asked": int(output.get("asked") or 0), "after": []}
        with self._lock:
            if self._closed:
                job.fail("source_unavailable", "Delegation is closed.")
                return
            self._follow[task_id] = job.execution_id
            try:
                # It goes on under the task's name: what waits for the task waits for this.
                self.rounds.add(rule["session"], job.execution_id, root.get("name") or "", "task")
            except RoundError as exc:
                self._follow.pop(task_id, None)
                job.fail(exc.code, str(exc))
                return
        if job.dispatched({"answer_by": "person" if by_person else "agent"}):
            job.progress("answered", {"task": task_id, "root": root_id, "by": context["by"],
                                      "answer": context["answer"][:QUESTION_CHARS]})
            self._start_when_ready(job, context)

    # -- the workbench: every task handed on, and its history (ADR-012) -------------
    def board(self, limit=BOARD_LIMIT):
        """Every task and question handed on in the project, newest first: one row
        a task, its answers folded in, its state as the person would act on it."""
        rows = self.store.list(self.project_id, capability_prefix="agents.", limit=limit)
        tasks, root_of, verdicts = {}, {}, {}
        for row in sorted(rows, key=lambda one: one.get("created_at") or 0):
            capability = row["capability_id"]
            output = row.get("output") or {}
            if capability.endswith(".delegate") or (capability.endswith(".message")
                                                    and (row.get("input") or {}).get("expects_reply")):
                root_of[row["execution_id"]] = row["execution_id"]
                tasks[row["execution_id"]] = {"root": row, "chain": [row]}
            elif capability == "agents.tasks.reply":
                root = root_of.get((row.get("input") or {}).get("task")) or output.get("root")
                if root in tasks:
                    root_of[row["execution_id"]] = root
                    tasks[root]["chain"].append(row)
            elif capability == "agents.tasks.verify" and row.get("state") == "succeeded":
                root = root_of.get(output.get("task"))
                if root:
                    verdicts.setdefault(root, {})[output.get("criterion")] = output.get("verdict")
        made = [self._board_row(found, verdicts.get(root, {})) for root, found in tasks.items()]
        return sorted(made, key=lambda one: -(one["updated_at"] or 0))

    def _board_row(self, found, verdicts):
        root, latest = found["root"], found["chain"][-1]
        given, output = root.get("input") or {}, latest.get("output") or {}
        error = latest.get("error") or {}
        target_id, is_role = split_target(root["capability_id"])
        role = roles.get(target_id, self.agents_path) if is_role else None
        connection = store.get(role["connection"] if role else target_id, self.agents_path)
        actor = root.get("actor_id") or ""
        from_connection = actor[len("agent:"):].split("/", 1)[0] if actor.startswith("agent:") else ""
        with self._lock:
            pending = latest["execution_id"] in self._pending
            blocked = latest["execution_id"] in self._blocked
            stopped = self._stopped.get(root["execution_id"])
        state = latest.get("state")
        if pending:
            state = "approval"
        elif blocked:
            state = "waiting"
        elif stopped and state not in ("running", "dispatched", "accepted"):
            state = "stopped"
        elif state == "succeeded" and output.get("needs_input"):
            state = ASKS
        elif state == "succeeded" and output.get("verdict") in ("rework", "no_verdict"):
            state = "rework"
        elif error.get("code") in ("skipped", "denied", "stopped"):
            state = error["code"]
        criteria = list(given.get("acceptance_criteria") or [])
        session = (root.get("output") or {}).get("session_id") or output.get("session_id") or ""
        return {"task": root["execution_id"], "latest": latest["execution_id"],
                "kind": "question" if root["capability_id"].endswith(".message") else "task",
                "state": state, "needs_person": state in ("approval", ASKS),
                "goal": str(given.get("goal") or given.get("body") or "")[:500], "name": given.get("name") or "",
                "target": (connection or {}).get("id") or target_id,
                "target_name": (connection or {}).get("name") or target_id,
                "role": (role or {}).get("name") or "", "model": given.get("model") or "",
                "effort": given.get("effort") or "", "actor": actor,
                "from_connection": from_connection,
                "from_name": (store.get(from_connection, self.agents_path) or {}).get("name") or from_connection,
                "from_session": actor.split("/", 1)[1] if "/" in actor else "",
                "child_session": session, "question": output.get("question") or "",
                "questions": int(output.get("asked") or 0),
                "files_changed": _count_files((output.get("changes") or {}).get("files")),
                "criteria": len(criteria), "passed": sum(1 for one in verdicts.values() if one == "passed"),
                "error": str(error.get("message") or "")[:500],
                "created_at": root.get("created_at"), "updated_at": latest.get("updated_at")}

    def history(self, task_id):
        """One task as it went, oldest first: handed on, approved, waited, each turn,
        each question and answer, each verdict - with what it cost."""
        found = next((row for row in self.board() if row["task"] == task_id), None)
        if found is None:
            raise QCRError("not_found", "No such task.")
        rows = self.store.list(self.project_id, capability_prefix="agents.tasks.", limit=BOARD_LIMIT)
        chain, ids = [self.store.get(task_id)], {task_id}
        for row in sorted(rows, key=lambda one: one.get("created_at") or 0):
            if row["capability_id"] == "agents.tasks.reply" and (row.get("input") or {}).get("task") in ids:
                chain.append(row)
                ids.add(row["execution_id"])
        entries = []
        for number, row in enumerate(chain):
            entries.extend(self._history_of(row, number == 0, found))
        for row in rows:
            output = row.get("output") or {}
            if row["capability_id"] == "agents.tasks.verify" and row.get("state") == "succeeded" \
                    and output.get("task") in ids:
                entries.append({"at": row.get("updated_at"), "kind": "verified", "execution_id": row["execution_id"],
                                "text": "Criterion %d %s" % (int(output.get("criterion") or 0) + 1,
                                                             output.get("verdict")),
                                "evidence": (output.get("evidence") or {}).get("execution_id") or ""})
        entries.sort(key=lambda one: one.get("at") or 0)
        cost = {}
        if found["child_session"]:
            try:
                from integrations.agents import usage
                cost = usage.conversation(self.root, found["child_session"]).get("own") or {}
            except Exception:
                cost = {}
        return {"task": found, "entries": entries, "usage": cost}

    def _history_of(self, row, first, found):
        made = []
        given, output, error = row.get("input") or {}, row.get("output") or {}, row.get("error") or {}
        if first:
            made.append({"at": row.get("created_at"), "kind": "handed", "execution_id": row["execution_id"],
                         "text": given.get("goal") or given.get("body") or "", "by": found["from_name"],
                         "to": found["role"] or found["target_name"]})
        try:
            events = self.store.events(row["execution_id"], limit=EVENT_PAGE)
        except QCRError:
            events = []
        for event in events:
            kind, payload = event.get("type"), event.get("payload") or {}
            if kind == "approval.requested":
                made.append({"at": event.get("at"), "kind": "approval", "text": "Waits for the person's approval."})
            elif kind == "approval.answered":
                made.append({"at": event.get("at"), "kind": "approved", "text": "Approved by %s." % (
                    payload.get("by") or "the person")})
            elif kind == "waiting":
                made.append({"at": event.get("at"), "kind": "waiting", "text": (
                    "Waits for %s to succeed." % ", ".join(payload.get("after") or ()) if payload.get("after")
                    else "Waits for a free place with its agent.")})
            elif kind == "answered":
                made.append({"at": event.get("at"), "kind": "answer", "execution_id": row["execution_id"],
                             "text": payload.get("answer") or "", "by": payload.get("by") or ""})
            elif kind == "state" and event.get("state") == "running":
                made.append({"at": event.get("at"), "kind": "started", "execution_id": row["execution_id"],
                             "text": "%s works on it." % (found["role"] or found["target_name"])})
        state = row.get("state")
        if state in TERMINAL:
            at = row.get("finished_at") or row.get("updated_at")
            if state == "succeeded" and output.get("needs_input"):
                made.append({"at": at, "kind": "asked", "execution_id": row["execution_id"],
                             "text": output.get("question") or "", "number": int(output.get("asked") or 0)})
            elif state == "succeeded":
                made.append({"at": at, "kind": "done", "execution_id": row["execution_id"],
                             "text": str(output.get("answer") or (output.get("reply") or {}).get("body") or "")[:4000],
                             "files_changed": _count_files((output.get("changes") or {}).get("files")),
                             "verdict": output.get("verdict") or ""})
            elif error.get("code") not in ("not_waiting",):
                made.append({"at": at, "kind": "stopped" if error.get("code") == "stopped" or (
                    state == "canceled" and found["state"] == "stopped") else state,
                    "execution_id": row["execution_id"], "text": str(error.get("message") or "")[:1000]})
        return made

    # -- verification ------------------------------------------------------------
    def _verify(self, job):
        request = job.request
        try:
            task = self._own(job, request["task"])
            if not task["capability_id"].endswith(".delegate"):
                raise QCRError("invalid_input", "That execution is not a delegated task.")
            if task["state"] != "succeeded":
                raise QCRError("invalid_input", "Only a task that completed can be verified.")
            criteria = task["input"].get("acceptance_criteria") or []
            if request["criterion"] >= len(criteria):
                raise QCRError("invalid_input", "That task has %d acceptance criteria." % len(criteria))
            evidence = None
            if request.get("evidence"):
                proof = self._own(job, request["evidence"])
                if proof["state"] not in TERMINAL:
                    raise QCRError("invalid_input", "Evidence is an execution that has ended.")
                evidence = {"execution_id": proof["execution_id"], "capability": proof["capability_id"],
                            "state": proof["state"]}
        except QCRError as exc:
            job.fail(exc.code, str(exc))
            return
        verdicts = self.verdicts(task, job.actor_id)
        verdicts[request["criterion"]] = request["verdict"]
        job.succeed({"task": task["execution_id"], "criterion": request["criterion"],
                     "verdict": request["verdict"], "evidence": evidence,
                     "acceptance": [{"criterion": one, "status": verdicts.get(index, "unverified")}
                                    for index, one in enumerate(criteria)],
                     "verified": bool(criteria) and all(verdicts.get(index) == "passed"
                                                        for index in range(len(criteria)))})

    def _own(self, job, execution_id):
        """An execution of this project that ``job``'s actor made, or not_found."""
        try:
            execution = self.store.get(execution_id)
        except QCRError:
            raise QCRError("not_found", "Execution is not available.") from None
        if execution["project_id"] != self.project_id or execution["actor_id"] != job.actor_id:
            raise QCRError("not_found", "Execution is not available.")
        return execution

    def verdicts(self, task, actor_id):
        """criterion index -> the latest verdict its delegator recorded."""
        found = {}
        rows = self.store.list(self.project_id, capability_id="agents.tasks.verify", actor_id=actor_id,
                               limit=1000)
        for row in sorted(rows, key=lambda one: one.get("created_at") or 0):
            output = row.get("output") or {}
            if row.get("state") == "succeeded" and output.get("task") == task["execution_id"]:
                found[output["criterion"]] = output["verdict"]
        return found

    # -- messages (ADR-006 D4) ---------------------------------------------------
    def _message(self, job):
        try:
            context = self._message_context(job)
            message, created = self.messages.create(
                job.execution_id, self.project_id, kind="reply" if context["reply_to"] else "message",
                sender_actor=job.actor_id, sender_session=context["parent"],
                sender_connection=context["sender"], recipient_connection=context["target"],
                recipient_session=context["recipient_session"], body=context["body"],
                expects_reply=context["expects_reply"], reply_to=context["reply_to"],
                conversation_id=context["conversation"])
        except QCRError as exc:
            job.fail(exc.code, str(exc))
            return
        context["message_id"] = message["message_id"]
        if context["expects_reply"]:
            # A question waits for an answer: an item of the round (ADR-009).
            try:
                self.rounds.add(context["parent"], job.execution_id, job.request.get("name") or "", "question")
            except RoundError as exc:
                self._message_state(context["message_id"], "failed", {"reason": "invalid_input"})
                job.fail(exc.code, str(exc))
                return
        card = {"kind": "message", "target": context["target"], "target_name": context["target_name"],
                "body": context["body"][:2000], "expects_reply": context["expects_reply"],
                "reply_to": context["reply_to"], "session": context["parent"],
                "model": context["model"], "effort": context["effort"]}
        with self._lock:
            if self._closed:
                self._message_state(context["message_id"], "failed", {"reason": "closed"})
                job.fail("source_unavailable", "Delegation is closed.")
                return
            if context["others"] == "approve":
                self._pending[job.execution_id] = (job, context)
        if context["others"] == "approve":
            self._message_state(context["message_id"], "awaiting_approval")
            if not job.dispatched({"approval": "requested"}) or not job.progress("approval.requested", card):
                with self._lock:
                    self._pending.pop(job.execution_id, None)
                return
            self._notify(dict(card, type="agent_approval", execution_id=job.execution_id,
                              actor=job.actor_id, message_id=context["message_id"]))
            return
        if job.dispatched({"approval": "not_needed"}):
            self._deliver(job, context)

    def _message_context(self, job):
        rule = self._policies.get(job.actor_id)
        target = job.capability.capability_id.split(".")[1]
        if rule is None or rule["others"] == "deny":
            raise QCRError("authorization_denied", "This conversation does not work with other agents.")
        if target == rule["connection"]:
            raise QCRError("authorization_denied", "An agent does not message its own connection.")
        connection = store.get(target, self.agents_path)
        if connection is None:
            raise QCRError("not_found", "That agent connection is gone.")
        if connection.get("others_default") == "deny":
            raise QCRError("recipient_unavailable", "%s works alone; it takes no messages from other agents."
                           % connection["name"])
        request = job.request
        recipient_session, conversation, reply_to = None, None, request.get("reply_to")
        if reply_to:
            try:
                original = self.messages.get(reply_to)
            except QCRError:
                raise QCRError("not_found", "There is no such message to reply to.") from None
            if (original["project_id"] != self.project_id or original["recipient_connection"] != rule["connection"]
                    or original["sender_connection"] != target):
                raise QCRError("not_found", "There is no such message to reply to.")
            recipient_session, conversation = original["sender_session"], original["conversation_id"]
            try:
                back = sessions.load(self.root, recipient_session)
            except sessions.SessionError:
                back = None
            if back is not None and sessions.others_of(back) == "deny":
                raise QCRError("recipient_unavailable", "That conversation now works alone.")
        expects_reply = bool(request.get("expects_reply"))
        if expects_reply:
            adapter = adapters.get(connection["kind"])
            if not adapter.resolve(connection.get("binary")):
                raise QCRError("not_installed", "%s is not installed." % adapter.NAME)
            if not os.path.isdir(rule["folder"]):
                raise QCRError("invalid_input", "The conversation's folder is gone.")
        if self.messages.sent_since(self.project_id, rule["session"], time.time() - 3600) >= MAX_MESSAGES_PER_HOUR:
            raise QCRError("limit_exceeded", "This conversation has sent %d messages in the last hour."
                           % MAX_MESSAGES_PER_HOUR)
        return {"kind": "message", "target": target, "target_name": connection["name"],
                "model": request.get("model") or "", "effort": request.get("effort") or "",
                "provider": connection["kind"], "parent": rule["session"], "parent_name": rule["name"],
                "sender": rule["connection"], "folder": rule["folder"], "restrictions": rule["restrictions"],
                "mode": "read", "others": rule["others"], "body": request["body"],
                "expects_reply": expects_reply, "reply_to": reply_to,
                "recipient_session": recipient_session, "conversation": conversation}

    def _message_state(self, message_id, state, payload=None):
        try:
            self.messages.transition(message_id, state, payload)
        except QCRError:
            pass

    def _deliver(self, job, context):
        """Into the recipient's inbox; with ``expects_reply``, one read-only turn answers it."""
        self._message_state(context["message_id"], "delivered")
        if context["reply_to"]:
            self._message_state(context["reply_to"], "acknowledged", {"replied_by": context["message_id"]})
        if not context["expects_reply"]:
            job.succeed({"message_id": context["message_id"], "state": "delivered",
                         "conversation_id": context["conversation"] or context["message_id"],
                         "recipient": context["target"]})
            return
        with self._lock:
            busy = sum(1 for slot in self._running.values() if slot[2] == context["parent"])
        if busy >= MAX_CHILDREN:
            self._message_state(context["message_id"], "failed", {"reason": "limit_exceeded"})
            job.fail("limit_exceeded", "This conversation already has %d tasks with other agents." % MAX_CHILDREN)
            return
        self._launch(job, context, self._answer_message)

    def _answer_message(self, job, context, stop):
        message_id = context["message_id"]
        try:
            if job.finished or stop.is_set():
                return
            connection = store.get(context["target"], self.agents_path)
            if connection is None or connection["kind"] != context["provider"]:
                raise QCRError("source_unavailable", "That agent connection has changed.")
            self._checked_late(context)
            session = sessions.create(
                self.root, connection, context["folder"], "read", title=context["body"][:80],
                restrictions=context["restrictions"], others="deny",
                source={"label": "Message", "ref": "qcr-message:" + message_id, "record": True,
                        "detail": "%s asks: %s" % (context["parent_name"], context["body"])[:300],
                        "parent": context["parent"], "by": context["parent_name"]})
            metadata = {"message_id": message_id, "session_id": session["id"], "provider": context["provider"],
                        "target": context["target"], "parent_session": context["parent"]}
            if not job.running(metadata) or stop.is_set():
                return
            told = ("[QAVector: a message from %s, who waits for your answer]\n\n%s\n\n"
                    "Answer it. This turn is read-only and nobody can answer a question back; your "
                    "answer is sent to %s as it is." % (context["parent_name"], context["body"],
                                                        context["parent_name"]))
            answer, text = run_turn(job, self.root, self.agents_path, session, context["provider"],
                                    message_id, metadata, told, "read", context["model"], context["effort"], stop)
            turn = answer["turn"]
            if turn["state"] != sessions.COMPLETED or not text.strip():
                self._message_state(message_id, "failed", {"reason": "no_answer"})
                job.fail("agent_failed", "The agent did not answer the message.",
                         output={"message_id": message_id, "session_id": session["id"],
                                 "turn_state": turn["state"]})
                return
            reply, _created = self.messages.create(
                message_id + ".reply", self.project_id, kind="reply",
                sender_actor="agent:%s/%s" % (context["target"], session["id"]),
                sender_session=session["id"], sender_connection=context["target"],
                recipient_connection=context["sender"], recipient_session=context["parent"],
                body=text[:ANSWER_CHARS], reply_to=message_id,
                conversation_id=context["conversation"] or message_id)
            self._message_state(reply["message_id"], "delivered")
            self._message_state(message_id, "acknowledged", {"replied_by": reply["message_id"]})
            job.succeed({"message_id": message_id, "state": "acknowledged", "session_id": session["id"],
                         "reply": {"message_id": reply["message_id"], "body": reply["body"]},
                         "conversation_id": reply["conversation_id"],
                         "artifacts": turn_artifacts(self.root, session["id"], turn)})
        except (runner.Refused, QCRError, sessions.SessionError) as exc:
            self._message_state(message_id, "failed", {"reason": "error"})
            job.fail(getattr(exc, "code", getattr(exc, "kind", "agent_failed")), str(exc))
        except Exception:
            self._message_state(message_id, "failed", {"reason": "error"})
            job.fail("agent_failed", "The message could not be answered; inspect its conversation.")
        finally:
            with self._lock:
                self._running.pop(job.execution_id, None)
            self._ends.put(("free",))

    def acknowledge(self, message_id, by=""):
        """The person passed a delivered message into a chat (the owner says so)."""
        message = self.messages.get(message_id)
        if message["project_id"] != self.project_id or message["state"] != "delivered":
            raise QCRError("not_found", "No delivered message has that id.")
        return self.messages.transition(message_id, "acknowledged", {"by": str(by or "the person")[:200]})
