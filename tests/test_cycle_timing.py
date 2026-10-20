"""Deterministic timer receipts: gaps never become offline execution time."""

import json

import pytest

from cycle import timing
from cycle.bus import EventObserver
from cycle.context import RunContext
from cycle.executor import run_cycle
from cycle.model import parse_cycle
from cycle.run import from_document, to_document
from domain.cycle import CycleRun, StepRun


class Time:
    mono = 10.0
    wall = 1000.0

    def advance(self, seconds, wall=None):
        self.mono += seconds
        self.wall += seconds if wall is None else wall


def setup(supervised=False):
    now = Time()
    run = CycleRun("r", "demo", steps={"a": StepRun("a", status="running")})
    confirmations = timing.Confirmations(lambda: now.mono, lambda: now.wall) if supervised else None
    if confirmations:
        confirmations.confirm()
    clock = timing.Clock(run, confirmations, lambda: now.mono, lambda: now.wall)
    clock.start(run.steps["a"])
    return now, run, clock, confirmations


def test_running_and_plugin_waiting_accumulate_but_stopped_nodes_do_not():
    now, run, clock, _ = setup()
    now.advance(2)
    clock.tick()
    run.steps["a"].status = "waiting"
    now.advance(3)
    clock.tick()
    assert run.steps["a"].active_elapsed_ms == 5000
    clock.finish(run.steps["a"])
    run.steps["a"].status = "cancelled"
    now.advance(4)
    clock.close()
    assert run.steps["a"].active_elapsed_ms == 5000
    assert run.active_elapsed_ms == 9000
    assert run.steps["a"].execution_segments[-1]["ended_at"] == 1005


@pytest.mark.parametrize("wall", [3602, -3600, 2])
def test_sleep_clock_correction_or_long_scheduler_gap_is_excluded(wall):
    now, run, clock, _ = setup()
    now.advance(2)
    clock.tick()
    now.advance(3602 if wall == 2 else 2, wall=wall)
    clock.tick()
    assert run.steps["a"].active_elapsed_ms == 2000
    assert run.steps["a"].execution_segments[0]["ended_at"] == 1002
    now.advance(1)
    clock.close()
    assert run.steps["a"].active_elapsed_ms == 3000
    assert len(run.steps["a"].execution_segments) == 2


def test_missing_gui_heartbeat_freezes_and_reconnected_heartbeat_does_not_bridge_gap():
    now, run, clock, confirmations = setup(True)
    now.advance(1)
    confirmations.confirm()
    clock.tick()
    now.advance(60)
    confirmations.confirm()  # arrives before the scheduler wakes
    clock.tick()
    assert run.active_elapsed_ms == 1000
    assert run.execution_segments[0]["ended_at"] == 1001
    now.advance(1)
    clock.tick()
    assert run.steps["a"].active_elapsed_ms == 2000
    now.advance(6)
    clock.tick()
    before = run.active_elapsed_ms
    now.advance(1)
    clock.tick()
    assert run.active_elapsed_ms == before


def test_resume_uses_confirmed_elapsed_and_seals_crashed_segment():
    now, run, clock, _ = setup()
    now.advance(2)
    clock.tick()
    restored = from_document(json.loads(json.dumps(to_document(run))))
    now.advance(86400)
    second = timing.Clock(restored, monotonic=lambda: now.mono, wall=lambda: now.wall)
    now.advance(3)
    second.close()
    assert restored.steps["a"].active_elapsed_ms == 5000
    assert restored.steps["a"].execution_segments[0]["ended_at"] == 1002


def test_legacy_records_have_unknown_supervised_time():
    old = from_document({"id": "r", "cycle_id": "c", "steps": {
        "a": {"step_id": "a", "status": "success", "duration_ms": 9000}}})
    assert old.steps["a"].active_elapsed_ms is None


def test_paused_run_does_not_accumulate_time_and_continuing_preserves_receipts():
    now, run, clock, _ = setup()
    now.advance(1)
    clock.tick()
    run.status = "paused"
    for _ in range(10):
        now.advance(1)
        clock.tick()
    assert run.active_elapsed_ms == run.steps["a"].active_elapsed_ms == 1000
    run.status = "running"
    clock.tick()
    now.advance(2)
    clock.tick()
    assert run.active_elapsed_ms == run.steps["a"].active_elapsed_ms == 3000


