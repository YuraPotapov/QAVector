"""A deterministic, non-agent NodeProvider; source example, not an installer.

The host validates config and inputs against the manifest's static schemas
before calling execute. No file/process/network access is needed by this node.
"""

from datetime import datetime, timezone
from uuid import uuid4

from qavector_sdk.node import NodeEventSink, NodeRequest, NodeResult
from qavector_sdk.plugin import ExecutionContext


class CountProvider:
    async def execute(self, request: NodeRequest, context: ExecutionContext,
                      emit: NodeEventSink) -> NodeResult:
        await emit({
            "schema_version": "1.0.0", "event_id": str(uuid4()),
            "attempt_id": request["attempt_id"], "sequence": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(), "type": "started",
            "payload": {"execution_id": context["execution_id"]},
        })
        count = sum(value > request["config"]["threshold"]
                    for value in request["inputs"]["values"])
        return {
            "schema_version": "1.0.0", "attempt_id": request["attempt_id"],
            "contribution_ref": "example.metrics.count", "plugin_version": "1.0.0",
            "status": "completed", "summary": "Counted %d values above the threshold." % count,
            "output": {"count": count}, "artifact_refs": [], "error": None,
        }
