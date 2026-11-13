"""Cycles through QCR (ADR-003 3.2): describe one, run it, follow it, stop it.

The cycles a project has come with the GUI's ``inventory`` (the core's
``--describe``), beside its scenarios. A run is carried out by the GUI
(:class:`CycleInvoker`, op ``cycle.execute``) through the very command line and
launcher the Cycles page's Run uses - no second cycle runner.

**One launcher** (D3): a run while it is busy fails with ``launcher_busy``.

**The execution is the run.** It succeeds when the cycle run reached its end,
whatever the cycle's own ``status`` (``succeeded``, ``failed``, ...) says - that
is in the output, with the run id and every step's status. While it runs, each
step's end is a progress event of the execution, so ``executions.subscribe``
follows it. Cancel - or the deadline - stops the run.

A cycle the core reports problems for is refused before anything is written.
Variables go on the command line as the Cycles page puts them (``--cycle-var``),
so they are not for secrets: a cycle's secrets are its own store's.
"""

from qavector_sdk.validation import compile_output

from ..contracts import CapabilityDescriptor, ResourceDescriptor, ResourceRef, text
from ..errors import QCRError
from ..registry import Registration
from .owner import ACK_SECONDS, OwnerInvoker
from .scenarios import check_inventory

READ = "cycles.read"
RUN = "cycles.run"
APPROVE = "cycles.approve"
#: The resource a cycle's approvals live on: apart from the cycle, so an answer
#: is not "busy" behind the cycle's own run, which is unfinished all along.
APPROVALS = "%s~approvals"
KINDS = ("node_approval", "approval", "revision", "input", "choice")
DEFAULT_RUN_SECONDS = 4 * 3600
MAX_RUN_SECONDS = 24 * 3600

_NAME = {"type": "string", "minLength": 1, "maxLength": 256}
DESCRIBE_OUTPUT = {"type": "object", "additionalProperties": False, "required": ["id", "name", "steps", "problems"],
                   "properties": {"id": {"type": "string"}, "name": _NAME,
                                  "description": {"type": ["string", "null"]},
                                  "steps": {"type": "integer", "minimum": 0},
                                  "problems": {"type": "array", "items": {"type": "string"}}}}
