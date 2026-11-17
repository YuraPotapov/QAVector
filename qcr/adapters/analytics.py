"""What agent work used and cost, as QCR reads (ADR-008 D6, increment U.2).

Read-only, over the project's agent-sessions record through
:mod:`integrations.agents.usage` - the same sums every view uses. Permission
``analytics.read``: an agent has it only when its connection's "Read usage"
switch is on. Nothing here asks any service or program; it reads files.

* ``analytics.usage.summary`` - totals, filtered by period, connection, model,
  kind or cycle and run, with breakdowns;
* ``analytics.usage.conversation`` - one conversation, own and inclusive of
  what it handed on to other agents;
* ``analytics.usage.ref`` - the conversations with one ref (a Jira issue's),
  own and inclusive.
"""

from integrations.agents import sessions, usage

from ..contracts import CapabilityDescriptor, ResourceDescriptor, ResourceRef
from ..errors import QCRError
from ..registry import Registration

PERMISSION = "analytics.read"
RESOURCE = "agent-usage"

_TEXT = {"type": "string", "maxLength": 512}
_TIME = {"type": "number", "minimum": 0}
SUMMARY_INPUT = {"type": "object", "additionalProperties": False, "properties": {
    "since": _TIME, "until": _TIME, "connection": _TEXT, "model": _TEXT,
    "kind": {"enum": list(usage.KINDS)}, "cycle": _TEXT, "run": _TEXT, "role": _TEXT,
    "by": {"type": "array", "maxItems": 5, "items": {"enum": ["model", "connection", "kind", "cycle", "role"]}}}}
_TOTALS = {"type": "object", "required": ["conversations", "turns", "tokens", "tokens_total", "estimate_usd",
                                          "priced_usd", "unknown_cost_turns"]}
_PAIR = {"type": "object", "required": ["own", "inclusive", "children"],
         "properties": {"own": _TOTALS, "inclusive": _TOTALS,
                        "children": {"type": "array", "items": {"type": "string"}}}}


class AnalyticsAdapter:
    def __init__(self, registry, project_id, root):
        self.registry, self.project_id, self.root = registry, project_id, root
        self._owner = object()

    def refresh(self):
        ref = ResourceRef(self.project_id, RESOURCE, "analytics")

        def cap(name, description, schema, output):
            return CapabilityDescriptor("analytics.usage." + name, ref, 1, description, schema, output,
                                        PERMISSION, read_only=True)
        caps = (
            cap("summary", "Agent tokens and est. cost in this project: filter by since/until (epoch s), "
                "connection, model, kind, cycle, run; break down by model, connection, kind or cycle.",
                SUMMARY_INPUT, dict(_TOTALS, properties={"by": {"type": "object"}})),
            cap("conversation", "One agent conversation's tokens and est. cost: its own, and inclusive of the "
                "conversations it handed work to.",
                {"type": "object", "additionalProperties": False, "required": ["session"],
                 "properties": {"session": _TEXT}}, _PAIR),
            cap("ref", "The conversations about one thing (a Jira issue's ref, jira:<connection>:<KEY>): own "
                "figures, and inclusive of what they handed on.",
                {"type": "object", "additionalProperties": False, "required": ["ref"],
                 "properties": {"ref": _TEXT}}, _PAIR))
        self.registry.replace(self._owner, (Registration(
            ResourceDescriptor(ref, "analytics", "Agent usage", PERMISSION), caps,
            lambda ref, names: {}, read_capability=self._read),))

    def _read(self, capability_id, version, request):
        name = capability_id.rsplit(".", 1)[1]
        try:
            if name == "summary":
                by = tuple(request.get("by") or ("model", "connection", "kind"))
                filters = {key: request[key] for key in ("since", "until", "connection", "model", "kind",
                                                          "cycle", "run", "role") if key in request}
                return usage.summary(self.root, by=by, **filters)
            if name == "conversation":
                return usage.conversation(self.root, request["session"])
            return usage.for_ref(self.root, request["ref"])
        except sessions.SessionError:
            raise QCRError("not_found", "There is no such conversation.") from None
