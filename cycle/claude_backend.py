"""Bridge existing Cycle process supervision to the public AgentBackend contract.

Only this trusted adapter holds core objects/callbacks. They never become worker
parameters. Legacy authentication, streaming UI and process policy stay with
their existing owners; the backend owns Claude argv and response normalization.
"""

import asyncio
import copy
import os

from cycle.checkpoints import digest
from qavector_agent_claude import ClaudeCliBackend
from qavector_agent_claude.backend import BACKEND_ID, config_schema
from qavector_sdk import preflight, validate_contract


class LegacyTransport:
    # The original UI streams live; contract events are reconstructed from the
    # bounded log tail after exit, so this transport does not advertise streaming.
    streaming = False

    def __init__(self, process, read_stdout, cwd):
        self.process, self.read_stdout, self.cwd = process, read_stdout, cwd
        self.result = None

    async def prerequisites(self, profile, context):
        # Existing node callers already resolved the binary and checked login.
        pass

    async def workspace(self, reference, context):
        return self.cwd

    async def run(self, argv, cwd, on_line):
        self.result = self.process(argv, cwd)
        for line in self.read_stdout(self.result).splitlines():
            await on_line(line)
        return {"ok": self.result.ok, "message": self.result.message,
                "started": self.result.outputs.get("started", True),
                "stdout_path": self.result.outputs.get("stdout_path", "the step log")}


def run_legacy_claude(*, run_id, node_id, operation_id, epoch, prompt, cwd,
                     binary, options, process, read_stdout, agent_profile=None,
                     output_schema=None):
    """Normalize old settings internally without changing Cycle YAML or argv.

    The process callback starts the durable Operation only after validation.
    Replaying an old receipt bypasses this function altogether.
    """
    limits = ({} if options["max_turns"] in (None, "") else
              {"turns": int(options["max_turns"])})
    profile = {
        "schema_version": "1.0.0", "id": "legacy." + node_id,
        "revision": "pending", "backend_ref": BACKEND_ID,
        "backend_version": ">=1.0.0,<2.0.0",
        "model": str(options["model"] or "").strip() or None,
        "instructions": "", "credentials_ref": None, "limits": limits,
        "backend_options": {"binary": binary, "tools": list(options["tools"]),
            "permission_mode": options["mode"],
            "effort": str(options["effort"] or "").strip(),
            "additional_directory": options["add_dir"],
            "disallowed_tools": list(options["disallowed"])}}
    profile["revision"] = digest(profile)
    if agent_profile is not None:
        profile = copy.deepcopy(agent_profile)
        limits = profile["limits"]
    attempt_id = digest([run_id, node_id, operation_id, epoch])
    context = {"schema_version": "1.0.0", "execution_id": attempt_id,
               "runtime_handle": "legacy.process", "tools_handle": None,
               "credentials_handle": None, "cancellation_handle": "legacy.cancel"}
    request = {"schema_version": "1.0.0", "run_id": run_id, "node_id": node_id,
               "attempt_id": attempt_id, "task": prompt, "instructions": "",
               "workspace_ref": "legacy.workspace" if options["tools"] else None,
               "context_refs": [], "artifact_refs": [], "output_schema": output_schema,
               "limits": dict(limits), "session_ref": None}
    transport = LegacyTransport(process, read_stdout, cwd)
    backend = ClaudeCliBackend(transport)

    async def receive(event):
        validate_contract("agent-event", event)

    async def execute():
        capabilities = await backend.describe(profile, context)
        preflight(profile, request, capabilities, {"schema_version": "1.0.0"},
                  config_schema=config_schema())
        return await backend.execute(request, context, receive)

    if os.name == "nt":
        result = asyncio.run(execute())
    else:
        # Cycle's synchronous supervisor also works where socketpair is denied.
        from qavector_sdk.event_loop import run
        result = run(execute())
    return transport.result, backend.last_answer, result, {
        "id": profile["id"], "revision": profile["revision"],
        "backend_ref": profile["backend_ref"], "backend_version": profile["backend_version"]}