def test_periodic_timing_updates_the_step_receipt_used_after_a_crash(tmp_path):
    from cycle.checkpoints import Store
    store = Store(str(tmp_path))
    now, run, clock, _ = setup()
    run.workspace = str(tmp_path)
    store.step(run, run.steps["a"])
    now.advance(3)
    clock.tick()
    store.timing(run)
    restored = store.load()
    assert restored.steps["a"].active_elapsed_ms == 3000
    assert restored.steps["a"].execution_segments[-1]["confirmed_at"] == 1003


def test_progress_is_diagnostic_and_worker_activity_is_drained_by_owner():
    events = []
    observer = EventObserver("r", sink=lambda kind, **values: events.append(dict(kind=kind, **values)))
    context = RunContext(None, None, "", observer=observer)
    context.activity = timing.Activity()
    context.progress("a", title="Module <one>", current=12, total=74, unit="modules")
    event = events[-1]
    assert event["kind"] == "cycle.step.progress"
    assert event["current"] == 12 and event["total"] == 74
    assert "status" not in event
    assert context.activity.drain()["a"]["progress"]["title"] == "Module <one>"


def test_executor_persists_timer_receipts_and_progress(tmp_path):
    cycle = parse_cycle({"id": "timer", "steps": [{"id": "one", "plugin": "command.shell",
                          "with": {"command": "echo timer"}, "timeout": 30}]}, "timer")
    found = []
    observer = EventObserver(sink=lambda kind, **fields: found.append(dict(kind=kind, **fields)))
    result = run_cycle(cycle, str(tmp_path), observer=observer)
    assert result.ok
    assert result.steps["one"].active_elapsed_ms >= 0
    saved = json.loads((tmp_path / "metadata.json").read_text())
    assert saved["steps"]["one"]["execution_segments"]
    assert all(segment["ended_at"] is not None for segment in saved["execution_segments"])
    assert any(event["kind"] == "cycle.step.progress" for event in found)
    assert found[-2]["kind"] == "cycle.run.timing"


def test_gui_confirmation_and_eof_are_received_on_the_control_channel(monkeypatch):
    import io
    import session_launcher as launcher
    from cycle import executor
    confirmed, stopped, closed = [], [], []
    monkeypatch.setattr(executor, "confirm_supervision", confirmed.append)
    monkeypatch.setattr(executor, "request_stop", lambda **fields: stopped.append(fields))
    monkeypatch.setattr(launcher.sys, "stdin", io.StringIO('{"command":"cycle.supervision","run_id":"r"}\n'))
    launcher.read_commands({}, on_closed=lambda: closed.append(True))
    assert confirmed == ["r"]
    assert stopped == [{"reason": "the control channel closed"}] and closed == [True]


@pytest.mark.parametrize("previous,changed,elapsed,generation", [("cancelled", False, 5000, 1), ("failed", False, 5000, 1),
                                                               ("failed", True, None, 2)])
def test_resume_continues_same_execution_and_archives_changed_definition(tmp_path, previous, changed, elapsed, generation):
    from cycle import checkpoints, registry
    cycle = parse_cycle({"id": "timer", "steps": [{"id": "ask", "plugin": "approval.gate",
                          "with": {"question": "Proceed?"}}]}, "timer")
    first = run_cycle(cycle, str(tmp_path))
    step = first.steps["ask"]
    step.status = previous
    step.active_elapsed_ms = 5000
    step.execution_segments = [{"generation": 1, "started_at": 10, "confirmed_at": 15,
                                "ended_at": 15, "elapsed_ms": 5000}]
    checkpoints.Store(str(tmp_path)).step(first, step)
    if changed:
        cycle.step("ask").settings["question"] = "A different decision?"
    restored, _ = checkpoints.Store(str(tmp_path)).resume(cycle, registry)
    assert restored.steps["ask"].active_elapsed_ms == elapsed
    assert restored.steps["ask"].execution_generation == generation
    assert restored.steps["ask"].execution_segments == step.execution_segments


def test_a_failed_periodic_receipt_does_not_fail_the_run(tmp_path, monkeypatch):
    from cycle import checkpoints
    monkeypatch.setattr(timing, "RECEIPT_INTERVAL", 0.0)
    failures = []

    def refuse(self, run):
        failures.append(run.id)
        raise checkpoints.CheckpointError("Cannot save execution state: file is locked")

    monkeypatch.setattr(checkpoints.Store, "timing", refuse)
    cycle = parse_cycle({"id": "timer", "steps": [{"id": "one", "plugin": "command.shell",
                          "with": {"command": "sleep 0.3"}, "timeout": 30}]}, "timer")
    result = run_cycle(cycle, str(tmp_path))
    assert failures
    assert result.ok
    saved = json.loads((tmp_path / "metadata.json").read_text())
    assert saved["steps"]["one"]["status"] == "success"
