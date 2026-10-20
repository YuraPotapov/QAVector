"""Pause and live phase retry preserve deadlines, budgets and resources."""

import json
import threading
from contextlib import contextmanager

import pytest

from cycle import checkpoints, executor, memory, model, registry
from cycle.context import CancelToken
from test_cycle_phases import Events


RUN_ID = "phase-controls"


class Fake(registry.CyclePlugin):
    def __init__(self, plugin_id="test.control", action=None):
        self.metadata = registry.PluginMetadata(id=plugin_id, name=plugin_id)
        self.action = action
        self.calls, self.lifecycle = [], []

    def prepare(self, context, step):
        self.lifecycle.append(("prepare", step.id))

    def execute(self, context, step):
        self.calls.append(step.id)
        self.lifecycle.append(("execute", step.id))
        return self.action(context, step) if self.action else registry.succeeded()

    def cleanup(self, context, step):
        self.lifecycle.append(("cleanup", step.id))

    def stop(self, context, step, handle):
        self.lifecycle.append(("stop", step.id))


class Plugins:
    def __init__(self, *plugins):
        self.entries = {plugin.metadata.id: plugin for plugin in plugins}

    def get(self, name):
        return self.entries.get(name) or registry.get(name)


def definition(steps, phases=None):
    return model.parse_cycle({"id": "controls", "phases": phases or [{"id": "work"}], "steps": steps}, "controls")


def node(name, plugin="test.control", phase="work", **extra):
    return dict(id=name, plugin=plugin, phase=phase, **extra)


@contextmanager
def live(tmp_path, cycle, *plugins, on_event=None):
    events, cancel, result = Events(on_event), CancelToken(), {}

    def run():
        try:
            result["run"] = executor.run_cycle(cycle, str(tmp_path), registry=Plugins(*plugins), observer=events,
                run_id=RUN_ID, cancel=cancel, controlled=True, memory_path=str(tmp_path / "memory.json"))
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield events, result
    finally:
        cancel.set("test cleanup")
        thread.join(7)
        assert not thread.is_alive()
        assert "error" not in result, result


def paused(events, *, after_retry=False):
    return events.wait("cycle.run.state", lambda e: e["action"] == "paused" and
        (not after_retry or any(item["action"] == "retry_phase" for item in e["control_history"])))


def metadata(tmp_path):
    return json.loads((tmp_path / "metadata.json").read_text())


def test_checkpoint_failure_after_exported_pause_reports_failure(tmp_path, monkeypatch):
    fail = threading.Event()
    save = checkpoints.Store.save

    def failing_save(store, run):
        if fail.is_set():
            raise checkpoints.CheckpointError("Cannot save execution state: synthetic final write failure")
        return save(store, run)

    monkeypatch.setattr(checkpoints.Store, "save", failing_save)
    observer = Events(lambda event: fail.set() if event["kind"] == "cycle.run.state" and event["action"] == "paused" else None)
    cycle = definition([node("one"), node("two", phase="later", needs=["one"])],
        [{"id": "work", "policy": "stop_after"}, {"id": "later"}])
    run = executor.run_cycle(cycle, str(tmp_path), registry=Plugins(Fake()), observer=observer)
    assert run.status == run.outcome == "failed" and run.exit_code == 1
    assert not run.paused and not run.pause_requested and run.phase_actions == {}
    assert run.ended_at is not None and run.steps["two"].status == "pending"


def budgeted_cycle(cap):
    return definition([
        node("spent", "memory.recall", **{"with": {"key": "synthetic/task", "field": "attempts", "default": "0"}}),
        node("admit", "check.gate", needs=["spent"], **{"with": {"checks": {"budget": "${steps.spent.outputs.value} < %s" % cap}}}),
        node("budget", "memory.remember", needs=["admit"], **{"with": {"key": "synthetic/task", "bump": ["attempts"]}}),
        node("implement", "agent.implement", needs=["budget"]),
        node("verify", needs=["implement"]),
    ])


