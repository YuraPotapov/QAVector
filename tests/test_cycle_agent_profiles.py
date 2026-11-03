"""Named agent roles, backward-compatible files and durable profile identity."""

import copy
import json
import os
from pathlib import Path
import sys

import pytest

from cycle import checkpoints, executor, model, registry, restrict
from cycle.agent_profiles import ProfileError, normalize, selected
from cycle.context import RunContext
from cycle.plugins.profile_agent import ProfileAgent
from domain.cycle import CycleRun


def declaration(binary="/fake/claude", instructions="Analyze the requirements."):
    return {"backend_ref": "qavector.agent.claude_cli", "model": "sonnet",
            "instructions": instructions, "limits": {"turns": 3},
            "backend_options": {"binary": binary, "tools": ["Read", "Glob", "Grep"]}}


def workflow(profile=None, **settings):
    return model.parse_cycle({"id": "roles", "agent_profiles": {
        "agent.business.analyst": profile or declaration()}, "steps": [{
            "id": "analyze", "plugin": "agent", "with": {
                "agent_profile": "agent.business.analyst", "task": "Inspect the task.", **settings}}]}, "roles")


def test_profiles_round_trip_without_materializing_defaults():
    cycle = workflow()
    document = model.to_document(cycle)
    assert document["agent_profiles"]["agent.business.analyst"] == declaration()
    assert model.parse_cycle(document, "roles") == cycle
    assert model.to_graph(cycle)["agent_profiles"] == document["agent_profiles"]
    assert model.problems(cycle, registry, document) == []
    document["agent_profiles"]["agent.business.analyst"]["instructions"] = "Changed"
    assert cycle.agent_profiles["agent.business.analyst"]["instructions"] == declaration()["instructions"]
    old = model.parse_cycle({"steps": []}, "old")
    assert "agent_profiles" not in model.to_document(old)
    assert "agent_profiles" not in model.to_graph(old)


def test_cycle_file_writer_and_editor_round_trip_preserve_role_definitions(tmp_path):
    from cycle.cyclefile import describe_cycle, save
    document = model.to_document(workflow())
    document["agent_profiles"]["agent.business.analyst"]["instructions"] = "Analyze requirements.\nExplain gaps.\n"
    for _ in range(2):
        result = save("roles", str(tmp_path), document=document, registry=registry)
        assert result["ok"], result["problems"]
        opened = describe_cycle("roles", str(tmp_path), registry=registry)
        assert opened["document"]["agent_profiles"] == document["agent_profiles"]
        document = opened["document"]


def test_isolated_profile_node_never_falls_back_to_host(tmp_path, monkeypatch):
    from cycle.runtime import CycleRuntime
    cycle = workflow()
    cycle.execution = {"runtime": {"mode": "isolated", "deployment": "demo",
        "revision": "a" * 64, "project_directory": str(tmp_path)}}
    monkeypatch.setattr("cycle.runtime.CycleRuntime.resolve", lambda *a: pytest.fail("Unsupported node must refuse before deployment use"))
    with pytest.raises(checkpoints.CheckpointError, match="host execution is forbidden"):
        CycleRuntime(cycle, registry, str(tmp_path))


def test_automatic_revision_tracks_content_and_does_not_mutate_declaration():
    raw = declaration()
    before = copy.deepcopy(raw)
    profile = normalize("agent.developer", raw)
    assert profile["revision"] == normalize("agent.developer", raw)["revision"]
    assert raw == before
    raw["instructions"] = "Implement the parser."
    assert normalize("agent.developer", raw)["revision"] != profile["revision"]


@pytest.mark.parametrize("raw", [[], {"backend_ref": "invalid"},
    dict(declaration(), id="another"), dict(declaration(), instructions=7),
    dict(declaration(), instructions="Use ${vars.secret}"),
    dict(declaration(), permissions=["all"]), dict(declaration(), revision="")])
def test_malformed_profiles_are_reported_without_execution(raw):
    cycle = workflow()
    cycle.agent_profiles["agent.business.analyst"] = raw
    assert model.problems(cycle, registry)


@pytest.mark.parametrize("change", [
    {"backend_ref": "external.agent.uninstalled"},
    {"backend_version": ">=2.0.0"},
    {"credentials_ref": "credentials/unbound"},
    {"limits": {"tokens": 10}},
    {"backend_options": {"binary": "/fake/claude", "tools": [], "typo": True}},
])
def test_unavailable_profile_features_fail_closed(change, tmp_path, monkeypatch):
    raw = declaration()
    raw.update(change)
    cycle = workflow(raw)
    monkeypatch.setattr("cycle.plugins.agent_cli.auth_status", lambda *a, **k: pytest.fail("No auth on invalid profile"))
    assert model.problems(cycle, registry)
    result = ProfileAgent().execute(RunContext(CycleRun(id="r", cycle_id="roles"), cycle, str(tmp_path)), cycle.steps[0])
    assert not result.ok


def test_unknown_profile_and_expression_reference_are_rejected():
    cycle = workflow(agent_profile="agent.missing")
    assert any("unknown agent_profile" in p for p in model.problems(cycle, registry))
    with pytest.raises(ProfileError):
        selected(cycle, "agent.missing")
    assert ProfileAgent().problems({"agent_profile": "${vars.role}", "task": "Do it"})


def test_only_referenced_profile_content_changes_step_fingerprint_even_with_same_revision():
    cycle = workflow(dict(declaration(), revision="1"))
    before = checkpoints.signatures(cycle, {}, registry)
    cycle.agent_profiles["unused"] = declaration(instructions="Another role")
    assert checkpoints.signatures(cycle, {}, registry) == before
    cycle.agent_profiles["agent.business.analyst"]["instructions"] = "Different task contract"
    assert checkpoints.signatures(cycle, {}, registry) != before


