"""Services through QCR: snapshots from the GUI owner, and start/stop/restart.

The trusted host injects a project-bound snapshot reader. It returns None when
the owner is disconnected. Reading never starts or probes a service.

**Mutations go to the owner, not around it** (ADR-002). ``start``, ``stop`` and
``restart`` are carried out by the GUI's own ``ServiceSupervisor``: the
:class:`ServiceInvoker` sends a ``dispatch`` message through the host, and the
GUI answers with ``owner`` reports - ``accepted`` (it took the job, not success),
``running``, ``progress``, then ``succeeded`` or ``failed``. Waiting for
readiness is the GUI's: it already knows the restart race (``ServiceBridge``).
No acknowledgement within ``ACK_SECONDS`` is ``owner_unavailable``.

**Dependencies** (D5). ``start`` and ``restart`` need ``services.start`` on the
whole ``depends_on`` closure. A dependency without a QCR identity cannot be
authorized, so such a start is refused (``dependency_unmigrated``) rather than
quietly let through: services without an id stay on the legacy path, they are
never skipped by a check.
"""

from functools import lru_cache

from qavector_sdk.validation import compile_output

from ..contracts import (CapabilityDescriptor, PropertyDescriptor, PropertySnapshot,
                         ResourceDescriptor, ResourceRef, text)
from ..errors import QCRError
from ..registry import Registration
from .owner import ACK_SECONDS, OwnerInvoker

SOURCE = "gui.service_supervisor.cache"
PERMISSION = "services.read"
PROPERTIES = {
    "status": {"type": "string", "enum": ["stopped", "waiting", "starting", "running", "stopping", "failed", "unknown"]},
    "runner_type": {"type": "string", "minLength": 1, "maxLength": 128},
    "detached": {"type": "boolean"},
    "configuration_stale": {"type": "boolean"},
}
_UUID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
_NAME = {"type": "string", "minLength": 1, "maxLength": 256}
#: green = "this is the good sign" (Running, Serving, passed), red = "this went
#: wrong" (Crashed, Port busy), amber = a warning; blue and grey only inform.
CRITERION_COLORS = ("green", "amber", "red", "blue", "grey")
_CRITERION = {"type": "object", "additionalProperties": False, "required": ["name", "met"],
              "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 256},
                             "color": {"enum": list(CRITERION_COLORS)}, "met": {"type": "boolean"}}}
SNAPSHOT_SCHEMA = {
    "$defs": {"criterion": _CRITERION},
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "project_id", "observed_at", "ttl_seconds", "resources", "legacy_count"],
    "properties": {
        "schema_version": {"const": 1}, "project_id": _NAME,
        "observed_at": {"type": "number", "minimum": 0},
        "ttl_seconds": {"type": "number", "minimum": 0, "maximum": 60},
        "legacy_count": {"type": "integer", "minimum": 0},
        "resources": {"type": "array", "maxItems": 10000, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["resource_id", "group_id", "alias", "display_name", "properties"],
            "properties": {
                "resource_id": _UUID, "group_id": _UUID, "alias": _NAME, "display_name": _NAME,
                "properties": {"type": "object", "additionalProperties": False,
                               "required": list(PROPERTIES), "properties": PROPERTIES},
                # Optional, for owners of before 2.3: no list means no known dependencies.
                "depends_on": {"type": "array", "items": _UUID, "maxItems": 1000, "uniqueItems": True},
                "unmigrated_dependencies": {"type": "integer", "minimum": 0},
                # What the service's own log says, criterion by criterion (optional).
                "criteria": {"type": "array", "maxItems": 100, "items": {"$ref": "#/$defs/criterion"}},
            },
        }},
    },
}


#: How long start/restart wait for readiness by default (as ``engine.services``).
DEFAULT_WAIT_SECONDS = 120
_WAIT = {"type": "number", "exclusiveMinimum": 0, "maximum": 3600}
START_INPUT = {"type": "object", "additionalProperties": False, "properties": {
    "wait_ready": {"type": "boolean"}, "timeout_seconds": _WAIT}}
STOP_INPUT = {"type": "object", "additionalProperties": False, "properties": {"timeout_seconds": _WAIT}}
STATUS = PROPERTIES["status"]
START_OUTPUT = {"type": "object", "additionalProperties": False, "required": ["service_status", "readiness"],
                "properties": {
                    "service_status": STATUS,
                    "readiness": {"enum": ["ready", "not_ready", "not_checked"]},
                    "criteria": {"type": "array", "maxItems": 100, "items": _CRITERION},
                    "has_criteria": {"type": "boolean"},
                    # The read to follow up with: services.<id>.criteria, or .output without criteria.
                    "next": {"type": "string", "pattern": "^services\\.[0-9a-f]{32}\\.(criteria|output)$"},
                    "dependencies": {"type": "array", "items": _UUID, "maxItems": 1000},
                    "observed_at": {"type": "number", "minimum": 0}}}