def test_retrying_verification_preserves_paid_implementation_and_its_budget(tmp_path, monkeypatch):
    implementation = Fake("agent.implement")
    verification = Fake(action=lambda context, step: registry.failed("temporary verification failure")
                        if len(verification.calls) == 1 else registry.succeeded())
    monkeypatch.setitem(registry._registered, "agent.implement", implementation)
    with live(tmp_path, budgeted_cycle(3), implementation, verification) as (events, result):
        paused(events)
        assert memory.recall("synthetic/task", str(tmp_path / "memory.json"))["attempts"] == 1
        assert not executor.control_run(RUN_ID, "retry_phase", "work")
        events.wait("cycle.run.end")
    run = result["run"]
    assert run.ok and implementation.calls == ["implement"] and verification.calls == ["verify", "verify"]
    assert run.steps["budget"].attempts == run.steps["spent"].attempts == run.steps["admit"].attempts == 1
    assert memory.recall("synthetic/task", str(tmp_path / "memory.json"))["attempts"] == 1


@pytest.mark.parametrize("cap", [1, 2])
def test_retrying_failed_implementation_rechecks_and_charges_the_existing_budget(tmp_path, monkeypatch, cap):
    implementation = Fake("agent.implement", action=lambda context, step: registry.failed("implementation failed")
                          if len(implementation.calls) == 1 else registry.succeeded())
    monkeypatch.setitem(registry._registered, "agent.implement", implementation)
    with live(tmp_path, budgeted_cycle(cap), implementation, Fake()) as (events, result):
        paused(events)
        assert not executor.control_run(RUN_ID, "retry_phase", "work")
        if cap == 1:
            paused(events, after_retry=True)
            saved = metadata(tmp_path)
            assert saved["steps"]["admit"]["status"] == "failed"
            assert saved["steps"]["budget"]["status"] == saved["steps"]["implement"]["status"] == "pending"
            assert implementation.calls == ["implement"]
        else:
            events.wait("cycle.run.end")
    saved = result["run"]
    assert saved.steps["spent"].attempts == saved.steps["admit"].attempts == 2
    assert len(implementation.calls) == cap
    assert memory.recall("synthetic/task", str(tmp_path / "memory.json"))["attempts"] == cap
    if cap == 2:
        assert saved.ok and saved.steps["budget"].attempts == 2


def test_failed_step_holding_a_resource_cannot_be_retried_in_place(tmp_path):
    def work(context, step):
        context.hold(step.id, plugin, "synthetic handle")
        return registry.failed("resource needs review")

    plugin = Fake(action=work)
    with live(tmp_path, definition([node("resource")]), plugin) as (events, result):
        paused(events)
        assert not executor.control_run(RUN_ID, "retry_phase", "work")
        events.wait("cycle.control.refused", lambda e: "holds a managed resource" in e["reason"])
        assert plugin.calls == ["resource"]
        assert plugin.lifecycle == [("prepare", "resource"), ("execute", "resource")]
    assert plugin.lifecycle[-2:] == [("stop", "resource"), ("cleanup", "resource")]


def test_live_retry_matches_cleanup_and_prepare_for_each_execution(tmp_path):
    plugin = Fake(action=lambda context, step: registry.failed("temporary")
                  if len(plugin.calls) == 1 else registry.succeeded())
    with live(tmp_path, definition([node("flaky")]), plugin) as (events, result):
        paused(events)
        executor.control_run(RUN_ID, "retry_phase", "work")
        events.wait("cycle.run.end")
    assert result["run"].ok
    assert plugin.lifecycle == [(action, "flaky") for action in (
        "prepare", "execute", "cleanup", "prepare", "execute", "cleanup")]


