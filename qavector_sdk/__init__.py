"""Portable plugin contracts with agent and node APIs (development preview).

This package must not import Cycle, Execution Runtime, Qt or backend code.
Validation does not confer trust, authorize execution or validate an archive.
"""

from .contracts import AgentBackend, ExecutionContext, EventSink
from .node import NodeProvider, NodeEventSink
from .plugin import PluginError, PluginEvent
from .validation import (ContractError, load_schema, validate_contract,
                         validate_output, validate_schema)
from .preflight import preflight

__all__ = ["AgentBackend", "NodeProvider", "NodeEventSink", "PluginError",
           "PluginEvent", "ExecutionContext", "EventSink", "ContractError",
           "load_schema", "validate_contract", "validate_output", "validate_schema",
           "preflight"]