STOP_OUTPUT = {"type": "object", "additionalProperties": False, "required": ["service_status"],
               "properties": {"service_status": STATUS, "observed_at": {"type": "number", "minimum": 0}}}
#: How a service's criteria stand now (``settled``): ``ready`` - every green one
#: lit, no red; ``failed`` - a red one lit; ``waiting`` - a green one not yet;
#: ``no_ready_signal`` - criteria, but none green to wait for; ``none`` - none.
CRITERIA_INPUT = {"type": "object", "additionalProperties": False, "properties": {
    "wait_seconds": {"type": "number", "minimum": 0, "maximum": 60}}}
CRITERIA_OUTPUT = {"type": "object", "additionalProperties": False,
                   "required": ["service_status", "has_criteria", "settled", "criteria"],
                   "properties": {"service_status": STATUS, "has_criteria": {"type": "boolean"},
                                  "settled": {"enum": ["ready", "failed", "waiting", "no_ready_signal", "none"]},
                                  "criteria": {"type": "array", "items": _CRITERION},
                                  "observed_at": {"type": "number", "minimum": 0}}}
OUTPUT_INPUT = {"type": "object", "additionalProperties": False, "properties": {
    "lines": {"type": "integer", "minimum": 1, "maximum": 200}}}
OUTPUT_OUTPUT = {"type": "object", "additionalProperties": False, "required": ["lines"],
                 "properties": {"lines": {"type": "array", "items": {"type": "string", "maxLength": 4000},
                                          "maxItems": 200},
                                "service_status": STATUS}}
#: The reads beside status: (what it does, input, output).
READS = {
    "criteria": ("Its log criteria: green = ready, red = failed. Has green ones? Wait here (wait_seconds "
                 "<= 60; call again for something slow, like a migration). None? Read its output.",
                 CRITERIA_INPUT, CRITERIA_OUTPUT),
    "output": ("The last lines it printed (its Console), at most 200: to judge a service without "
               "criteria, or why it failed.", OUTPUT_INPUT, OUTPUT_OUTPUT),
}


def settled(criteria):
    """How a service's criteria stand now - see CRITERIA_OUTPUT."""
    if not criteria:
        return "none"
    if any(one.get("color") == "red" and one["met"] for one in criteria):
        return "failed"
    green = [one for one in criteria if one.get("color", "green") == "green"]
    if not green:
        return "no_ready_signal"
    return "ready" if all(one["met"] for one in green) else "waiting"


#: op -> (permission, risk, input, output, what it does)
OPERATIONS = {
    "start": ("services.start", "medium", START_INPUT, START_OUTPUT,
              "Start the service (and what it depends on) through its owner; optionally wait until it is ready."),
    "restart": ("services.restart", "medium", START_INPUT, START_OUTPUT,
                "Restart the service through its owner; optionally wait until the new run is ready."),
    "stop": ("services.stop", "high", STOP_INPUT, STOP_OUTPUT,
             "Stop the service through its owner and wait until it is down."),
}


@lru_cache(maxsize=1)
def _check_snapshot():
    """SNAPSHOT_SCHEMA checked against the metaschema once, not per snapshot."""
    return compile_output(SNAPSHOT_SCHEMA)


