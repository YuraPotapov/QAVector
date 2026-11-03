"""Shared contracts for every executable plugin contribution."""

from typing import Any, Dict, Optional, TypedDict


class ExecutionContext(TypedDict):
    schema_version: str
    execution_id: str
    runtime_handle: str
    tools_handle: Optional[str]
    credentials_handle: Optional[str]
    cancellation_handle: str


class PluginError(TypedDict):
    schema_version: str
    code: str
    message: str
    retryable: bool
    side_effects: str
    details: Dict[str, Any]


class PluginEvent(TypedDict):
    schema_version: str
    event_id: str
    attempt_id: str
    sequence: int
    timestamp: str
    type: str
    payload: Dict[str, Any]