def test_profile_agent_is_subject_to_cycle_restrictions():
    step = workflow().steps[0]
    assert not restrict.is_system(step)
    assert step.plugin in restrict.NEEDS_NETWORK
    step.system = True
    assert restrict.is_system(step)


@pytest.fixture
def cli(tmp_path):
    if os.name == "nt":
        pytest.skip("Executable CLI fixture uses a POSIX shebang")
    from claude_backend_fixture import fake_cli
    calls = tmp_path / "calls.jsonl"
    binary = fake_cli(tmp_path / "claude", sys.executable, body=(
        "prompt = sys.argv[sys.argv.index('-p') + 1]\n"
        "with open(%r, 'a') as handle: handle.write(json.dumps(sys.argv) + '\\n')\n"
        "print(json.dumps({'type': 'result', 'result': json.dumps({'decision': 'approved'}), 'total_cost_usd': 0.01}))\n"
        % str(calls)))
    return binary, calls


def test_role_chain_runs_from_yaml_and_reuses_receipts_without_rewriting_file(tmp_path, cli):
    import yaml
    binary, calls = cli
    profiles = {name: declaration(binary, instruction) for name, instruction in (
        ("agent.business.analyst", "Analyze requirements."),
        ("agent.developer", "Implement the requirements."),
        ("agent.tester", "Test the implementation."))}
    schema = {"type": "object", "properties": {"decision": {"enum": ["approved"]}},
              "required": ["decision"], "additionalProperties": False}
    raw = {"id": "roles", "agent_profiles": profiles, "steps": []}
    for index, role in enumerate(profiles):
        step = {"id": "step%d" % index, "plugin": "agent", "with": {
            "agent_profile": role, "task": "Work on the parser.", "output_schema": schema}}
        if index:
            step["needs"] = ["step%d" % (index - 1)]
            step["with"]["inputs"] = {"previous": "${steps.step%d.outputs.output}" % (index - 1)}
        raw["steps"].append(step)
    # Leave a local report failed so Resume has unfinished work to retry.
    class Report(registry.CyclePlugin):
        metadata = registry.PluginMetadata(id="test.report", name="Report")

        def execute(self, context, step):
            return registry.failed("report writer unavailable")

    report = Report()

    class Plugins:
        @staticmethod
        def get(identifier):
            return report if identifier == "test.report" else registry.get(identifier)

    raw["steps"].append({"id": "report", "plugin": "test.report", "needs": ["step2"]})
    path = tmp_path / "roles.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    original = path.read_bytes()
    cycle = model.parse_cycle(yaml.safe_load(original), "roles", str(path))
    workspace = str(tmp_path / "run")
    first = executor.run_cycle(cycle, workspace, registry=Plugins)
    assert first.status == "failed" and first.steps["report"].status == "failed"
    assert all(first.steps["step%d" % i].status == "success" for i in range(3)), [s.message for s in first.steps.values()]
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == 3
    for index, role in enumerate(profiles):
        argv = invocations[index]
        prompt = argv[argv.index("-p") + 1]
        assert prompt.startswith(profiles[role]["instructions"] + "\n\n")
        if index:
            assert '"previous": {' in prompt and '"decision": "approved"' in prompt
        assert first.steps["step%d" % index].outputs["output"] == {"decision": "approved"}
    resumed = executor.run_cycle(cycle, workspace, registry=Plugins, resume=True)
    assert resumed.status == "failed" and resumed.steps["report"].status == "failed"
    assert len(calls.read_text().splitlines()) == 3
    assert path.read_bytes() == original
    # A user-supplied revision must not hide changed instructions on Resume.
    cycle.agent_profiles["agent.tester"]["instructions"] = "Test edge cases too."
    with pytest.raises(checkpoints.CheckpointError, match="Completed steps changed"):
        executor.run_cycle(cycle, workspace, registry=Plugins, resume=True)
    assert len(calls.read_text().splitlines()) == 3


def test_invalid_output_is_saved_and_retry_does_not_buy_another_response(tmp_path, cli):
    binary, calls = cli
    cycle = workflow(declaration(binary), output_schema={"type": "object", "required": ["missing"]})
    first = executor.run_cycle(cycle, str(tmp_path / "run"), registry=registry)
    assert first.status == "failed"
    assert first.steps["analyze"].outputs["agent_result"]["error"]["code"] == "OUTPUT_SCHEMA_INVALID"
    second = executor.run_cycle(cycle, str(tmp_path / "run"), registry=registry, resume=True)
    assert second.status == "failed"
    assert len(calls.read_text().splitlines()) == 1


def test_profile_deadline_stops_the_cli_and_preserves_uncertainty(tmp_path):
    if os.name == "nt":
        pytest.skip("Executable CLI fixture uses a POSIX shebang")
    from claude_backend_fixture import fake_cli
    binary = fake_cli(tmp_path / "slow-claude", sys.executable,
                      body="import time\ntime.sleep(30)\n")
    profile = declaration(binary)
    profile["limits"] = {"wall_time_seconds": 1}
    cycle = workflow(profile)
    run = executor.run_cycle(cycle, str(tmp_path / "run"), registry=registry)
    assert run.status == "failed"
    assert run.steps["analyze"].status == "timeout"
    assert run.steps["analyze"].outputs["agent_result"]["status"] == "unknown"
    [receipt] = Path(run.workspace).glob("steps/analyze/operations/*/receipt.json")
    assert json.loads(receipt.read_text())["state"] == "started"
