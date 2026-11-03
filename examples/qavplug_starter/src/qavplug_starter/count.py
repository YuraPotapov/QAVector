"""An ordinary data-processing node: no model, network, files or processes."""

from . import __version__


class CountProvider:
    async def execute(self, request, context, emit):
        count = sum(value > request["config"]["threshold"]
                    for value in request["inputs"]["values"])
        return {
            "schema_version": "1.0.0",
            "attempt_id": request["attempt_id"],
            "contribution_ref": "example.starter.count",
            "plugin_version": __version__,
            "status": "completed",
            "summary": "Counted %d values above the threshold." % count,
            "output": {"count": count},
            "artifact_refs": [],
            "error": None,
        }
