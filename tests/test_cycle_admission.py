"""Task attempts are reserved for new work, never for replaying paid answers."""

import pytest
import threading
from concurrent.futures import ThreadPoolExecutor

from cycle import admission, checkpoints, executor, memory, registry
from cycle.operations import Operation
from test_cycle_phase_controls import Fake, Plugins, budgeted_cycle


def execute(tmp_path, plugin, cap=1, resume=False):
    return executor.run_cycle(budgeted_cycle(cap), str(tmp_path / "run"),
        registry=Plugins(plugin, Fake()), memory_path=str(tmp_path / "memory.json"), resume=resume)


def attempts(tmp_path):
    return memory.recall("synthetic/task", str(tmp_path / "memory.json"))["attempts"]


def test_repeated_resume_cannot_bypass_task_cap(tmp_path):
    plugin = Fake("agent.implement", action=lambda c, s: registry.failed("implementation failed"))
    execute(tmp_path, plugin)
    for _ in range(3):
        try:
            resumed = execute(tmp_path, plugin, resume=True)
        except checkpoints.CheckpointError as exc:
            assert "no failed or interrupted work" in str(exc)
        else:
            assert not resumed.ok
        assert attempts(tmp_path) == 1
    assert plugin.calls == ["implement"]


def test_resume_rechecks_and_charges_before_a_fresh_implementation(tmp_path):
    plugin = Fake("agent.implement", action=lambda c, s: registry.failed("before provider")
                  if len(plugin.calls) == 1 else registry.succeeded())
    execute(tmp_path, plugin, cap=2)
    final = execute(tmp_path, plugin, cap=2, resume=True)
    assert final.ok and plugin.calls == ["implement", "implement"] and attempts(tmp_path) == 2
    assert final.steps["spent"].attempts == final.steps["admit"].attempts == final.steps["budget"].attempts == 2


def test_paid_response_replay_does_not_spend_another_task_attempt(tmp_path):
    provider_calls = []
    def work(context, step):
        operation = Operation(context, step, "provider", {"prompt": "same"})
        answer = operation.cached()
        if answer is None:
            operation.begin()
            provider_calls.append(1)
            answer = {"answer": "paid"}
            operation.complete(answer, True)
            raise OSError("local report failed")
        return registry.succeeded(**answer)
    plugin = Fake("agent.implement", action=work)
    execute(tmp_path, plugin)
    final = execute(tmp_path, plugin, resume=True)
    assert final.ok and provider_calls == [1] and attempts(tmp_path) == 1
    assert final.steps["budget"].attempts == final.steps["admit"].attempts == 1


@pytest.mark.parametrize("cap", [1, 2])
def test_a_new_call_after_replaying_a_paid_response_requires_admission(tmp_path, cap):
    provider_calls = []
    def work(context, step):
        first = Operation(context, step, "provider", {"prompt": "first"})
        cached = first.cached()
        if cached is None:
            first.begin()
            provider_calls.append("first")
            first.complete({"answer": "first"}, True)
            raise OSError("failed before next call")
        second = Operation(context, step, "provider", {"prompt": "second"})
        second.begin()
        provider_calls.append("second")
        second.complete({"answer": "second"}, True)
        return registry.succeeded()
    plugin = Fake("agent.implement", action=work)
    execute(tmp_path, plugin, cap)
    final = execute(tmp_path, plugin, cap, resume=True)
    assert attempts(tmp_path) == cap
    assert provider_calls == (["first"] if cap == 1 else ["first", "second"])
    assert final.ok is (cap == 2)
    if cap == 1:
        assert "admission refused" in final.steps["implement"].message


def test_crash_after_initial_budget_reservation_reuses_it(tmp_path, monkeypatch):
    reserve = memory.remember_once
    failed = []
    def crash(*args, **kwargs):
        answer = reserve(*args, **kwargs)
        if not failed:
            failed.append(True)
            raise OSError("crashed after durable reservation")
        return answer
    monkeypatch.setattr(memory, "remember_once", crash)
    plugin = Fake("agent.implement")
    initial = execute(tmp_path, plugin)
    assert not initial.ok and not plugin.calls and attempts(tmp_path) == 1
    final = execute(tmp_path, plugin, resume=True)
    assert final.ok and plugin.calls == ["implement"] and attempts(tmp_path) == 1


def test_crash_after_deferred_reservation_does_not_reserve_twice(tmp_path, monkeypatch):
    provider_calls = []
    def work(context, step):
        first = Operation(context, step, "provider", {"prompt": "first"})
        if first.cached() is None:
            first.begin()
            provider_calls.append("first")
            first.complete({"answer": "first"}, True)
            raise OSError("local failure")
        second = Operation(context, step, "provider", {"prompt": "second"})
        second.begin()
        provider_calls.append("second")
        second.complete({"answer": "second"}, True)
        return registry.succeeded()
    plugin = Fake("agent.implement", action=work)
    execute(tmp_path, plugin, cap=2)
    original, crashed = admission.Guard._reserve, []
    def reserve(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if not crashed:
            crashed.append(True)
            raise checkpoints.CheckpointError("Cannot save execution state: synthetic crash after reservation")
    monkeypatch.setattr(admission.Guard, "_reserve", reserve)
    failed = execute(tmp_path, plugin, cap=2, resume=True)
    assert not failed.ok and provider_calls == ["first"] and attempts(tmp_path) == 2
    final = execute(tmp_path, plugin, cap=2, resume=True)
    assert final.ok and provider_calls == ["first", "second"] and attempts(tmp_path) == 2


def test_concurrent_admission_checks_and_reservations_cannot_overrun_a_task_cap(tmp_path, monkeypatch):
    from cycle.plugins.memory import MemoryRemember
    ready = threading.Barrier(2)
    original = MemoryRemember.execute
    def reserve(self, context, step):
        # Both independent runs have read zero and passed the ordinary gate.
        # The reservation must still recheck under the shared memory lock.
        if step.id == "budget":
            ready.wait(timeout=5)
        return original(self, context, step)
    monkeypatch.setattr(MemoryRemember, "execute", reserve)
    plugin = Fake("agent.implement")
    def run(name):
        return executor.run_cycle(budgeted_cycle(1), str(tmp_path / name), registry=Plugins(plugin, Fake()),
            memory_path=str(tmp_path / "memory.json"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("one", "two")))
    assert sum(result.ok for result in results) == 1
    assert plugin.calls == ["implement"] and attempts(tmp_path) == 1


def test_legacy_inflight_counter_without_idempotence_evidence_stays_uncertain(tmp_path):
    plugin = Fake("agent.implement", action=lambda c, s: registry.failed("temporary"))
    initial = execute(tmp_path, plugin)
    budget = initial.steps["budget"]
    budget.status, budget.admission_reservation = "running", ""
    checkpoints.Store(initial.workspace).step(initial, budget)
    with pytest.raises(checkpoints.CheckpointError, match="Uncertain outcome for budget"):
        execute(tmp_path, plugin, resume=True)
    assert attempts(tmp_path) == 1 and plugin.calls == ["implement"]