def test_paused_parked_plugin_keeps_its_deadline_and_continues_the_same_run(tmp_path, monkeypatch):
    monotonic = executor.time.monotonic
    offset = [0.0]
    monkeypatch.setattr(executor.time, "monotonic", lambda: monotonic() + offset[0])
    plugin = Fake(action=lambda context, step: registry.waiting(0.01, "Waiting for a local result")
                  if len(plugin.calls) == 1 else registry.succeeded())

    def on_event(event):
        if event["kind"] == "cycle.step.waiting":
            assert not executor.control_run(RUN_ID, "pause")

    with live(tmp_path, definition([node("parked", timeout=1)]), plugin, on_event=on_event) as (events, result):
        paused(events)
        before = metadata(tmp_path)
        assert before["steps"]["parked"]["status"] == "waiting"
        offset[0] = 100.0
        # A refused control proves the scheduler processed another turn after
        # the clock jump while the step stayed parked and paused.
        executor.control_run(RUN_ID, "run_phase", "missing")
        events.wait("cycle.control.refused", lambda e: "next phase" in e["reason"])
        during = metadata(tmp_path)
        assert during["steps"]["parked"]["status"] == "waiting" and plugin.calls == ["parked"]
        assert not executor.control_run(RUN_ID, "continue")
        events.wait("cycle.run.end")
    assert result["run"].ok and result["run"].id == RUN_ID and result["run"].resume_count == 0
    assert plugin.calls == ["parked", "parked"]


def test_stop_while_paused_cancels_pending_steps_and_releases_resources(tmp_path):
    def hold(context, step):
        context.hold(step.id, plugin, "synthetic handle")
        return registry.succeeded()

    plugin = Fake(action=hold)
    cycle = definition([node("resource"), node("later", phase="later", needs=["resource"])],
        [{"id": "work", "policy": "stop_after"}, {"id": "later"}])
    with live(tmp_path, cycle, plugin) as (events, result):
        paused(events)
        assert plugin.lifecycle == [("prepare", "resource"), ("execute", "resource")]
        assert executor.request_stop(RUN_ID, "Stopped while paused") == 1
        events.wait("cycle.run.end")
    assert result["run"].status == "cancelled" and not result["run"].paused
    assert result["run"].steps["later"].status == "cancelled" and plugin.calls == ["resource"]
    assert plugin.lifecycle[-2:] == [("stop", "resource"), ("cleanup", "resource")]


def test_automatic_next_phase_sees_the_previous_durable_receipt(tmp_path):
    def work(context, step):
        if step.id == "later":
            receipt = json.loads((tmp_path / ".execution/phases/work.json").read_text())
            assert receipt["status"] == "success" and receipt["steps"] == ["one"]
            assert receipt["evidence"]["one"]["input_digest"]
        return registry.succeeded()

    plugin = Fake(action=work)
    cycle = definition([node("one"), node("later", phase="later", needs=["one"])],
        [{"id": "work"}, {"id": "later"}])
    run = executor.run_cycle(cycle, str(tmp_path), registry=Plugins(plugin))
    assert run.ok and plugin.calls == ["one", "later"]


def test_continue_refuses_when_another_run_now_holds_the_task_claim(tmp_path):
    path, key = str(tmp_path / "memory.json"), "synthetic/task"
    cycle = definition([
        node("claim", "memory.claim", **{"with": {"key": key}}),
        node("later", phase="later", needs=["claim"]),
    ], [{"id": "work", "policy": "stop_after"}, {"id": "later"}])
    plugin = Fake()
    with live(tmp_path, cycle, plugin) as (events, result):
        paused(events)
        assert memory.holder(key, path) == RUN_ID
        assert memory.release(key, RUN_ID, path)
        assert memory.claim(key, "another-run", path=path)[0]
        executor.control_run(RUN_ID, "continue")
        events.wait("cycle.control.refused", lambda e: "now held by another-run" in e["reason"])
        assert plugin.calls == [] and metadata(tmp_path)["steps"]["later"]["status"] == "pending"
    assert memory.holder(key, path) == "another-run"
