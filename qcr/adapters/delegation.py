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
"""

import os
import threading
import time

from integrations.agents import adapters, runner, sessions, store
from qcr import context as packages
from qcr.contracts import CapabilityDescriptor, ResourceDescriptor, ResourceRef
from qcr.errors import QCRError
from qcr.executions import TERMINAL
from qcr.registry import Registration

from .agents import run_turn, turn_artifacts

PERMISSION = "agents.delegate"
MESSAGE_PERMISSION = "agents.message"
TASKS = "agent-tasks"
MODES = ("read", "edit", "full")            # narrowest first: a child never goes higher

#: ADR-006 D6.
MAX_CHILDREN = 3
DEFAULT_SECONDS, MAX_SECONDS = 1800, 21600
GOAL_CHARS, SUMMARY_CHARS, CRITERION_CHARS, MAX_CRITERIA = 2000, 20000, 500, 10
ANSWER_CHARS = 20000
#: ADR-006 D6: messages one conversation may send in an hour.
MAX_MESSAGES_PER_HOUR = 20

_EXECUTION = {"type": "string", "minLength": 1, "maxLength": 200}
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
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_SECONDS}}}
DELEGATE_OUTPUT = {"type": "object"}
MESSAGE_INPUT = {
    "type": "object", "additionalProperties": False, "required": ["body"],
    "properties": {"body": {"type": "string", "minLength": 1, "maxLength": 20000},
                   "expects_reply": {"type": "boolean"}, "reply_to": _EXECUTION}}
VERIFY_INPUT = {
    "type": "object", "additionalProperties": False, "required": ["task", "criterion", "verdict"],
    "properties": {"task": _EXECUTION, "criterion": {"type": "integer", "minimum": 0},
                   "verdict": {"enum": ["passed", "failed"]}, "evidence": _EXECUTION,
                   "note": {"type": "string", "maxLength": 2000}}}

#: What the child is told after the task: the turn is all it has.
CLOSING = ("Work only in this folder. Nobody can answer a question during this task: if "
           "something blocks you, say what and stop. End with what you changed and how you "
           "checked it.")


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
                "acceptance_criteria, a summary, files and your runs it needs. Follow with qavector_wait.",
                DELEGATE_INPUT, DELEGATE_OUTPUT, PERMISSION, risk_level="high", mode="long_running",
                supports_cancel=True, supports_idempotency=True, timeout_seconds=MAX_SECONDS)
            registrations.append(Registration(ResourceDescriptor(ref, "agents", name, PERMISSION),
                                              (capability,), lambda ref, names: {}, invoker=self))
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
        ref = ResourceRef(self.project_id, TASKS, "agent_tasks")
        verify = CapabilityDescriptor(
            "agents.tasks.verify", ref, 1,
            "Record whether one acceptance criterion of a task you delegated passed, with the "
            "execution that shows it. A task is verified when every criterion passed.",
            VERIFY_INPUT, {"type": "object"}, PERMISSION, risk_level="low", timeout_seconds=30)
        registrations.append(Registration(ResourceDescriptor(ref, "agents", "Delegated tasks", PERMISSION),
                                          (verify,), lambda ref, names: {}, invoker=self))
        self.registry.replace(self._owner, registrations)

    # -- the Engine's invoker interface ----------------------------------------
    def requirements(self, capability, request):
        if self._closed:
            raise QCRError("source_unavailable", "Delegation is closed.")
        if capability.capability_id.endswith(".delegate"):
            if not request["goal"].strip():
                raise QCRError("invalid_input", "A task needs a goal.")
            # Before the Engine writes the request down: a secret the agent put in
            # its task must not land in the journal either (ADR-006 D7).
            packages.refuse_secrets([("goal", request["goal"]),
                                     ("acceptance criteria", request.get("acceptance_criteria") or []),
                                     ("summary", request.get("summary") or ""),
                                     ("file list", request.get("files") or [])], self._secrets())
        if capability.capability_id.endswith(".message"):
            if not request["body"].strip():
                raise QCRError("invalid_input", "A message needs a body.")
            packages.refuse_secrets([("message", request["body"])], self._secrets())
        return ()

    def timeout(self, capability, request):
        if capability.capability_id == "agents.tasks.verify":
            return None
        return request.get("timeout_seconds", DEFAULT_SECONDS)

    def start(self, job):
        if job.capability.capability_id == "agents.tasks.verify":
            self._verify(job)
            return
        if job.capability.capability_id.endswith(".message"):
            self._message(job)
            return
        try:
            context = self._context(job)
        except QCRError as exc:
            job.fail(exc.code, str(exc))
            return
        with self._lock:
            if self._closed:
                job.fail("source_unavailable", "Delegation is closed.")
                return
            busy = sum(1 for slot in self._running.values() if slot[2] == context["parent"]) + sum(
                1 for _job, other in self._pending.values() if other["parent"] == context["parent"])
            if busy >= MAX_CHILDREN:
                job.fail("limit_exceeded", "This conversation already has %d tasks with other agents."
                         % MAX_CHILDREN)
                return
            if context["others"] == "approve":
                self._pending[job.execution_id] = (job, context)
        card = {"target": context["target"], "target_name": context["target_name"],
                "goal": context["goal"], "mode": context["mode"], "session": context["parent"],
                "context_package": context["package"], "files": len(context["document"]["files"]),
                "artifacts": len(context["document"]["artifacts"])}
        if context["others"] == "approve":
            if not job.dispatched({"approval": "requested"}) or not job.progress("approval.requested", card):
                with self._lock:
                    self._pending.pop(job.execution_id, None)
                return
            self._notify(dict(card, type="agent_approval", execution_id=job.execution_id,
                              actor=job.actor_id))
            return
        if job.dispatched({"approval": "not_needed"}):
            self._launch(job, context)

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
                self._launch(job, context)
        return True

    def cancel(self, job):
        with self._lock:
            waiting = self._pending.pop(job.execution_id, None)
            slot = self._running.get(job.execution_id)
        if waiting and waiting[1].get("kind") == "message":
            self._message_state(waiting[1]["message_id"], "failed", {"reason": "canceled"})
        if slot:
            slot[0].set()

    def close(self):
        with self._lock:
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
            running = list(self._running.values())
        for job, context in pending:
            if context.get("kind") == "message":
                self._message_state(context["message_id"], "failed", {"reason": "closed"})
            job.fail("source_unavailable", "QAVector closed before the person answered.")
        for stop, _thread, _parent in running:
            stop.set()
        for _stop, thread, _parent in running:
            thread.join(runner.STOP_GRACE + 2)

    # -- one delegation ----------------------------------------------------------
    def _context(self, job):
        """Everything the child needs, checked before anything is asked or run."""
        rule = self._policies.get(job.actor_id)
        target = job.capability.capability_id.split(".")[1]
        if rule is None or rule["others"] == "deny":
            raise QCRError("authorization_denied", "This conversation does not work with other agents.")
        if target == rule["connection"]:
            raise QCRError("authorization_denied", "An agent does not delegate to its own connection.")
        connection = store.get(target, self.agents_path)
        if connection is None:
            raise QCRError("not_found", "That agent connection is gone.")
        adapter = adapters.get(connection["kind"])
        mode = job.request.get("mode") or rule["mode"]
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
            self._running[job.execution_id] = (stop, thread, context["parent"])
        thread.start()

    def _run(self, job, context, stop):
        try:
            if job.finished or stop.is_set():
                return
            connection = store.get(context["target"], self.agents_path)
            if connection is None or connection["kind"] != context["provider"]:
                raise QCRError("source_unavailable", "That agent connection has changed.")
            session = sessions.create(
                self.root, connection, context["folder"], context["mode"],
                title=context["goal"], restrictions=context["restrictions"], others="deny",
                source={"label": "Delegated", "ref": "qcr-task:" + job.execution_id, "record": True,
                        "detail": "%s: %s" % (context["parent_name"], context["goal"])[:300],
                        "parent": context["parent"], "by": context["parent_name"]})
            task_id = job.execution_id
            metadata = {"task_id": task_id, "session_id": session["id"], "provider": context["provider"],
                        "target": context["target"], "parent_session": context["parent"]}
            if not job.running(metadata) or stop.is_set():
                return
            # Read again from where it was kept: the bytes the person approved.
            document = packages.load(context["package"], self.context_dir)
            stale = packages.freshness(document, context["folder"])
            message = packages.render(document, sender=context["parent_name"], mode=context["mode"],
                                      changed=stale, closing=CLOSING)
            access = self._grant("agent:%s/%s" % (context["target"], session["id"]), context["grants"]) \
                if self._grant is not None and context["grants"] else None
            try:
                answer, text = run_turn(job, self.root, self.agents_path, session, context["provider"],
                                        task_id, metadata, message, context["mode"], "", "", stop,
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
                      "verified": False}
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
        card = {"kind": "message", "target": context["target"], "target_name": context["target_name"],
                "body": context["body"][:2000], "expects_reply": context["expects_reply"],
                "reply_to": context["reply_to"], "session": context["parent"]}
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
                                    message_id, metadata, told, "read", "", "", stop)
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

    def acknowledge(self, message_id, by=""):
        """The person passed a delivered message into a chat (the owner says so)."""
        message = self.messages.get(message_id)
        if message["project_id"] != self.project_id or message["state"] != "delivered":
            raise QCRError("not_found", "No delivered message has that id.")
        return self.messages.transition(message_id, "acknowledged", {"by": str(by or "the person")[:200]})
