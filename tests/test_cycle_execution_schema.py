"""Execution policy metadata stays explicit and preserves old run contracts."""

import json
from dataclasses import asdict

import pytest

from cycle import checkpoints, cyclefile, model, registry, run as records
from domain.cycle import CycleRun, FAILED, PENDING, StepRun
from engine import yamlload


def document(**extra):
    return {"id": "demo", "steps": [{"id": "work", "plugin": "command.shell"}], **extra}


def parse(raw=None):
    return model.parse_cycle(document() if raw is None else raw, "demo")


def test_legacy_cycle_does_not_gain_explicit_execution_metadata():
    cycle = parse()
    assert cycle.execution == {}
    assert cycle.steps[0].important is False
    assert cycle.steps[0].importance_reason == ""
    assert model.to_document(cycle) == document()
    graph = model.to_graph(cycle)
    assert "execution" not in graph
    assert "important" not in graph["nodes"][0]
    assert "importance_reason" not in graph["nodes"][0]
    assert model.warnings(cycle) == []


@pytest.mark.parametrize("mode", ["auto", "important", "approve"])
def test_execution_mode_and_node_metadata_round_trip_as_explicit_values(mode):
    raw = document(execution={"default_mode": mode})
    raw["steps"][0].update(important=True, importance_reason="Changes the remote branch")
    cycle = parse(raw)
    assert model.problems(cycle, raw=raw) == []
    assert model.warnings(cycle) == []
    assert model.to_document(cycle) == raw
    graph = model.to_graph(cycle)
    assert graph["execution"] == raw["execution"]
    assert graph["nodes"][0]["important"] is True
    assert graph["nodes"][0]["importance_reason"] == "Changes the remote branch"
    assert yamlload.safe_load(cyclefile.render(raw)) == raw


@pytest.mark.parametrize("value", ["false", "true", "yes", 0, 1, None, [], {}])
def test_important_accepts_only_boolean_values(value):
    raw = document()
    raw["steps"][0]["important"] = value
    with pytest.raises(model.CycleError, match="important must be a boolean"):
        parse(raw)


@pytest.mark.parametrize("value", [False, True])
def test_boolean_important_values_are_accepted(value):
    raw = document()
    raw["steps"][0]["important"] = value
    assert parse(raw).steps[0].important is value


@pytest.mark.parametrize("value", [None, [], False, "important", 1])
def test_execution_requires_a_mapping(value):
    with pytest.raises(model.CycleError, match="execution must be a mapping"):
        parse(document(execution=value))


@pytest.mark.parametrize("value", [None, [], False, 1, {}])
def test_default_mode_requires_text(value):
    with pytest.raises(model.CycleError, match="default_mode must be text"):
        parse(document(execution={"default_mode": value}))


@pytest.mark.parametrize("value", [None, [], True, 1, {}])
def test_importance_reason_requires_text_when_supplied(value):
    raw = document()
    raw["steps"][0]["importance_reason"] = value
    with pytest.raises(model.CycleError, match="importance_reason must be text"):
        parse(raw)


@pytest.mark.parametrize("mode", ["", "Auto", "manual", " important "])
def test_unknown_mode_is_a_fatal_validation_problem(mode):
    raw = document(execution={"default_mode": mode})
    found = model.problems(parse(raw), raw=raw)
    assert any("execution.default_mode" in message for message in found)
    assert not model.is_valid(parse(raw), raw=raw)


def test_unknown_execution_key_is_rejected_without_dropping_it_in_the_preview():
    raw = document(execution={"default_mode": "important", "mode": "auto"})
    assert model.to_document(parse(raw)) == raw
    assert "execution: unknown key 'mode'" in model.problems(parse(raw), raw=raw)


def test_important_without_reason_warns_and_can_still_be_saved(tmp_path):
    raw = document()
    raw["steps"][0]["important"] = True
    cycle = parse(raw)
    assert model.problems(cycle, raw=raw) == []
    assert model.warnings(cycle) == [
        "work: important step has no importance_reason; explain why it needs attention."]
    answer = cyclefile.save("demo", cycles_dir=str(tmp_path), document=raw)
    assert answer["ok"], answer
    shown = cyclefile.describe_cycle("demo", str(tmp_path))
    assert shown["problems"] == []
    assert shown["warnings"] == model.warnings(cycle)
    raw["steps"][0].pop("important")
    assert cyclefile.save("demo", cycles_dir=str(tmp_path), document=raw)["ok"]
    assert "warnings" not in cyclefile.describe_cycle("demo", str(tmp_path))


