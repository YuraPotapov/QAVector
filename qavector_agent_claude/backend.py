"""Claude-specific commands, prerequisites and normalized agent output."""

import copy
from datetime import datetime, timezone
from importlib import resources
import json
import math
import re
from uuid import uuid4

from qavector_sdk import ContractError, validate_contract, validate_output
from qavector_sdk.ipc import failure
from qavector_sdk.versions import satisfies

from .command import command

BACKEND_ID = "qavector.agent.claude_cli"
VERSION = json.loads(resources.files("qavector_agent_claude").joinpath(
    "manifest.json").read_text(encoding="utf-8"))["version"]


def config_schema():
    return json.loads(resources.files("qavector_agent_claude").joinpath("schemas").joinpath(
        "config.json").read_text(encoding="utf-8"))


def parse_packet(line):
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def last_result(text):
    for line in reversed((text or "").splitlines()):
        event = parse_packet(line)
        if event is not None and event.get("type") == "result":
            return event
    return None


def structured_output(answer):
    if answer.get("structured_output") is not None:
        return answer["structured_output"]
    text = str(answer.get("result") or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # Preserve the established acceptance of prose/fences around a JSON object.
    try:
        return json.loads(text)
    except ValueError:
        start = text.find("{")
        if start < 0:
            raise ContractError("OUTPUT_SCHEMA_INVALID", "No structured output in Claude response") from None
        try:
            return json.JSONDecoder().raw_decode(text[start:])[0]
        except ValueError:
            raise ContractError("OUTPUT_SCHEMA_INVALID", "Invalid JSON in Claude response") from None


def usage(answer):
    metrics = []
    fields = [("cost", answer.get("total_cost_usd"), "USD"),
              ("turns", answer.get("num_turns"), "turns")]
    reported = answer.get("usage")
    if isinstance(reported, dict):
        fields += [(key, reported.get(key), "tokens") for key in
                   ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    for name, value, unit in fields:
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            metrics.append({"name": name, "value": value, "unit": unit})
    return {"source": "claude_cli", "metrics": metrics} if metrics else None


class ClaudeCliBackend:
    def __init__(self, transport=None):
        # A trusted compatibility adapter may inject its existing process
        # supervisor. The default transport refuses to run outside SDK worker.
        if transport is None:
            from .transport import WorkerTransport
            transport = WorkerTransport()
        self.transport = transport
        self.profile = None
        self.execution_id = None
        self.last_answer = None

    async def describe(self, profile, context):
        validate_contract("agent-profile", profile)
        validate_contract("execution-context", context)
        validate_output(profile["backend_options"], config_schema(), code="CONFIG_INVALID")
        if profile["backend_ref"] != BACKEND_ID:
            raise ContractError("CONFIG_INVALID", "Profile selects a different backend")
        if not satisfies(VERSION, profile["backend_version"]):
            raise ContractError("CONFIG_INVALID", "Profile requires a different backend version")
        await self.transport.prerequisites(profile, context)
        self.profile, self.execution_id = copy.deepcopy(profile), context["execution_id"]
        options = profile["backend_options"]
        tools = options["tools"]
        access = "write" if any(tool.split("(", 1)[0] in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")
                                for tool in tools) else "read" if tools else "none"
        return {"schema_version": "1.0.0", "workspace_access": access,
                "structured_output": "validated_text",
                "schema_dialects": ["https://json-schema.org/draft/2020-12/schema"],
                "streaming": self.transport.streaming, "tool_events": "partial",
                "tool_binding": [], "session_resume": False, "interactive_input": False,
                "cancellation": "request", "limits": {"turns": "best_effort"},
                "content_types": ["text"]}

    async def execute(self, request, context, emit):
        validate_contract("agent-request", request)
        validate_contract("execution-context", context)
        if self.profile is None or self.execution_id != context["execution_id"]:
            raise ContractError("AGENT_PREREQUISITE_MISSING", "Describe must succeed for this execution first")
        if request["session_ref"] is not None:
            raise ContractError("CAPABILITY_UNSUPPORTED", "Provider session resume is not enabled")
        if request["context_refs"] or request["artifact_refs"]:
            raise ContractError("CAPABILITY_UNSUPPORTED", "Resolve evidence into task text before calling this backend")
        options = self.profile["backend_options"]
        for key, ceiling in self.profile["limits"].items():
            if key not in request["limits"] or request["limits"][key] > ceiling:
                raise ContractError("POLICY_UNSATISFIED", "Request widens profile limit: " + key)
        prompt = "\n\n".join(part for part in (self.profile["instructions"],
                             request["instructions"], request["task"]) if part)
        argv = command(options["binary"], prompt, options["tools"],
                       mode=options.get("permission_mode", ""), model=self.profile["model"] or "",
                       max_turns=request["limits"].get("turns"), effort=options.get("effort", ""),
                       add_dir=options.get("additional_directory", ""), disallowed=options.get("disallowed_tools", []))
        cwd = await self.transport.workspace(request["workspace_ref"], context)
        sequence, tool_ids, packet_ids = 0, set(), set()

        async def publish(kind, payload):
            nonlocal sequence
            event = {"schema_version": "1.0.0", "event_id": uuid4().hex,
                     "attempt_id": request["attempt_id"], "sequence": sequence,
                     "timestamp": datetime.now(timezone.utc).isoformat(), "type": kind, "payload": payload}
            sequence += 1
            await emit(event)

        async def line_received(line):
            packet = parse_packet(line)
            if not packet:
                return
            if packet.get("type") == "result":
                self.last_answer = packet
            packet_id = packet.get("uuid")
            if isinstance(packet_id, str):
                if packet_id in packet_ids:
                    return
                packet_ids.add(packet_id)
            if packet.get("type") == "system" and packet.get("subtype") == "init":
                await publish("started", {"execution_id": context["execution_id"]})
            message = packet.get("message")
            blocks = message.get("content", []) if isinstance(message, dict) else []
            if not isinstance(blocks, list):
                blocks = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text" and isinstance(block.get("text"), str):
                    await publish("message.delta", {"text": block["text"]})
                elif kind == "tool_use" and isinstance(block.get("id"), str) and block["id"] and isinstance(block.get("name"), str) and block["name"]:
                    if block["id"] not in tool_ids:
                        tool_ids.add(block["id"])
                        await publish("tool.started", {"call_id": block["id"], "name": block["name"]})
                elif kind == "tool_result" and isinstance(block.get("tool_use_id"), str) and block["tool_use_id"] in tool_ids:
                    tool_ids.remove(block["tool_use_id"])
                    await publish("tool.completed", {"call_id": block["tool_use_id"],
                        "status": "failed" if block.get("is_error") else "completed"})
            if packet.get("type") == "result" and usage(packet) is not None:
                await publish("usage.updated", {"usage": usage(packet)})

        self.last_answer = None
        outcome = await self.transport.run(argv, cwd, line_received)
        answer = self.last_answer
        result = {"schema_version": "1.0.0", "attempt_id": request["attempt_id"],
                  "backend_ref": BACKEND_ID, "plugin_version": VERSION,
                  "status": "completed", "summary": "", "output": None,
                  "artifact_refs": [], "session_ref": None, "usage": usage(answer) if answer else None,
                  "error": None}
        problem, code = "", "AGENT_EXECUTION_FAILED"
        if not outcome["ok"]:
            problem = outcome.get("message") or "Claude process failed"
        elif answer is None:
            problem = "Claude Code ended without a result. Its output is in %s" % outcome.get("stdout_path", "the step log")
        elif answer.get("is_error"):
            problem = "Claude Code refused: %s" % str(answer.get("result") or answer.get("subtype") or "")[:200]
        if problem:
            result["status"] = "unknown" if answer is None and outcome.get("started", True) else "failed"
            code = "EXECUTION_STATE_UNKNOWN" if result["status"] == "unknown" else code
            result.update(summary=problem, error=failure(code, problem,
                          side_effects="none" if outcome.get("started") is False else "unknown"))
        else:
            text = str(answer.get("result") or "").strip()
            result["summary"] = text
            try:
                result["output"] = text if request["output_schema"] is None else structured_output(answer)
                if request["output_schema"] is not None:
                    validate_output(result["output"], request["output_schema"])
            except ContractError:
                result.update(status="failed", summary="Claude output does not satisfy the requested schema",
                              output=None, error=failure("OUTPUT_SCHEMA_INVALID", "Claude output does not satisfy the requested schema"))
        validate_contract("agent-result", result)
        return result
