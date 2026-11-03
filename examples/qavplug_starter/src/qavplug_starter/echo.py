"""AgentBackend contract example; echoes text without an AI/provider request."""

from . import __version__


class EchoBackend:
    def __init__(self):
        self.prefix = ""

    async def describe(self, profile, context):
        self.prefix = profile["backend_options"].get("prefix", "")
        return {
            "schema_version": "1.0.0",
            "workspace_access": "none",
            "structured_output": "native",
            "schema_dialects": ["https://json-schema.org/draft/2020-12/schema"],
            "streaming": False,
            "tool_events": "none",
            "tool_binding": [],
            "session_resume": False,
            "interactive_input": False,
            "cancellation": "confirmed",  # The managed host confirms process cleanup.
            "limits": {"wall_time_seconds": "hard"},
            "content_types": ["text"],
        }

    async def execute(self, request, context, emit):
        return {
            "schema_version": "1.0.0",
            "attempt_id": request["attempt_id"],
            "backend_ref": "example.starter.echo",
            "plugin_version": __version__,
            "status": "completed",
            "summary": "Echoed the supplied task; no model was called.",
            "output": {"answer": self.prefix + request["task"]},
            "artifact_refs": [],
            "session_ref": None,
            "usage": None,
            "error": None,
        }
