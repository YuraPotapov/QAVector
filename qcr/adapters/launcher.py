"""QAVector's launcher through QCR (ADR-004 L.1): is it busy, what did it say, stop it.

The launcher is the GUI's - one ``LauncherProcess`` per window - and nothing
here changes what it does. Its status and its output are the window's to
answer (``owner_read`` ``launcher.status`` / ``launcher.output``); a stop is
dispatched to it (``launcher.stop``) and does what the Stop button does once it
is confirmed. The resource is ``launcher.main``: one launcher today, more when
QAVector runs several at once (ADR-003 D3), each its own resource then.

**Output is read by cursor.** Each entry has a sequence number that only grows;
a reader passes the last ``next`` it got as ``after`` and gets what came since,
``dropped`` telling it older entries went before it looked. With
``wait_seconds`` the read waits (at most 60 s) for something new - how an agent
listens to a run.
"""

from ..contracts import CapabilityDescriptor, ResourceDescriptor, ResourceRef, text
from ..errors import QCRError
from ..registry import Registration
from .owner import OwnerInvoker

READ = "launcher.read"
RUN = "launcher.run"
RESOURCE_ID = "main"
WAIT_SECONDS = 60
STOP_SECONDS = 120

_TEXT = {"type": "string", "maxLength": 4000}
_SHORT = {"type": "string", "maxLength": 256}
_COUNT = {"type": ["integer", "null"], "minimum": 0}

#: One scenario of a run as the launcher's events tell it - shared with
#: ``scenarios.<id>.run``'s output.
SCENARIO_RESULT = {
    "type": "object", "additionalProperties": False, "required": ["id", "status"],
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": 256},
        "name": _SHORT, "session": _SHORT, "status": {"type": "string", "maxLength": 32},
        "passed": _COUNT, "total": _COUNT,
        # The first step that did not pass: the one that explains the rest.
        "failed_step": {"type": "object", "additionalProperties": False,
                        "required": ["index", "status", "message"],
                        "properties": {"index": {"type": "integer", "minimum": -1},
                                       "label": {"type": "string", "maxLength": 500},
                                       "blocks": {"type": "array", "items": _SHORT, "maxItems": 20},
                                       "status": {"type": "string", "maxLength": 32},
                                       "message": _TEXT, "attempts": {"type": "integer", "minimum": 0}}},
        "artifacts": {"$ref": "#/$defs/artifacts"},
    },
}
ARTIFACTS = {"type": "object", "additionalProperties": False, "required": ["dir", "files"],
             "properties": {"dir": _TEXT, "files": {"type": "array", "items": _SHORT, "maxItems": 200}}}
_RUN = {
    "type": ["object", "null"], "additionalProperties": False,
    "required": ["number", "label", "kind", "by", "started_at", "scenarios"],
    "properties": {
        "number": {"type": "integer", "minimum": 1}, "label": _TEXT,
        "kind": {"type": "string", "maxLength": 32}, "by": _SHORT,
        "started_at": {"type": "number"}, "ended_at": {"type": ["number", "null"]},
        "exit_code": {"type": ["integer", "null"]}, "execution_id": _SHORT, "history_entry": _SHORT,
        "run_dir": _TEXT, "scenarios": {"type": "array", "items": {"$ref": "#/$defs/scenario"}, "maxItems": 500},
        "artifacts": {"$ref": "#/$defs/artifacts"},
    },
}
DEFS = {"scenario": SCENARIO_RESULT, "artifacts": ARTIFACTS}

STATUS_OUTPUT = {"type": "object", "additionalProperties": False, "required": ["running", "run"],
                 "properties": {"running": {"type": "boolean"}, "run": _RUN}, "$defs": DEFS}
OUTPUT_INPUT = {"type": "object", "additionalProperties": False, "properties": {
    "after": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    "wait_seconds": {"type": "number", "minimum": 0, "maximum": WAIT_SECONDS}}}
