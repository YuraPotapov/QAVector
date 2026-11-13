"""Scenarios through QCR (ADR-003 3.1): describe one, run one, stop it.

The scenarios, environments and accounts a project has are what the GUI's own
inventory says - the core's ``--describe`` - sent to the host as an
``inventory`` message; nothing here reads the flows folder. A run is carried
out by the GUI (:class:`ScenarioInvoker`, op ``scenario.run``) through the very
launcher and command-line builder its Run button uses, so there is no second
runner.

**One launcher** (D3): while it is busy - a person's run or an agent's - a new
run fails with ``launcher_busy``, naming nothing it cannot see.

**Execution status is not the test outcome.** The execution succeeds when the
run went through to its end; whether the scenarios passed is ``outcome`` in its
output (``passed`` / ``failed`` / ``error`` / ``none``), with each scenario's
own status. Cancel - or the deadline - stops the run: unlike a service, the
execution *is* the run.
"""

from functools import lru_cache

from qavector_sdk.validation import compile_output

from ..contracts import CapabilityDescriptor, ResourceDescriptor, ResourceRef, text
from ..errors import QCRError
from ..registry import Registration
from .launcher import DEFS as RUN_DEFS
from .owner import ACK_SECONDS, OwnerInvoker

READ = "scenarios.read"
RUN = "scenarios.run"
DEFAULT_RUN_SECONDS = 1800
MAX_RUN_SECONDS = 6 * 3600

_ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,200}$"}
_NAME = {"type": "string", "minLength": 1, "maxLength": 256}
_TEXT = {"type": "string", "maxLength": 4000}
_ENVIRONMENT = {"type": "object", "additionalProperties": False, "required": ["name", "accounts"],
                "properties": {"name": _NAME, "accounts": {"type": "array", "items": _NAME, "maxItems": 500}}}
INVENTORY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "project_id", "scenarios", "environments"],
    "properties": {
        "schema_version": {"const": 1}, "project_id": _NAME,
        "scenarios": {"type": "array", "maxItems": 5000, "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "name"],
            "properties": {"id": _ID, "name": _NAME, "description": {"type": ["string", "null"], "maxLength": 4000},
                           "tags": {"type": "array", "items": _NAME, "maxItems": 100}}}},
        "environments": {"type": "array", "items": _ENVIRONMENT, "maxItems": 500},
        # Optional: an owner of before 3.2 sends none. Read by qcr.adapters.cycles.
        "cycles": {"type": "array", "maxItems": 2000, "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "name"],
            "properties": {"id": _ID, "name": _NAME, "description": {"type": ["string", "null"], "maxLength": 4000},
                           "steps": {"type": "integer", "minimum": 0},
                           "problems": {"type": "array", "items": _TEXT, "maxItems": 100}}}},
    },
}
DESCRIBE_OUTPUT = {"type": "object", "additionalProperties": False,
                   "required": ["id", "name", "tags", "environments"],
                   "properties": {"id": _ID, "name": _NAME, "description": {"type": ["string", "null"]},
                                  "tags": {"type": "array", "items": _NAME},
                                  "environments": {"type": "array", "items": _ENVIRONMENT}}}
