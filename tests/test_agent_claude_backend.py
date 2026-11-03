"""Claude contract and migration checks; no model service or credentials used."""

import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from qavector_agent_claude import ClaudeCliBackend
from qavector_agent_claude.backend import BACKEND_ID
from qavector_sdk import ContractError, validate_contract


def run(coroutine):
    if os.name == "nt":
        return asyncio.run(coroutine)
    from qavector_sdk.event_loop import run as run_loop
    return run_loop(coroutine)


def documents():
    root = Path(__file__).parents[1] / "examples/plugin_contracts"
    profile = json.loads((root / "agent-profile.json").read_text())
    profile.update(id="agent.developer", backend_ref=BACKEND_ID,
                   instructions="Implement the requested change.", limits={"turns": 3},
                   backend_options={"binary": "/approved/claude", "tools": ["Read", "Edit"]})
    request = json.loads((root / "agent-request.json").read_text())
    request.update(task="Fix the parser.", instructions="Preserve compatibility.",
                   workspace_ref="workspace-1", session_ref=None, context_refs=[],
                   artifact_refs=[], limits={"turns": 3}, output_schema=None)
    context = json.loads((root / "execution-context.json").read_text())
    return profile, request, context


class Transport:
    streaming = True

    def __init__(self, packets=(), **outcome):
        self.packets = packets
        self.outcome = dict({"ok": True, "started": True}, **outcome)
        self.calls = []

    async def prerequisites(self, profile, context):
        pass

    async def workspace(self, reference, context):
        assert reference == "workspace-1"
        return "/workspace"

    async def run(self, argv, cwd, on_line):
        self.calls.append((argv, cwd))
        for packet in self.packets:
            await on_line(json.dumps(packet))
        return self.outcome


def execute(transport, change=None):
    profile, request, context = documents()
    if change:
        change(profile, request, context)
    backend, events = ClaudeCliBackend(transport), []

    async def emit(event):
        validate_contract("agent-event", event)
        events.append(event)

    async def call():
        caps = await backend.describe(profile, context)
        validate_contract("agent-capabilities", caps)
        result = await backend.execute(request, context, emit)
        validate_contract("agent-result", result)
        return result

    return run(call()), events


def test_roles_share_backend_and_profile_instructions_precede_the_task():
    transport = Transport([{"type": "result", "result": "Done"}])
    result, events = execute(transport)
    argv, cwd = transport.calls[0]
    assert argv == ["/approved/claude", "-p",
        "Implement the requested change.\n\nPreserve compatibility.\n\nFix the parser.",
        "--output-format", "stream-json", "--verbose", "--allowedTools", "Read Edit",
        "--max-turns", "3"]
    assert cwd == "/workspace" and result["output"] == "Done"
    assert result["usage"] is None and result["session_ref"] is None
    assert events == []  # No fabricated events/metrics when CLI reports none.
    tester = Transport([{"type": "result", "result": "Checked"}])
    execute(tester, lambda p, r, c: p.update(id="agent.tester", instructions="Test the change."))
    assert tester.calls[0][0][2].startswith("Test the change.\n\n")


def test_events_deduplicate_packets_and_report_only_observed_tool_calls():
    packet = {"type": "assistant", "uuid": "packet-1", "message": {"content": [
        {"type": "text", "text": "Checking"},
        {"type": "tool_use", "id": "tool-1", "name": "Read"}]}}
    result, events = execute(Transport([
        {"type": "system", "subtype": "init"}, packet, packet,
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": []},
            {"type": "tool_result", "tool_use_id": "unobserved"},
            {"type": "tool_result", "tool_use_id": "tool-1"}]}},
        {"type": "result", "result": "Done", "total_cost_usd": 0.01,
         "num_turns": 1, "usage": {"input_tokens": 5, "output_tokens": 2}}]))
    assert [e["type"] for e in events] == ["started", "message.delta", "tool.started",
                                          "tool.completed", "usage.updated"]
    assert [e["sequence"] for e in events] == list(range(5))
    assert result["usage"]["metrics"][0] == {"name": "cost", "value": 0.01, "unit": "USD"}


@pytest.mark.parametrize("packet,outcome,status,code", [
    (None, {}, "unknown", "EXECUTION_STATE_UNKNOWN"),
    (None, {"ok": False, "started": False}, "failed", "AGENT_EXECUTION_FAILED"),
    (None, {"ok": False}, "unknown", "EXECUTION_STATE_UNKNOWN"),
    ({"type": "result", "is_error": True, "result": "Refused"}, {}, "failed", "AGENT_EXECUTION_FAILED"),
    ({"type": "result", "result": "Done"}, {"ok": False}, "failed", "AGENT_EXECUTION_FAILED"),
])
def test_known_failure_is_distinct_from_missing_remote_outcome(packet, outcome, status, code):
    result, _ = execute(Transport([] if packet is None else [packet], **outcome))
    assert result["status"] == status and result["error"]["code"] == code


