"""Execution barriers, durable phase receipts and explicit live controls."""

import copy
import json
import threading
from contextlib import contextmanager

import pytest

from cycle import checkpoints, executor, model, registry
from cycle.bus import EventObserver
from cycle.context import CancelToken
from domain.cycle import Artifact


class Plugin(registry.CyclePlugin):
    metadata = registry.PluginMetadata(id="test.node", name="Test node")

    def __init__(self, action=None):
        self.action, self.calls = action, []

    def execute(self, context, step):
        self.calls.append(step.id)
        return self.action(context, step) if self.action else registry.succeeded()


class Plugins:
    def __init__(self, plugin):
        self.plugin = plugin

    def get(self, name):
        return self.plugin if name == "test.node" else registry.get(name)


def cycle(steps=None, phases=None):
    return model.parse_cycle({"id": "demo", "phases": phases or [
        {"id": "prepare", "title": "Prepare task", "policy": "stop_after", "outcome": "task_prepared"},
        {"id": "build", "title": "Build"}], "steps": steps or [
        {"id": "one", "plugin": "test.node", "phase": "prepare"},
        {"id": "two", "plugin": "test.node", "phase": "build", "needs": ["one"]}]}, "demo")


class Events(EventObserver):
    def __init__(self, on_event=None):
        self.events, self.condition = [], threading.Condition()
        self.on_event = on_event
        super().__init__(sink=self.emit)

    def emit(self, kind, **fields):
        event = dict(fields, kind=kind)
        with self.condition:
            self.events.append(copy.deepcopy(event))
            self.condition.notify_all()
        if self.on_event:
            self.on_event(event)

    def wait(self, kind, predicate=lambda event: True):
        with self.condition:
            assert self.condition.wait_for(lambda: any(event["kind"] == kind and predicate(event) for event in self.events), timeout=5), self.events
            return next(event for event in self.events if event["kind"] == kind and predicate(event))


