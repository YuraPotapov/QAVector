"""Backend signatures and wire types; JSON Schemas define runtime validation.

Async is confined to the future worker SDK. The synchronous Cycle engine is
not converted to asyncio. Handles below are opaque IDs, never core objects.
"""

from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, TypedDict

from .plugin import ExecutionContext, PluginError, PluginEvent


AgentError = PluginError
AgentEvent = PluginEvent


class AgentProfile(TypedDict):
    schema_version: str
    id: str
    revision: str
    backend_ref: str
    backend_version: str
    model: Optional[str]
    instructions: str
    credentials_ref: Optional[str]
    limits: Dict[str, Any]
    backend_options: Dict[str, Any]


class AgentRequest(TypedDict):
    schema_version: str
    run_id: str
    node_id: str
    attempt_id: str
    task: str
    instructions: str
    workspace_ref: Optional[str]
    context_refs: List[str]
    artifact_refs: List[str]
    output_schema: Any
    limits: Dict[str, Any]
    session_ref: Optional[str]


class AgentResult(TypedDict):
    schema_version: str
    attempt_id: str
    backend_ref: str
    plugin_version: str
    status: str
    summary: str
    output: Any
    artifact_refs: List[str]
    session_ref: Optional[str]
    usage: Optional[Dict[str, Any]]
    error: Optional[AgentError]


class AgentCapabilities(TypedDict):
    schema_version: str
    workspace_access: str
    structured_output: str
    schema_dialects: List[str]
    streaming: bool
    tool_events: str
    tool_binding: List[str]
    session_resume: bool
    interactive_input: bool
    cancellation: str
    limits: Dict[str, str]
    content_types: List[str]


EventSink = Callable[[AgentEvent], Awaitable[None]]


class AgentBackend(Protocol):
    async def describe(self, profile: AgentProfile,
                       context: ExecutionContext) -> AgentCapabilities:
        """Report effective capabilities after checking prerequisites."""
        ...

    async def execute(self, request: AgentRequest, context: ExecutionContext,
                      emit: EventSink) -> AgentResult:
        """Execute one attempt through scoped host services."""
        ...