@pytest.mark.parametrize("answer,expected", [
    ({"result": '```json\n{"ok":true}\n```'}, "completed"),
    ({"result": 'Prose {"ok":true} trailing prose'}, "completed"),
    ({"result": "Done", "structured_output": {"ok": True}}, "completed"),
    ({"result": '{"ok":"yes"}'}, "failed"),
    ({"result": "not json"}, "failed"),
])
def test_output_schema_is_enforced(answer, expected):
    result, _ = execute(Transport([dict(type="result", **answer)]),
        lambda p, r, c: r.update(output_schema={"type": "object", "properties": {
            "ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}))
    assert result["status"] == expected
    if expected == "failed":
        assert result["error"]["code"] == "OUTPUT_SCHEMA_INVALID"


@pytest.mark.parametrize("change,code", [
    (lambda p, r, c: p["backend_options"].update(typo=True), "CONFIG_INVALID"),
    (lambda p, r, c: p.update(backend_version=">=2.0.0"), "CONFIG_INVALID"),
    (lambda p, r, c: r.update(session_ref="session-1"), "CAPABILITY_UNSUPPORTED"),
    (lambda p, r, c: r.update(context_refs=["unresolved"]), "CAPABILITY_UNSUPPORTED"),
    (lambda p, r, c: r.update(limits={"turns": 4}), "POLICY_UNSATISFIED"),
])
def test_incompatible_request_never_launches_a_task(change, code):
    transport = Transport()
    with pytest.raises(ContractError) as caught:
        execute(transport, change)
    assert caught.value.code == code and transport.calls == []


def test_default_backend_refuses_execution_in_host_process():
    profile, request, context = documents()
    with pytest.raises(ContractError) as caught:
        run(ClaudeCliBackend().describe(profile, context))
    assert caught.value.code == "POLICY_UNSATISFIED"


def test_cycle_metadata_discovery_does_not_import_backend():
    root = str(Path(__file__).parents[1])
    script = ("import sys; sys.path.insert(0, " + repr(root) + "); "
              "from cycle import registry; registry.describe(); "
              "assert 'qavector_agent_claude.backend' not in sys.modules; "
              "assert 'qavector_sdk.worker' not in sys.modules")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_backend_imports_no_private_core():
    import ast
    root = Path(__file__).parents[1] / "qavector_agent_claude"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = ([node.module or ""] if isinstance(node, ast.ImportFrom) else
                       [item.name for item in node.names] if isinstance(node, ast.Import) else [])
            assert not any(name.split(".")[0] in ("cycle", "domain", "execution_runtime", "plugin_host", "cms_gui")
                           for name in modules), path


def test_legacy_receipt_replays_without_constructing_backend(tmp_path, monkeypatch):
    from cycle.context import RunContext
    from cycle.operations import Operation, result_document
    from cycle import registry
    from cycle.plugins import agent_run
    from domain.cycle import CycleRun, CycleStep
    context = RunContext(CycleRun(id="run", cycle_id="cycle"), None, str(tmp_path))
    step = CycleStep(id="review", plugin="agent.review", settings={})
    # Literal pre-migration argv: this test must not use the new argv builder.
    argv = ["/fake/claude", "-p", "Review", "--output-format", "stream-json",
            "--verbose", "--allowedTools", "Read Glob Grep"]
    operation = Operation(context, step, "agent", {"argv": argv, "cwd": str(tmp_path)})
    operation.begin()
    operation.complete({"result": result_document(registry.succeeded()), "written": [],
        "answer": {"type": "result", "result": "Saved", "total_cost_usd": 0.05},
        "problem": ""}, True)
    monkeypatch.setattr("cycle.claude_backend.ClaudeCliBackend",
                        lambda *a: pytest.fail("Cached response must not construct a backend"))
    context.run.resume_count += 1
    reply = agent_run.run_claude(context, step, "Review", str(tmp_path),
                                agent_run.READ_TOOLS, "/fake/claude")
    assert reply.ok and reply.text == "Saved" and reply.cost == 0.05
    assert reply.agent_result is None


def test_legacy_settings_and_identity_survive_backend_normalization(tmp_path, monkeypatch):
    from cycle import model, registry
    from cycle.context import RunContext
    from cycle.plugins import agent_run
    from domain.cycle import CycleRun
    raw = {"id": "demo", "steps": [{"id": "edit", "plugin": "agent.edit", "with": {
        "task": "Fix", "model": " sonnet ", "effort": "high", "max_turns": "3"}}]}
    original = copy.deepcopy(raw)
    cycle = model.parse_cycle(raw, "demo")
    context = RunContext(CycleRun(id="r", cycle_id="demo"), cycle, str(tmp_path))
    step = cycle.steps[0]
    settings = copy.deepcopy(step.settings)
    calls = []

    def process(ctx, one, argv, cwd, name, **kwargs):
        calls.append(argv)
        Path(ctx.step_dir(one.id), "stdout.log").write_text(
            json.dumps({"type": "result", "result": "Done"}))
        return registry.succeeded()

    monkeypatch.setattr(agent_run, "run_process", process)
    args = dict(model=step.settings["model"], effort=step.settings["effort"],
                max_turns=step.settings["max_turns"], mode="acceptEdits",
                disallowed=["Bash(git push:*)"], add_dir="/read-only")
    reply = agent_run.run_claude(context, step, "Fix", str(tmp_path),
                                agent_run.EDIT_TOOLS, "/fake/claude", **args)
    assert reply.ok and reply.agent_result["backend_ref"] == BACKEND_ID
    assert calls == [["/fake/claude", "-p", "Fix", "--output-format", "stream-json", "--verbose",
        "--allowedTools", "Read Glob Grep Edit Write MultiEdit", "--disallowedTools", "Bash(git push:*)",
        "--max-turns", "3", "--permission-mode", "acceptEdits", "--model", "sonnet",
        "--effort", "high", "--add-dir", "/read-only"]]
    [receipt] = tmp_path.glob("steps/edit/operations/*/receipt.json")
    saved = json.loads(receipt.read_text())["answer"]
    assert saved["agent_result"] == reply.agent_result
    assert saved["agent_profile"]["revision"]
    assert step.settings == settings and raw == original
    context.run.resume_count += 1
    assert agent_run.run_claude(context, step, "Fix", str(tmp_path), agent_run.EDIT_TOOLS,
                                "/fake/claude", **args).agent_result == reply.agent_result
    assert len(calls) == 1