@contextmanager
def live(tmp_path, definition, plugin=None, on_event=None, jobs=4):
    plugin, observer, token, result = plugin or Plugin(), Events(on_event), CancelToken(), {}
    def run():
        try:
            result["run"] = executor.run_cycle(definition, str(tmp_path), registry=Plugins(plugin), observer=observer,
                                               cancel=token, run_id="live", controlled=True, jobs=jobs)
        except Exception as exc:
            result["error"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield observer, plugin, result
    finally:
        token.set("test cleanup")
        thread.join(7)
        assert not thread.is_alive()
        assert "error" not in result, result


def test_phase_roundtrip_and_graph_keep_machine_identity():
    original = cycle()
    saved = model.to_document(original)
    assert model.to_document(model.parse_cycle(saved, "demo")) == saved
    assert model.to_graph(original)["nodes"][0]["phase"] == "prepare"
    assert model.to_graph(original)["nodes"][0]["group"] == "Prepare task"


@pytest.mark.parametrize("edit,phrase", [
    (lambda raw: raw["phases"][0].update(policy="unknown"), "policy"),
    (lambda raw: raw["phases"].append({"id": "prepare"}), "duplicate"),
    (lambda raw: raw["steps"][0].update(phase="missing"), "unknown phase"),
    (lambda raw: raw["steps"][0].pop("phase"), "group is visual"),
    (lambda raw: raw["steps"][0].update(needs=["two"]), "later phase"),
    (lambda raw: raw["phases"][0].update(deliverables=["two"]), "deliverable"),
    (lambda raw: raw["phases"][0].update(policy="approval_required", approval="one"), "approval.gate"),
])
def test_invalid_phase_contract_is_rejected_before_execution(edit, phrase):
    raw = model.to_document(cycle())
    edit(raw)
    assert any(phrase in issue for issue in model.problems(model.parse_cycle(raw, "demo")))


@pytest.mark.parametrize("phases", [{}, [7], [{"id": "a", "typo": True}], [{"id": "a", "effects": "git.write"}]])
def test_bad_phase_shapes_are_rejected(phases):
    with pytest.raises(model.CycleError):
        model.parse_cycle({"steps": [], "phases": phases}, "demo")


def test_approval_phase_cannot_run_an_operation_around_its_gate():
    c = cycle(phases=[{"id": "prepare", "policy": "approval_required", "approval": "gate"}], steps=[
        {"id": "gate", "plugin": "approval.gate", "phase": "prepare"},
        {"id": "write", "plugin": "test.node", "phase": "prepare"}])
    assert any("depend on its approval" in issue for issue in model.problems(c))
    c.step("write").needs = ("gate",)
    assert not [issue for issue in model.problems(c) if issue.startswith("phases.")]


def test_rejected_phase_approval_blocks_conditional_effects_until_retry(tmp_path, monkeypatch):
    answers = iter(("Reject", "Approve"))
    monkeypatch.setattr("cycle.ask.ask", lambda *args, **kwargs: {
        "answered": True, "answer": next(answers), "who": "test"})
    c = cycle(phases=[{"id": "publish", "policy": "approval_required", "approval": "gate"}], steps=[
        {"id": "gate", "plugin": "approval.gate", "phase": "publish", "on_failure": "continue",
         "with": {"question": "Publish the reviewed change?"}},
        {"id": "write", "plugin": "test.node", "phase": "publish", "needs": ["gate"],
         "if": "${steps.gate.status} != 'waiting'"}])
    assert not model.problems(c)
    with live(tmp_path, c) as (events, plugin, result):
        state = events.wait("cycle.run.state", lambda e: e["action"] == "paused")
        assert state["phase_actions"]["retry"] == "publish"
        assert not plugin.calls
        saved = json.loads((tmp_path / "metadata.json").read_text())
        assert saved["steps"]["write"]["status"] == "pending"
        executor.control_run("live", "continue")
        events.wait("cycle.control.refused", lambda e: "Retry phase" in e["reason"])
        executor.control_run("live", "retry_phase", "publish")
        events.wait("cycle.run.end")
        assert plugin.calls == ["write"]
        assert json.loads((tmp_path / "metadata.json").read_text())["steps"]["gate"]["attempts"] == 2


def test_stop_after_writes_receipt_and_keeps_pending_work(tmp_path):
    plugin = Plugin()
    run = executor.run_cycle(cycle(), str(tmp_path), registry=Plugins(plugin))
    assert run.status == "waiting" and run.execution_status == "paused" and run.exit_code == 0
    assert run.outcome == "task_prepared"
    assert run.steps["two"].status == "pending" and plugin.calls == ["one"]
    receipt = json.loads((tmp_path / ".execution/phases/prepare.json").read_text())
    assert receipt["status"] == "success" and receipt["steps"] == ["one"]
    assert receipt["evidence"]["one"]["input_digest"]
    assert checkpoints.Store(str(tmp_path)).load().phase_receipts["prepare"] == receipt


def test_resume_after_exported_pause_keeps_success_and_crosses_consumed_boundary(tmp_path):
    plugin, c = Plugin(), cycle()
    first = executor.run_cycle(c, str(tmp_path), registry=Plugins(plugin))
    final = executor.run_cycle(c, str(tmp_path), registry=Plugins(plugin), resume=True)
    assert final.ok and final.id == first.id and plugin.calls == ["one", "two"]
    assert final.resume_count == 1 and final.control_history[-1]["action"] == "resume"


def test_pause_requested_before_submission_starts_no_nodes(tmp_path):
    def on_event(event):
        if event["kind"] == "cycle.run.state" and event["action"] == "start":
            assert not executor.control_run("live", "pause")
    with live(tmp_path, cycle(), on_event=on_event) as (events, plugin, result):
        events.wait("cycle.run.state", lambda e: e["action"] == "paused")
        assert plugin.calls == []
        assert all(s["status"] == "pending" for s in json.loads((tmp_path / "metadata.json").read_text())["steps"].values())


def test_live_continue_uses_same_run_without_repeating_preparation(tmp_path):
    with live(tmp_path, cycle()) as (events, plugin, result):
        paused = events.wait("cycle.run.state", lambda e: e["action"] == "paused")
        assert paused["phase_actions"]["continue"]
        assert not executor.control_run("live", "continue")
        events.wait("cycle.run.end")
        assert plugin.calls == ["one", "two"]
        assert json.loads((tmp_path / "metadata.json").read_text())["resume_count"] == 0


def test_manual_phase_needs_run_phase_and_cannot_be_skipped(tmp_path):
    c = cycle(phases=[{"id": "prepare", "policy": "manual"}, {"id": "build"}])
    with live(tmp_path, c) as (events, plugin, result):
        events.wait("cycle.run.state", lambda e: e["action"] == "paused")
        executor.control_run("live", "continue")
        events.wait("cycle.control.refused", lambda e: "explicit Run phase" in e["reason"])
        executor.control_run("live", "run_phase", "build")
        events.wait("cycle.control.refused", lambda e: "next phase" in e["reason"])
        assert not plugin.calls
        executor.control_run("live", "run_phase", "prepare")
        events.wait("cycle.run.state", lambda e: e["action"] == "paused" and e["pause_reason"] == "phase_boundary")
        assert plugin.calls == ["one"]
        executor.control_run("live", "continue")
        events.wait("cycle.run.end")


def test_parallel_pause_drains_only_active_slots_and_never_cancels_pending(tmp_path):
    started, release = threading.Barrier(3), threading.Event()
    def work(context, step):
        if step.id in ("a", "b"):
            started.wait(timeout=5)
            assert release.wait(5)
        return registry.succeeded()
    c = model.parse_cycle({"steps": [{"id": name, "plugin": "test.node", "group": "Visual"} for name in "abcd"]}, "demo")
    with live(tmp_path, c, Plugin(work), jobs=2) as (events, plugin, result):
        started.wait(timeout=5)
        executor.control_run("live", "pause")
        release.set()
        events.wait("cycle.run.state", lambda e: e["action"] == "paused")
        saved = json.loads((tmp_path / "metadata.json").read_text())
        assert sorted(plugin.calls) == ["a", "b"]
        assert saved["steps"]["c"]["status"] == saved["steps"]["d"]["status"] == "pending"
        executor.control_run("live", "continue")
        events.wait("cycle.run.end")
        assert sorted(plugin.calls) == list("abcd")


def test_failed_phase_retry_preserves_successful_paid_nodes(tmp_path):
    attempts = []
    def work(context, step):
        if step.id == "flaky":
            attempts.append(step.id)
            return registry.failed("temporary failure") if len(attempts) == 1 else registry.succeeded()
        return registry.succeeded()
    c = cycle(steps=[{"id": name, "plugin": "test.node", "phase": phase} for name, phase in
                     (("paid", "prepare"), ("flaky", "prepare"), ("later", "build"))])
    with live(tmp_path, c, Plugin(work)) as (events, plugin, result):
        state = events.wait("cycle.run.state", lambda e: e["action"] == "paused")
        assert state["phase_actions"]["retry"] == "prepare"
        executor.control_run("live", "retry_phase", "prepare")
        events.wait("cycle.run.state", lambda e: e["action"] == "paused" and e["pause_reason"] == "phase_boundary")
        assert plugin.calls.count("paid") == 1 and plugin.calls.count("flaky") == 2 and "later" not in plugin.calls
        executor.control_run("live", "continue")
        events.wait("cycle.run.end")
        assert len(list((tmp_path / ".execution/phases/history/prepare").glob("*.json"))) == 1


def test_phase_contract_changes_cannot_silently_change_a_saved_run(tmp_path):
    c, plugins = cycle(), Plugins(Plugin())
    executor.run_cycle(c, str(tmp_path), registry=plugins)
    c.phases[0].policy = "automatic"
    with pytest.raises(checkpoints.CheckpointError, match="phase contract changed"):
        executor.run_cycle(c, str(tmp_path), registry=plugins, resume=True)


def test_resume_of_failed_phase_still_honours_its_first_successful_boundary(tmp_path):
    def work(context, step):
        if step.id == "one" and plugin.calls.count("one") == 1:
            return registry.failed("temporary failure")
        return registry.succeeded()
    plugin, c = Plugin(work), cycle()
    first = executor.run_cycle(c, str(tmp_path), registry=Plugins(plugin))
    assert first.paused and first.pause_reason == "phase_failed"
    resumed = executor.run_cycle(c, str(tmp_path), registry=Plugins(plugin), resume=True)
    assert resumed.paused and resumed.pause_reason == "phase_boundary"
    assert resumed.phase_receipts["prepare"]["status"] == "success"
    assert plugin.calls == ["one", "one"] and resumed.steps["two"].status == "pending"


def test_phase_receipt_survives_a_missing_metadata_update(tmp_path):
    c = cycle()
    executor.run_cycle(c, str(tmp_path), registry=Plugins(Plugin()))
    saved = json.loads((tmp_path / "metadata.json").read_text())
    saved["phase_receipts"] = {}
    (tmp_path / "metadata.json").write_text(json.dumps(saved))
    assert checkpoints.Store(str(tmp_path)).load().phase_receipts["prepare"]["status"] == "success"