_ENTRY = {"type": "object", "additionalProperties": False, "required": ["seq", "at", "type"],
          "properties": {"seq": {"type": "integer", "minimum": 1}, "at": {"type": "number"},
                         "type": {"enum": ["log", "event", "run"]},
                         "level": {"type": "string", "maxLength": 16}, "session": _SHORT, "text": _TEXT,
                         "kind": {"type": "string", "maxLength": 64}, "fields": {"type": "object"},
                         "run": {"type": "integer"}, "state": {"enum": ["started", "ended"]},
                         "label": _TEXT, "by": _SHORT, "exit_code": {"type": ["integer", "null"]}}}
OUTPUT_OUTPUT = {"type": "object", "additionalProperties": False, "required": ["entries", "next", "dropped", "running"],
                 "properties": {"entries": {"type": "array", "items": _ENTRY, "maxItems": 500},
                                "next": {"type": "integer", "minimum": 0}, "dropped": {"type": "boolean"},
                                "running": {"type": "boolean"}}}
STOP_INPUT = {"type": "object", "additionalProperties": False,
              "properties": {"reason": {"type": "string", "maxLength": 500}}}
STOP_OUTPUT = {"type": "object", "additionalProperties": False, "required": ["stopped", "run"],
               "properties": {"stopped": {"type": "boolean"}, "run": _RUN}, "$defs": DEFS}

_NO_INPUT = {"type": "object", "properties": {}, "additionalProperties": False}
#: op -> (permission, what it does, input, output) for the reads the window answers.
READS = {
    "status": ("Whether the launcher is busy, with what and started by whom, and the current or last run: "
               "each scenario's status, passed/total, the step that failed (message, attempts), artifacts.",
               _NO_INPUT, STATUS_OUTPUT),
    "output": ("What the launcher printed and its events, after a cursor (pass the last 'next' as 'after'). "
               "wait_seconds (<= 60) waits for something new - to follow a run as it goes.",
               OUTPUT_INPUT, OUTPUT_OUTPUT),
}


class LauncherAdapter:
    """The ``launcher.main`` resource - always there while the host is."""

    def __init__(self, registry, project_id, invoker=None):
        text(project_id, "project id")
        self.registry, self.project_id = registry, project_id
        self._owner = object()
        self.invoker = invoker
        self.ref = ResourceRef(project_id, RESOURCE_ID, "launcher")
        self.refresh()

    def refresh(self):
        resource = ResourceDescriptor(self.ref, "launcher", "QAVector launcher", READ)
        caps = [CapabilityDescriptor("launcher.%s.%s" % (RESOURCE_ID, op), self.ref, 1, description, given,
                                     output, READ, read_only=True)
                for op, (description, given, output) in READS.items()]
        if self.invoker is not None:
            caps.append(CapabilityDescriptor(
                "launcher.%s.stop" % RESOURCE_ID, self.ref, 1,
                "Stop what the launcher is running - anyone's run, as the Stop button does: its windows close. "
                "Say why in reason.", STOP_INPUT, STOP_OUTPUT, RUN, risk_level="medium", mode="long_running",
                timeout_seconds=STOP_SECONDS, supports_cancel=False, supports_idempotency=True))
        self.registry.replace(self._owner, [Registration(resource, tuple(caps), lambda ref, names: {},
                                                         read_capability=self._read, invoker=self.invoker)])
        return True

    @staticmethod
    def _read(capability_id, version, request):
        raise QCRError("owner_read", "This read is the owner's to answer.")

    @staticmethod
    def owner_op(capability_id):
        """The ``owner_read`` op for one of :data:`READS`, or None."""
        parts = capability_id.split(".")
        if len(parts) == 3 and parts[0] == "launcher" and parts[2] in READS:
            return "launcher." + parts[2]
        return None


class LauncherInvoker(OwnerInvoker):
    """``launcher.stop`` carried out by the window (see :mod:`.owner`)."""

    def wire_op(self, capability):
        return "launcher.stop"

    def timeout(self, capability, request):
        return STOP_SECONDS
