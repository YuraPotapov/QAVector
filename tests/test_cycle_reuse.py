"""The development cycle must not remember completion without a check.

Run its actual gate and memory step with deterministic review outcomes. No
Jira requests, paid agents or writes to a user's task memory are involved.
"""

from pathlib import Path

import pytest

from cycle import executor, loader, memory, registry
from cycle.registry import CyclePlugin, PluginMetadata, PluginResult
from domain.cycle import Cycle, CycleStep, FAILED, SKIPPED, SUCCESS, TIMEOUT


class Review(CyclePlugin):
    metadata = PluginMetadata(id="test.review", name="Review")

    def __init__(self, outcome):
        self.outcome = outcome

    def execute(self, context, step):
        if step.id == "todo":
            return registry.succeeded(key="QA-7")
        return self.outcome


class Plugins:
    def __init__(self, outcome):
        self.review = Review(outcome)

    def get(self, plugin_id):
        return self.review if plugin_id == "test.review" else registry.get(plugin_id)


@pytest.mark.parametrize("cycle_id", ["development", "development_in_progress"])
@pytest.mark.parametrize("scenario,expected_gate,expected_record", [
    ("skipped_review", SKIPPED, SKIPPED),
    ("failed_review", SKIPPED, SKIPPED),
    ("timed_out_review", SKIPPED, SKIPPED),
    ("work_left", SUCCESS, SKIPPED),
    ("already_done", FAILED, SUCCESS),
    ("broken_review", FAILED, FAILED),
])
def test_completion_is_only_remembered_after_an_actual_verdict(
        tmp_path, cycle_id, scenario, expected_gate, expected_record):
    templates = Path(__file__).resolve().parents[1] / "cycles"
    template, _ = loader.read_cycle(cycle_id, str(templates))
    outcomes = {
        "skipped_review": PluginResult(SKIPPED, message="budget refused"),
        "failed_review": PluginResult(FAILED, message="session limit"),
        "timed_out_review": PluginResult(TIMEOUT, message="review timed out"),
        "work_left": registry.succeeded(issue_count=1, summary="Work remains."),
        "already_done": registry.succeeded(issue_count=0, summary="Found in code."),
        "broken_review": registry.succeeded(summary="No issue count was returned."),
    }
    cycle = Cycle(id=cycle_id, steps=[
        CycleStep(id="todo", plugin="test.review"),
        CycleStep(id="reconcile", plugin="test.review", needs=["todo"],
                  on_failure="continue"),
        template.step("work_remains"),
        template.step("record_reuse"),
    ])
    store = str(tmp_path / "memory.json")
    run = executor.run_cycle(
        cycle, str(tmp_path / "run"), registry=Plugins(outcomes[scenario]),
        memory_path=store)

    assert run.steps["work_remains"].status == expected_gate
    record = run.steps["record_reuse"]
    assert record.status == expected_record, record.message
    if scenario == "already_done":
        assert memory.recall("dev/QA-7", store) == {
            "state": "committed", "found": "Found in code."}
    else:
        assert memory.recall("dev/QA-7", store) == {}
    if expected_gate == SKIPPED:
        assert "work_remains was skipped" in record.message
