"""Deterministic worker fixtures, never production backend registrations."""

import asyncio
import os

from examples.plugin_contracts.count_provider import CountProvider
from qavector_sdk.worker import host_call


class Slow(CountProvider):
    async def execute(self, request, context, emit):
        await asyncio.sleep(30)
        return await super().execute(request, context, emit)


class Crash(CountProvider):
    async def execute(self, request, context, emit):
        os._exit(17)


class InvalidOutput(CountProvider):
    async def execute(self, request, context, emit):
        result = await super().execute(request, context, emit)
        result["output"] = {"count": "wrong type"}
        return result


class Noisy(CountProvider):
    async def execute(self, request, context, emit):
        print("ordinary plugin diagnostic")
        return await super().execute(request, context, emit)


class ScopedCall(CountProvider):
    async def execute(self, request, context, emit):
        response = await host_call(context["tools_handle"], "counter.read", {})
        result = await super().execute(request, context, emit)
        result["output"] = response
        return result


class UnverifiedArtifact(CountProvider):
    async def execute(self, request, context, emit):
        result = await super().execute(request, context, emit)
        result["artifact_refs"] = ["/etc/passwd"]
        return result


class FakeAgent:
    """Conformance fixture, not a substitute for a real second backend."""

    async def describe(self, profile, context):
        return {"schema_version": "1.0.0", "workspace_access": "read",
                "structured_output": "validated_text",
                "schema_dialects": ["https://json-schema.org/draft/2020-12/schema"],
                "streaming": False, "tool_events": "none", "tool_binding": [],
                "session_resume": False, "interactive_input": False,
                "cancellation": "confirmed", "limits": {"wall_time_seconds": "hard"},
                "content_types": ["text"]}

    async def execute(self, request, context, emit):
        return {"schema_version": "1.0.0", "attempt_id": request["attempt_id"],
                "backend_ref": "example.agent.review", "plugin_version": "1.0.0",
                "status": "completed", "summary": "Deterministic review fixture",
                "output": {"verdict": "changes_required"}, "artifact_refs": [],
                "session_ref": None, "usage": None, "error": None}