def test_reason_is_preserved_even_without_the_important_flag():
    raw = document()
    raw["steps"][0]["importance_reason"] = "Future review note"
    cycle = parse(raw)
    assert model.to_document(cycle) == raw
    assert model.warnings(cycle) == []


def test_legacy_fingerprint_matches_the_definition_before_policy_fields_existed():
    cycle = parse()
    step = asdict(cycle.steps[0])
    for name in ("label", "source_index", "group", "retry_backoff", "phase",
                 "important", "importance_reason", "runtime", "restrictions", "system"):
        step.pop(name)
    plugin = registry.get("command.shell")
    expected = checkpoints.digest({"step": step, "variables": {},
                                   "version": plugin.metadata.version})
    assert checkpoints.signatures(cycle, {}, registry) == {"work": expected}


def test_changed_default_does_not_change_node_fingerprints():
    cycle = parse(document(execution={"default_mode": "important"}))
    old = checkpoints.signatures(cycle, {}, registry)
    cycle.execution["default_mode"] = "approve"
    assert checkpoints.signatures(cycle, {}, registry) == old
    cycle.steps[0].important = True
    assert checkpoints.signatures(cycle, {}, registry) != old


def test_policy_fields_in_run_records_round_trip_and_legacy_defaults_stay_empty():
    run = CycleRun("run-id", "demo", initial_execution_mode="important",
                   execution_mode="approve", execution_policy_version=1,
                   node_approvals={"request-id": {"decision": "approved"}},
                   policy_changes=[{"from": "important", "to": "approve"}])
    loaded = records.from_document(records.to_document(run))
    for field in ("initial_execution_mode", "execution_mode", "execution_policy_version",
                  "node_approvals", "policy_changes"):
        assert getattr(loaded, field) == getattr(run, field)
    legacy = records.from_document({"id": "legacy", "cycle_id": "demo"})
    assert legacy.initial_execution_mode == legacy.execution_mode == ""
    assert legacy.node_approvals == {}
    assert legacy.policy_changes == []


def test_resume_keeps_initial_and_current_modes_when_yaml_default_changes(tmp_path):
    cycle = parse(document(execution={"default_mode": "important"}))
    run = CycleRun("run-id", "demo", workspace=str(tmp_path), status=FAILED,
                   initial_execution_mode="important", execution_mode="approve",
                   steps={"work": StepRun("work", plugin="command.shell", status=PENDING)})
    store = checkpoints.Store(str(tmp_path))
    store.begin(run, cycle, registry)
    manifest_path = tmp_path / ".execution/manifest.json"
    launch_snapshot = json.loads(manifest_path.read_text())["execution"]
    assert launch_snapshot == {"initial_mode": "important", "policy_version": 1}
    cycle.execution["default_mode"] = "auto"
    resumed, kept = store.resume(cycle, registry)
    assert kept == []
    assert resumed.initial_execution_mode == "important"
    assert resumed.execution_mode == "approve"
    store.begin(resumed, cycle, registry)
    assert json.loads(manifest_path.read_text())["execution"] == launch_snapshot


def test_legacy_manifest_keeps_its_shape_without_launch_policy_fields(tmp_path):
    cycle = parse()
    run = CycleRun("run-id", "demo", workspace=str(tmp_path))
    checkpoints.Store(str(tmp_path)).begin(run, cycle, registry)
    manifest = json.loads((tmp_path / ".execution/manifest.json").read_text())
    assert "execution" not in manifest


def test_execution_and_run_policy_mappings_do_not_share_mutable_defaults():
    raw = document(execution={"default_mode": "important"})
    cycle = parse(raw)
    raw["execution"]["default_mode"] = "auto"
    assert cycle.execution == {"default_mode": "important"}
    output = model.to_document(cycle)
    output["execution"]["default_mode"] = "approve"
    assert cycle.execution == {"default_mode": "important"}
    one, two = CycleRun("one", "demo"), CycleRun("two", "demo")
    one.node_approvals["id"] = {}
    one.policy_changes.append({})
    assert two.node_approvals == {} and two.policy_changes == []