class ServiceAdapter:
    def __init__(self, registry, project_id, snapshot_reader, invoker=None):
        """``invoker``: a :class:`ServiceInvoker` makes start/stop/restart
        invocable; without one only the reads of Phase 1A are registered."""
        text(project_id, "project id")
        self.registry = registry
        self.project_id = project_id
        self._reader = snapshot_reader
        self._owner = object()
        self.invoker = invoker
        if invoker is not None:
            invoker.adapter = self

    def _snapshot(self):
        try:
            snapshot = self._reader()
            if snapshot is None:
                return None
            _check_snapshot()(snapshot)
            if snapshot["project_id"] != self.project_id:
                raise QCRError("invalid_source", "Snapshot belongs to a different project.")
            ids = [row["resource_id"] for row in snapshot["resources"]]
            if len(set(ids)) != len(ids):
                raise QCRError("invalid_source", "Duplicate service identity.")
            return snapshot
        except Exception:
            raise QCRError("source_unavailable", "Service snapshot is invalid or unavailable.") from None

    def refresh(self):
        """Explicit metadata refresh; no persistent data is created or changed.

        Disconnection retains known descriptors, whose reads become unavailable.
        An authoritative empty inventory removes this adapter's registrations.
        """
        snapshot = self._snapshot()
        if snapshot is None:
            return False
        registrations = []
        for row in snapshot["resources"]:
            ref = ResourceRef(self.project_id, row["resource_id"], "service")
            resource = ResourceDescriptor(ref, "services", row["display_name"], PERMISSION,
                aliases=(row["alias"],), properties=tuple(
                    PropertyDescriptor(name, schema, PERMISSION) for name, schema in PROPERTIES.items()))
            # Reads, answered from the owner's snapshot (output: from the owner, asked).
            capabilities = [CapabilityDescriptor("services.%s.status" % ref.resource_id, ref, 1,
                "Read cached service process status; this is not a health check.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                PROPERTIES["status"], PERMISSION, read_only=True)]
            for op, (description, given, output) in READS.items():
                capabilities.append(CapabilityDescriptor("services.%s.%s" % (ref.resource_id, op), ref, 1,
                                                         description, given, output, PERMISSION,
                                                         read_only=True))
            if self.invoker is not None:
                for op, (permission, risk, given, output, description) in OPERATIONS.items():
                    capabilities.append(CapabilityDescriptor(
                        "services.%s.%s" % (ref.resource_id, op), ref, 1, description, given, output,
                        permission, risk_level=risk, mode="long_running", timeout_seconds=3600 + ACK_SECONDS,
                        supports_cancel=True, supports_idempotency=True))
            registrations.append(Registration(resource, tuple(capabilities), self._read,
                                              read_capability=self._status, invoker=self.invoker))
        self.registry.replace(self._owner, registrations)
        return True

    def closure(self, resource_id):
        """Every service ``resource_id`` depends on, transitively, by id.

        Raises ``dependency_unmigrated`` when any of them has no QCR identity,
        ``not_found`` when the service is not in the current snapshot.
        """
        snapshot = self._snapshot()
        rows = {row["resource_id"]: row for row in (snapshot or {}).get("resources", ())}
        if resource_id not in rows:
            raise QCRError("not_found", "Service is not available.")
        seen, todo = [], [resource_id]
        while todo:
            row = rows.get(todo.pop())
            if row is None:
                continue
            if row.get("unmigrated_dependencies"):
                raise QCRError("dependency_unmigrated",
                               "A service this one depends on has no QCR identity yet.")
            for dependency in row.get("depends_on", ()):
                if dependency not in seen and dependency != resource_id:
                    if dependency not in rows:
                        raise QCRError("dependency_unmigrated",
                                       "A service this one depends on has no QCR identity yet.")
                    seen.append(dependency)
                    todo.append(dependency)
        return seen

    def _status(self, capability_id, version, request):
        """``services.<id>.status`` / ``.criteria`` from the owner's latest snapshot.
        (``.output`` is the owner's to answer; the host asks it - never here.)"""
        _, resource_id, op = capability_id.split(".")
        snapshot = self._snapshot()
        row = next((row for row in (snapshot or {}).get("resources", ()) if row["resource_id"] == resource_id), None)
        if row is None:
            raise QCRError("source_unavailable", "The service's owner has not said how it is.")
        if op == "status":
            return row["properties"]["status"]
        if op == "criteria":
            criteria = list(row.get("criteria") or ())
            return {"service_status": row["properties"]["status"], "has_criteria": bool(criteria),
                    "settled": settled(criteria), "criteria": criteria, "observed_at": snapshot["observed_at"]}
        raise QCRError("owner_read", "This read is the owner's to answer.")

    def _read(self, ref, names):
        snapshot = self._snapshot()
        row = next((row for row in snapshot["resources"] if row["resource_id"] == ref.resource_id), None) if snapshot else None
        if row is None:
            return {name: PropertySnapshot(None, SOURCE, None, 0, "unavailable") for name in names}
        return {name: PropertySnapshot(row["properties"][name], SOURCE,
                                      snapshot["observed_at"], snapshot["ttl_seconds"])
                for name in names}


class ServiceInvoker(OwnerInvoker):
    """start/stop/restart through the GUI's supervisor (see :mod:`.owner`).

    A failure the owner reports without a code is ``service_failed``.
    """

    def requirements(self, capability, request):
        if self.wire_op(capability) not in ("start", "restart"):
            return ()
        ref = capability.resource
        return [(ResourceRef(ref.project_id, dependency, "service"), "services.start")
                for dependency in self.adapter.closure(ref.resource_id)]

    def timeout(self, capability, request):
        wait = request.get("timeout_seconds", DEFAULT_WAIT_SECONDS)
        if self.wire_op(capability) != "stop" and request.get("wait_ready") is False:
            wait = 0
        return self._ack_seconds + wait if wait else self._ack_seconds * 2

    def report(self, message):
        if message.get("event") == "failed" and not isinstance((message.get("error") or {}).get("code"), str):
            message = dict(message, error=dict(message.get("error") or {}, code="service_failed"))
        return super().report(message)
