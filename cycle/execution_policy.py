"""Scheduler-owned node approvals and durable execution policy transitions.

The canonical policy journal survives an older metadata summary. Only the
scheduler mutates it; a control reader merely queues answers. An approval covers
one logical node execution, including its declared retries and parked polls.
"""

import copy
import os
import time
import uuid

from cycle import checkpoints, revisions
from cycle import variables
from domain.cycle import PENDING, RUNNING, WAITING

MODES = ("auto", "important", "approve")
VERSION = 1
FIELDS = ("initial_execution_mode", "execution_mode", "execution_policy_version", "node_approvals", "policy_changes")
FILE = "execution-policy.json"


def input_preview(settings, raw, context):
    """A bounded explanation of inputs, excluding secret-bearing values."""
    secrets = [str(value) for value in context.secrets.values() if value]
    hidden_keys = ("password", "token", "secret", "authorization", "api_key", "apikey", "webhook", "credential", "private_key", "_pass")

    def render(value, template, key="", depth=0):
        refs = variables.references(template) if not isinstance(value, (dict, list, tuple)) else []
        hidden = any(word in key.lower() for word in hidden_keys) or any(
            ref.startswith("vars.") and ref.split(".")[1] in context.cycle.secrets
            or ref.startswith("env.") and any(word in ref.lower() for word in hidden_keys) for ref in refs)
        if hidden:
            return "[redacted]"
        if depth >= 4:
            return "[nested value omitted]"
        if isinstance(value, dict):
            return {str(name): render(item, template.get(name) if isinstance(template, dict) else None, str(name), depth + 1)
                    for name, item in list(value.items())[:20]}
        if isinstance(value, (list, tuple)):
            return [render(item, template[i] if isinstance(template, (list, tuple)) and i < len(template) else None,
                           key, depth + 1) for i, item in enumerate(value[:10])]
        if isinstance(value, str):
            for secret in sorted(secrets, key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
            return value[:500] + ("…" if len(value) > 500 else "")
        return value

    return render(settings, raw)


def requires_node_approval(run, node, resolved_inputs=None):
    mode = run.execution_mode or "auto"
    if mode not in MODES:
        raise checkpoints.CheckpointError("Unknown execution mode: %s" % mode)
    return node.plugin != "approval.gate" and (mode == "approve" or (mode == "important" and node.important))


def _save(store, run, **changes):
    document = {name: copy.deepcopy(changes.get(name, getattr(run, name))) for name in FIELDS}
    store.write(os.path.join(store.directory, FILE), dict(document, schema=1, run_id=run.id))
    for name, value in document.items():
        setattr(run, name, value)
    store.save(run)


def restore(store, run):
    path = os.path.join(store.directory, FILE)
    if not os.path.exists(path):
        return
    document = checkpoints._read(path)
    if (document.get("schema") != 1 or document.get("run_id") != run.id
            or document.get("initial_execution_mode") not in MODES or document.get("execution_mode") not in MODES
            or document.get("execution_policy_version") != VERSION
            or not isinstance(document.get("node_approvals"), dict) or not isinstance(document.get("policy_changes"), list)):
        raise checkpoints.CheckpointError("The execution policy journal is incompatible or unreadable.")
    if run.initial_execution_mode and run.initial_execution_mode != document["initial_execution_mode"]:
        raise checkpoints.CheckpointError("The initial execution mode does not match its policy journal.")
    for key, receipt in document["node_approvals"].items():
        if (not isinstance(receipt, dict) or receipt.get("id") != key or receipt.get("run_id") != run.id
                or receipt.get("step") not in run.steps or receipt.get("status") not in ("pending", "approved", "declined", "invalidated")
                or not isinstance(receipt.get("fingerprint"), str)):
            raise checkpoints.CheckpointError("The execution policy contains an unreadable node approval receipt.")
    for key in FIELDS:
        setattr(run, key, copy.deepcopy(document[key]))


def initialize(store, run, cycle, mode, resume):
    if mode is not None and mode not in MODES:
        raise checkpoints.CheckpointError("Execution mode must be auto, important or approve.")
    if resume:
        restore(store, run)
        run.initial_execution_mode = run.initial_execution_mode or "auto"
        run.execution_mode = run.execution_mode or run.initial_execution_mode
    else:
        run.initial_execution_mode = mode or cycle.execution.get("default_mode", "auto")
        run.execution_mode = run.initial_execution_mode
    if run.execution_mode not in MODES or run.execution_policy_version != VERSION:
        raise checkpoints.CheckpointError("The execution policy version or mode is unsupported.")
    change = None
    if resume and mode is not None and mode != run.execution_mode:
        change = change_mode(store, run, mode, "resume_change")
    else:
        _save(store, run)
    return change


def change_mode(store, run, mode, reason):
    if mode not in MODES:
        raise ValueError("Execution mode must be auto, important or approve.")
    if mode == run.execution_mode:
        return None
    approvals = copy.deepcopy(run.node_approvals)
    invalidated = []
    for receipt in approvals.values():
        step = run.steps.get(receipt["step"])
        if receipt["status"] in ("pending", "approved") and step is not None and step.started_at is None:
            receipt.update(status="invalidated", invalidated_at=time.time(), invalidation_reason="execution mode changed")
            invalidated.append(receipt["id"])
    receipt = {"id": len(run.policy_changes) + 1, "run_id": run.id, "from": run.execution_mode, "to": mode,
               "policy_version": VERSION, "at": time.time(), "reason": reason, "invalidated": invalidated}
    _save(store, run, execution_mode=mode, node_approvals=approvals, policy_changes=run.policy_changes + [receipt])
    return receipt


class Approvals:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.run, self.store = scheduler.run, scheduler.context.checkpoints

    def pending(self):
        return [receipt for receipt in self.run.node_approvals.values() if receipt["status"] == "pending"
                and self.run.steps[receipt["step"]].status == PENDING]

    def _binding(self, step, settings):
        state = self.run.steps[step.id]
        return {"run_id": self.run.id, "step": step.id, "attempt": state.attempts + 1,
                "execution_generation": state.execution_generation,
                "settings_digest": checkpoints.digest(settings),
                "definition_digest": self.store.definitions[step.id],
                **({"runtime": self.store.runtime_bindings[step.id]} if step.id in self.store.runtime_bindings else {}),
                "mode": self.run.execution_mode, "policy_version": VERSION}

    def _record(self, receipt):
        approvals = dict(self.run.node_approvals, **{receipt["id"]: receipt})
        _save(self.store, self.run, node_approvals=approvals)
        self.scheduler.observer.node_approval(self.run, receipt)

    def allow(self, step, settings):
        # Polling is a continuation of the execution already submitted. A safe
        # pause or policy change cannot retroactively withdraw that permission.
        unchanged = self.scheduler.execution_inputs.get(step.id) == (checkpoints.digest(settings), self.store.definitions[step.id])
        if (step.id in self.scheduler.begun and unchanged) or not requires_node_approval(self.run, step, settings):
            return True
        binding = self._binding(step, settings)
        fingerprint = checkpoints.digest(binding)
        candidates = [receipt for receipt in self.run.node_approvals.values() if receipt["step"] == step.id
                      and receipt["status"] in ("pending", "approved")]
        matching = next((receipt for receipt in candidates if receipt["fingerprint"] == fingerprint), None)
        for receipt in candidates:
            if receipt is not matching:
                self._record(dict(receipt, status="invalidated", invalidated_at=time.time(), invalidation_reason="node inputs changed"))
        if matching is not None:
            return matching["status"] == "approved"
        receipt = dict(binding, id=uuid.uuid4().hex, fingerprint=fingerprint, status="pending", created_at=time.time(),
                       label=step.title, important=step.important, importance_reason=step.importance_reason,
                       plugin=step.plugin, timeout=step.timeout,
                       purpose=self.scheduler.registry.get(step.plugin).metadata.summary,
                       resolved_inputs=input_preview(settings, step.settings, self.scheduler.context),
                       permissions=list(self.scheduler.registry.get(step.plugin).metadata.permissions),
                       declared_effects=list(next((phase.effects for phase in self.scheduler.cycle.phases if phase.id == step.phase), ())))
        self.run.status = WAITING
        self._record(receipt)
        self.scheduler._state("waiting_for_node_approval")
        return False

    def answer(self, request):
        receipt = self.run.node_approvals.get(request["request_id"])
        if receipt is None or receipt["status"] != "pending" or receipt["fingerprint"] != request["fingerprint"]:
            return "This approval is no longer pending or its fingerprint changed."
        step = self.scheduler.steps[receipt["step"]]
        state = self.run.steps[step.id]
        if state.status != PENDING:
            return "This node execution is no longer waiting for approval."
        try:
            settings = revisions.settings(step, self.run, self.scheduler._scope(), self.scheduler.cycle)
        except Exception as exc:
            return "The node inputs are unavailable: %s" % exc
        if receipt["fingerprint"] != checkpoints.digest(self._binding(step, settings)):
            self._record(dict(receipt, status="invalidated", invalidated_at=time.time(), invalidation_reason="node inputs changed"))
            return "The node inputs changed. Review a new approval request."
        approved = request["approved"]
        self._record(dict(receipt, status="approved" if approved else "declined", approved=approved, decided_at=time.time()))
        if not approved:
            next_action = "Review node details or Stop; Continue asks again"
            if self.run.pause_requested:
                self.run.pause_reason, self.run.next_action = "approval_declined", next_action
                self.run.control_history.append({"action": "approval_declined", "request_id": receipt["id"], "at": time.time()})
                self.scheduler._state("paused" if self.run.paused else "pausing")
            else:
                self.scheduler.pause_at("approval_declined", next_action)
        elif not self.run.paused and not self.run.pause_requested:
            self.run.status = WAITING if self.pending() else RUNNING
            self.scheduler._state("node_approved")
        return ""

    def invalidate(self, names, reason):
        for receipt in list(self.run.node_approvals.values()):
            if receipt["step"] in names and receipt["status"] in ("pending", "approved"):
                self._record(dict(receipt, status="invalidated", invalidated_at=time.time(), invalidation_reason=reason))


def parse_answer(raw):
    if not isinstance(raw, dict) or any(not isinstance(raw.get(key), str) or not raw[key]
                                        for key in ("run_id", "request_id", "fingerprint")) or type(raw.get("approved")) is not bool:
        return None, "Node approval needs a run id, request id, fingerprint and boolean decision."
    return {key: raw[key] for key in ("run_id", "request_id", "fingerprint", "approved")}, ""
