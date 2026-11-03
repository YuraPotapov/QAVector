"""Ordinary Cycle node contributions, independent of agents and models."""

from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, TypedDict

from .plugin import ExecutionContext, PluginError, PluginEvent


class NodeRequest(TypedDict):
    schema_version: str
    run_id: str
    node_id: str
    attempt_id: str
    config: Dict[str, Any]
    inputs: Dict[str, Any]
    workspace_ref: Optional[str]
    artifact_refs: List[str]
    output_schema: Any
    limits: Dict[str, Any]


class NodeResult(TypedDict):
    schema_version: str
    attempt_id: str
    contribution_ref: str
    plugin_version: str
    status: str
    summary: str
    output: Any
    artifact_refs: List[str]
    error: Optional[PluginError]


NodeEvent = PluginEvent
NodeEventSink = Callable[[NodeEvent], Awaitable[None]]


class NodeProvider(Protocol):
    async def execute(self, request: NodeRequest, context: ExecutionContext,
                      emit: NodeEventSink) -> NodeResult:
        """Execute one ordinary node through the same scoped host services."""
        ...