EXECUTE_INPUT = {"type": "object", "additionalProperties": False, "properties": {
    "variables": {"type": "object", "maxProperties": 100,
                  "propertyNames": {"pattern": "^[A-Za-z_][A-Za-z0-9_]{0,63}$"},
                  "additionalProperties": {"type": "string", "maxLength": 4000}},
    "execution_mode": {"enum": ["auto", "important", "approve"]},
    "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_RUN_SECONDS}}}
EXECUTE_OUTPUT = {"type": "object", "additionalProperties": False, "required": ["run_id", "status", "exit_code", "steps"],
                  "properties": {
                      "run_id": {"type": "string", "maxLength": 256},
                      "status": {"type": "string", "maxLength": 64},
                      "exit_code": {"type": ["integer", "null"]},
                      "steps": {"type": "array", "maxItems": 2000, "items": {
                          "type": "object", "additionalProperties": False, "required": ["id", "status"],
                          "properties": {"id": {"type": "string", "maxLength": 256},
                                         "status": {"type": "string", "maxLength": 64}}}},
                      "history_entry": {"type": "string", "maxLength": 256}}}


_SHORT = {"type": "string", "maxLength": 4000}
_ITEM = {"type": "object", "additionalProperties": False, "required": ["id", "kind", "step", "question"],
         "properties": {
             "id": {"type": "string", "pattern": "^(node|ask):[A-Za-z0-9_-]{1,128}$"},
             "kind": {"enum": list(KINDS)},
             "step": {"type": "string", "maxLength": 256},
             "label": {"type": "string", "maxLength": 256},
             "question": _SHORT, "detail": {"type": "string", "maxLength": 20000},
             "options": {"type": "array", "items": {"type": "string", "maxLength": 256}, "maxItems": 20},
             "text": {"type": "object", "additionalProperties": False, "properties": {
                 "required": {"type": "boolean"}, "placeholder": {"type": "string", "maxLength": 500}}},
             "choice": {"type": "object", "additionalProperties": False, "required": ["mode", "rows"], "properties": {
                 "mode": {"enum": ["single", "multiple"]},
                 "columns": {"type": "array", "items": {"type": "string", "maxLength": 256}, "maxItems": 20},
                 "rows": {"type": "array", "maxItems": 500, "items": {"type": "array", "maxItems": 20,
                          "items": {"type": "string", "maxLength": 1000}}}}},
             "revision": {"type": "object", "additionalProperties": False,
                          "properties": {"enabled": {"type": "boolean"}}},
             "inputs": {}}}
PENDING_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["cycle", "run_id", "items"],
                  "properties": {"cycle": {"type": "string", "maxLength": 200},
                                 "run_id": {"type": "string", "maxLength": 256},
                                 "items": {"type": "array", "items": _ITEM, "maxItems": 200}}}
PENDING_OUTPUT = {"type": "object", "additionalProperties": False, "required": ["run_id", "items"],
                  "properties": {"run_id": {"type": "string"}, "items": {"type": "array", "items": _ITEM}}}
ANSWER_INPUT = {"type": "object", "additionalProperties": False, "required": ["item", "answer"], "properties": {
    "item": {"type": "string", "maxLength": 200},
    "answer": {"type": "string", "minLength": 1, "maxLength": 256},
    "text": {"type": "string", "maxLength": 8000},
    "chosen": {"type": "array", "items": {"type": "integer", "minimum": 0}, "maxItems": 500, "uniqueItems": True},
    "feedback": {"type": "string", "maxLength": 8000}}}
ANSWER_OUTPUT = {"type": "object", "additionalProperties": False, "required": ["item", "decided"],
                 "properties": {"item": {"type": "string"},
                                "decided": {"enum": ["approved", "declined", "answered", "revision_requested"]}}}


class CycleAdapter:
    """``cycles.<id>`` resources from the GUI's inventory, and their approvals.

    ``pending_reader()`` gives what the GUI says is open in the run on screen:
    ``{cycle, run_id, items}`` (:data:`PENDING_SCHEMA`), or None.
    """

    def __init__(self, registry, project_id, inventory_reader, invoker=None, pending_reader=None,
                 answer_invoker=None):
        text(project_id, "project id")
        self.registry, self.project_id = registry, project_id
        self._reader = inventory_reader
        self._pending_reader = pending_reader or (lambda: None)
        self._owner = object()
        self.invoker, self.answer_invoker = invoker, answer_invoker
        for one in (invoker, answer_invoker):
            if one is not None:
                one.adapter = self

    def _cycles(self):
        try:
            inventory = self._reader()
            if inventory is None:
                return None
            check_inventory()(inventory)
            if inventory["project_id"] != self.project_id:
                raise ValueError("another project")
            return list(inventory.get("cycles") or ())
        except Exception:
            raise QCRError("source_unavailable", "The cycle inventory is invalid or unavailable.") from None

    def refresh(self):
        cycles = self._cycles()
        if cycles is None:
            return False
        registrations = []
        for cycle in cycles:
            ref = ResourceRef(self.project_id, cycle["id"], "cycle")
            caps = [CapabilityDescriptor("cycles.%s.describe" % ref.resource_id, ref, 1,
                                         "What the cycle is: its steps and any problems the core found in it.",
                                         {"type": "object", "properties": {}, "additionalProperties": False},
                                         DESCRIBE_OUTPUT, READ, read_only=True)]
            if self.invoker is not None:
                caps.append(CapabilityDescriptor(
                    "cycles.%s.execute" % ref.resource_id, ref, 1,
                    "Run the cycle through QAVector's launcher; ends when the run does.",
                    EXECUTE_INPUT, EXECUTE_OUTPUT, RUN, risk_level="medium", mode="long_running",
                    timeout_seconds=MAX_RUN_SECONDS + ACK_SECONDS, supports_cancel=True,
                    supports_idempotency=True))
            registrations.append(Registration(ResourceDescriptor(ref, "cycles", cycle["name"], READ), tuple(caps),
                                              lambda ref, names: {}, read_capability=self._describe,
                                              invoker=self.invoker))
            approvals = ResourceRef(self.project_id, APPROVALS % cycle["id"], "cycle_approvals")
            caps = [CapabilityDescriptor("cycles.%s.pending" % cycle["id"], approvals, 1,
                                         "The questions and approvals open in this cycle's run on screen.",
                                         {"type": "object", "properties": {}, "additionalProperties": False},
                                         PENDING_OUTPUT, READ, read_only=True)]
            if self.answer_invoker is not None:
                caps.append(CapabilityDescriptor(
                    "cycles.%s.answer" % cycle["id"], approvals, 1,
                    "Answer one open question or approval of this cycle's run, as the asking actor.",
                    ANSWER_INPUT, ANSWER_OUTPUT, APPROVE, risk_level="high", mode="long_running",
                    timeout_seconds=60 + ACK_SECONDS, supports_idempotency=True))
            registrations.append(Registration(
                ResourceDescriptor(approvals, "cycles", "%s approvals" % cycle["name"], READ), tuple(caps),
                lambda ref, names: {}, read_capability=self._pending_read, invoker=self.answer_invoker))
        self.registry.replace(self._owner, registrations)
        return True

    def pending(self, cycle_id):
        """``{run_id, items}`` open in ``cycle_id``'s run on screen; empty for any other cycle."""
        try:
            pending = self._pending_reader()
            if pending is not None:
                compile_pending()(pending)
        except Exception:
            raise QCRError("source_unavailable", "The open approvals are invalid or unavailable.") from None
        if not pending or pending["cycle"] != cycle_id:
            return {"run_id": "", "items": []}
        return {"run_id": pending["run_id"], "items": pending["items"]}

    def _pending_read(self, capability_id, version, request):
        return self.pending(capability_id.split(".")[1])

    def find(self, cycle_id):
        cycle = next((one for one in self._cycles() or () if one["id"] == cycle_id), None)
        if cycle is None:
            raise QCRError("not_found", "Cycle is not available.")
        return cycle

    def _describe(self, capability_id, version, request):
        cycle = self.find(capability_id.split(".")[1])
        return {"id": cycle["id"], "name": cycle["name"], "description": cycle.get("description"),
                "steps": int(cycle.get("steps") or 0), "problems": list(cycle.get("problems") or ())}


_compiled_pending = []


def compile_pending():
    if not _compiled_pending:
        _compiled_pending.append(compile_output(PENDING_SCHEMA))
    return _compiled_pending[0]


class CycleInvoker(OwnerInvoker):
    """``cycle.execute`` carried out by the GUI's launcher (see :mod:`.owner`)."""

    def wire_op(self, capability):
        return "cycle.execute"

    def requirements(self, capability, request):
        if self.adapter.find(capability.resource.resource_id).get("problems"):
            raise QCRError("cycle_invalid", "The cycle has problems; fix it before running.")
        return ()

    def timeout(self, capability, request):
        return self._ack_seconds + request.get("timeout_seconds", DEFAULT_RUN_SECONDS)


class CycleAnswerInvoker(OwnerInvoker):
    """``cycle.answer``: the GUI hands an answer to the run the way its own
    dialog or Approve button would, attributed to the actor (see :mod:`.owner`).

    Checked here before anything is written: the item is open in this cycle's
    run, and the answer is one it takes - Approve / Decline for a node; one of
    its options for a question (``revise`` with feedback for a revision that
    allows it); the text a text question requires; the rows a choice allows.
    """

    def wire_op(self, capability):
        return "cycle.answer"

    def requirements(self, capability, request):
        cycle_id = capability.capability_id.split(".")[1]
        item = next((one for one in self.adapter.pending(cycle_id)["items"] if one["id"] == request["item"]), None)
        if item is None:
            raise QCRError("not_pending", "Nothing with that id is waiting for an answer in this cycle's run.")
        answer = request["answer"]
        if item["kind"] == "node_approval":
            if answer not in ("approve", "decline"):
                raise QCRError("invalid_input", "A node approval is answered approve or decline.")
            return ()
        if answer == "revise":
            if item["kind"] != "revision" or not (item.get("revision") or {}).get("enabled") \
                    or not str(request.get("feedback") or "").strip():
                raise QCRError("invalid_input", "Only a revision that allows it is revised, with feedback.")
            return ()
        if answer not in (item.get("options") or []):
            raise QCRError("invalid_input", "That answer is not one of the question's options.")
        if (item.get("text") or {}).get("required") and not str(request.get("text") or "").strip():
            raise QCRError("invalid_input", "This question needs written text.")
        choice = item.get("choice")
        if choice is not None and "chosen" in request:
            rows = len(choice.get("rows") or ())
            if any(index >= rows for index in request["chosen"]) or (
                    choice.get("mode") == "single" and len(request["chosen"]) > 1):
                raise QCRError("invalid_input", "The rows chosen are not rows this choice offers.")
        return ()

    def timeout(self, capability, request):
        return self._ack_seconds + 60