RUN_INPUT = {"type": "object", "additionalProperties": False, "required": ["environment"],
             "properties": {
                 "environment": _NAME,
                 "accounts": {"type": "array", "items": _NAME, "minItems": 1, "maxItems": 100, "uniqueItems": True},
                 "jobs": {"type": "integer", "minimum": 1, "maximum": 16},
                 "keep_windows": {"type": "boolean"},
                 "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_RUN_SECONDS}}}
RUN_OUTPUT = {"type": "object", "additionalProperties": False, "required": ["exit_code", "outcome", "scenarios"],
              "properties": {
                  "exit_code": {"type": ["integer", "null"]},
                  "outcome": {"enum": ["passed", "failed", "error", "none"]},
                  # Each with its status and, from the launcher's own events (ADR-004 D2):
                  # passed/total, the step that failed (message, attempts) and its artifacts.
                  "scenarios": {"type": "array", "maxItems": 500, "items": {"$ref": "#/$defs/scenario"}},
                  "run_dir": _TEXT,
                  "history_entry": {"type": "string", "maxLength": 256},
                  "note": _TEXT},
              "$defs": RUN_DEFS}


@lru_cache(maxsize=1)
def check_inventory():
    """The ``inventory`` schema checked once (shared with qcr.adapters.cycles)."""
    return compile_output(INVENTORY_SCHEMA)


def _check_inventory():
    return check_inventory()


class ScenarioAdapter:
    """``scenarios.<id>`` resources from the GUI's inventory."""

    def __init__(self, registry, project_id, inventory_reader, invoker=None):
        text(project_id, "project id")
        self.registry, self.project_id = registry, project_id
        self._reader = inventory_reader
        self._owner = object()
        self.invoker = invoker
        if invoker is not None:
            invoker.adapter = self

    def inventory(self):
        try:
            inventory = self._reader()
            if inventory is None:
                return None
            _check_inventory()(inventory)
            if inventory["project_id"] != self.project_id:
                raise ValueError("another project")
            return inventory
        except Exception:
            raise QCRError("source_unavailable", "The scenario inventory is invalid or unavailable.") from None

    def refresh(self):
        inventory = self.inventory()
        if inventory is None:
            return False
        registrations = []
        for scenario in inventory["scenarios"]:
            ref = ResourceRef(self.project_id, scenario["id"], "scenario")
            resource = ResourceDescriptor(ref, "scenarios", scenario["name"], READ)
            caps = [CapabilityDescriptor("scenarios.%s.describe" % ref.resource_id, ref, 1,
                                         "What the scenario is, and the environments and accounts it can run with.",
                                         {"type": "object", "properties": {}, "additionalProperties": False},
                                         DESCRIBE_OUTPUT, READ, read_only=True)]
            if self.invoker is not None:
                caps.append(CapabilityDescriptor(
                    "scenarios.%s.run" % ref.resource_id, ref, 1,
                    "Run the scenario in one environment through QAVector's launcher; ends when the run does, with "
                    "each scenario's status, the step that failed (message, attempts) and its artifacts.",
                    RUN_INPUT, RUN_OUTPUT, RUN, risk_level="medium", mode="long_running",
                    timeout_seconds=MAX_RUN_SECONDS + ACK_SECONDS, supports_cancel=True,
                    supports_idempotency=True))
            registrations.append(Registration(resource, tuple(caps), lambda ref, names: {},
                                              read_capability=self._describe, invoker=self.invoker))
        self.registry.replace(self._owner, registrations)
        return True

    def _describe(self, capability_id, version, request):
        inventory = self.inventory() or {"scenarios": [], "environments": []}
        scenario_id = capability_id.split(".")[1]
        scenario = next((one for one in inventory["scenarios"] if one["id"] == scenario_id), None)
        if scenario is None:
            raise QCRError("not_found", "Scenario is not available.")
        return {"id": scenario["id"], "name": scenario["name"], "description": scenario.get("description"),
                "tags": list(scenario.get("tags") or ()), "environments": inventory["environments"]}

    def check_run(self, request):
        """The environment and accounts a run names exist - before anything is written."""
        inventory = self.inventory() or {"environments": []}
        environment = next((one for one in inventory["environments"] if one["name"] == request["environment"]), None)
        if environment is None:
            raise QCRError("invalid_input", "No such environment in this project.")
        unknown = set(request.get("accounts") or ()) - set(environment["accounts"])
        if unknown:
            raise QCRError("invalid_input", "An account is not in that environment.")


class ScenarioInvoker(OwnerInvoker):
    """``scenario.run`` carried out by the GUI's launcher (see :mod:`.owner`)."""

    def wire_op(self, capability):
        return "scenario.run"

    def requirements(self, capability, request):
        self.adapter.check_run(request)
        return ()

    def timeout(self, capability, request):
        return self._ack_seconds + request.get("timeout_seconds", DEFAULT_RUN_SECONDS)
