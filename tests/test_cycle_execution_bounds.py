"""Explicit execution scope remains bounded through skips and process recovery."""

import json
import shutil

import pytest

from cycle import checkpoints, executor, model, registry, timing
from cycle.context import CancelToken
from test_cycle_phases import Events, Plugin, Plugins


def definition(first=None):
    return model.parse_cycle({"id": "bounded", "phases": [{"id": "prepare"}, {"id": "publish"}],
        "steps": [dict({"id": "prepare", "plugin": "test.node", "phase": "prepare"}, **(first or {})),
                  {"id": "publish", "plugin": "test.node", "phase": "publish"}]}, "bounded")


def run_one_callback(token, on_prepared=None):
    def callback(event):
        if event["kind"] == "cycle.run.state" and event["action"] == "start":
            executor.control_run("bounded", "pause")
        elif event["kind"] == "cycle.run.state" and event["action"] == "paused":
            if event["pause_reason"] == "user_request":
                executor.control_run("bounded", "run_phase", "prepare")
            else:
                token.set("test finished at its boundary")
        if on_prepared:
            on_prepared(event)
    return callback


@pytest.mark.parametrize("first", [{"disabled": True}, {"if": "'yes' == 'no'"}])
def test_run_phase_stops_after_a_phase_with_no_executed_nodes(tmp_path, first):
    token, plugin = CancelToken(), Plugin()
    run = executor.run_cycle(definition(first), str(tmp_path), run_id="bounded", controlled=True,
        cancel=token, registry=Plugins(plugin), observer=Events(run_one_callback(token)))
    assert not plugin.calls
    assert run.phase_receipts["prepare"]["status"] == "skipped"
    assert any(item.get("reason") == "phase_boundary" for item in run.control_history)


@pytest.mark.parametrize("point", ["before_submit", "waiting", "step_receipt", "phase_receipt", "boundary_timing"])
def test_run_phase_intent_survives_crash_before_and_after_its_receipts(tmp_path, point, monkeypatch):
    source, snapshot = tmp_path / "source", tmp_path / "snapshot"
    token = CancelToken()
    if point == "boundary_timing":
        original = checkpoints.Store.timing
        def capture_timing(store, run):
            original(store, run)
            if run.phase_receipts.get("prepare", {}).get("status") == "success" and not run.pause_requested and not snapshot.exists():
                shutil.copytree(source, snapshot)
        monkeypatch.setattr(checkpoints.Store, "timing", capture_timing)
        monkeypatch.setattr(timing, "RECEIPT_INTERVAL", 0)
    def capture(event):
        hit = ((point == "before_submit" and event["kind"] == "cycle.run.state" and event["action"] == "run_phase") or
               (point == "waiting" and event["kind"] == "cycle.step.waiting") or
               (point == "step_receipt" and event["kind"] == "cycle.step.end" and event["step"] == "prepare") or
               (point == "phase_receipt" and event["kind"] == "cycle.phase"))
        if hit and not snapshot.exists():
            shutil.copytree(source, snapshot)
    plugin = Plugin(lambda c, s: registry.waiting(0.001) if point == "waiting" and len(plugin.calls) == 1 else registry.succeeded())
    executor.run_cycle(definition(), str(source), run_id="bounded", controlled=True, cancel=token,
        registry=Plugins(plugin), observer=Events(run_one_callback(token, capture)))
    plugin = Plugin()
    resumed = executor.run_cycle(definition(), str(snapshot), resume=True, registry=Plugins(plugin))
    assert "publish" not in plugin.calls
    assert resumed.paused and resumed.pause_reason == "phase_boundary"
    assert resumed.steps["publish"].status == "pending"
    assert resumed.execution_bound == {"phase": "prepare", "action": "run_phase"}
    assert any(item.get("phase") == "prepare" for item in resumed.control_history if item["action"] == "run_phase")
    final = executor.run_cycle(definition(), str(snapshot), resume=True, registry=Plugins(plugin))
    assert final.ok and plugin.calls[-1] == "publish" and final.execution_bound == {}


@pytest.mark.parametrize("gate", [{"disabled": True}, {"if": "'yes' == 'no'"}])
@pytest.mark.parametrize("write_condition", ["", "'yes' == 'no'", "'yes' == 'yes'"])
def test_skipped_approval_phase_propagates_skips_without_executing_effects(tmp_path, gate, write_condition):
    cycle = model.parse_cycle({"id": "skip", "phases": [
        {"id": "publish", "policy": "approval_required", "approval": "gate"}, {"id": "after"}], "steps": [
        dict({"id": "gate", "plugin": "approval.gate", "phase": "publish", "with": {"question": "Publish?"}}, **gate),
        {"id": "write", "plugin": "test.node", "phase": "publish", "needs": ["gate"], "if": write_condition},
        {"id": "after", "plugin": "test.node", "phase": "after"}]}, "skip")
    assert not model.problems(cycle)
    plugin = Plugin()
    run = executor.run_cycle(cycle, str(tmp_path), registry=Plugins(plugin))
    assert run.ok and plugin.calls == ["after"]
    assert run.steps["gate"].status == run.steps["write"].status == "skipped"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("stop", [False, True])
@pytest.mark.parametrize("selection", [["selected"], ["selected", "publish"]])
def test_partial_resume_preserves_selection_and_does_not_start_parallel_or_future_work(tmp_path, legacy, stop, selection):
    token = CancelToken()
    plugin = Plugin(lambda c, s: (token.set("stopped") or registry.failed("stopped")) if stop else registry.failed("temporary"))
    cycle = model.parse_cycle({"id": "partial", "steps": [
        {"id": "selected", "plugin": "test.node"},
        {"id": "parallel", "plugin": "test.node"},
        {"id": "publish", "plugin": "test.node", "needs": ["selected"]}]}, "partial")
    first = executor.run_cycle(cycle, str(tmp_path), only=selection, cancel=token, registry=Plugins(plugin))
    assert first.selected_steps == selection
    if legacy:
        path = tmp_path / "metadata.json"
        saved = json.loads(path.read_text())
        saved.pop("selected_steps")
        saved.pop("scope")
        path.write_text(json.dumps(saved))
    plugin = Plugin()
    resumed = executor.run_cycle(cycle, str(tmp_path), resume=True, registry=Plugins(plugin))
    assert resumed.ok and plugin.calls == selection
    assert resumed.selected_steps == selection
    assert resumed.steps["parallel"].reason_code == "not_selected"
    if "publish" not in selection:
        assert resumed.steps["publish"].reason_code == "not_selected"


def test_run_phase_stops_after_an_empty_query(tmp_path):
    from test_cycle_phase_controls import Fake, Plugins as Registry
    token = CancelToken()
    query = Fake("jira.issues", action=lambda c, s: registry.succeeded(count=0))
    later = Fake("test.node")
    run = executor.run_cycle(definition({"plugin": "jira.issues"}), str(tmp_path), run_id="bounded",
        controlled=True, cancel=token, registry=Registry(query, later), observer=Events(run_one_callback(token)))
    assert query.calls == ["prepare"] and not later.calls
    assert run.phase_receipts["prepare"]["status"] == "skipped"
    assert any(item.get("reason") == "phase_boundary" for item in run.control_history)
