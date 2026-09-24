"""The three steps over the store.

Most of what matters is in `test_cycle_memory.py`; what is here is the part a
cycle file touches, and two of these tests exist because writing the first
cycle that used them went wrong in exactly these ways.

The first: a plugin's outputs are the keyword arguments to ``succeeded``, not
a dict handed to it, and getting that wrong nests everything one level deeper
so every `${steps.x.outputs.y}` in the cycle fails to resolve.

The second, and the more interesting: reaching into `fields` for a name that is
not there yet **fails the step** rather than reading empty - and on a first run
no name is there. A guard written that way works from the second run onward and
breaks on the one that matters. Hence `value`.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import bus, conditions, executor, memory, registry, variables  # noqa: E402
from cycle.context import CancelToken, RunContext                 # noqa: E402
from cycle.model import parse_cycle                               # noqa: E402
from cycle.workspace import create                                # noqa: E402
from domain.cycle import CycleRun, CycleStep                      # noqa: E402


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "memory.json")


@pytest.fixture
def run(tmp_path, store):
    """Runs one memory step and hands back its result and the context."""
    def go(plugin_id, run_id="run-a", **settings):
        workspace = create("20260919-000000-m", str(tmp_path / "runs"))
        context = RunContext(CycleRun(id=run_id, cycle_id="c",
                                      workspace=workspace),
                             None, workspace, cancel=CancelToken(),
                             memory_path=store)
        step = CycleStep(id="m", plugin=plugin_id, settings=dict(settings))
        return registry.get(plugin_id).execute(context, step), context, step
    return go


# ------------------------------------------------------------------- recall
def test_a_fresh_key_reads_as_nothing_known(run):
    result, _ctx, _step = run("memory.recall", key="QA-1")
    assert result.ok
    assert result.outputs["known"] is False
    assert result.outputs["fields"] == {}


def test_what_was_remembered_comes_back_as_outputs(run, store):
    memory.remember("QA-1", {"status": "done"}, store)
    result, _ctx, _step = run("memory.recall", key="QA-1", field="status")

    assert result.outputs["known"] is True
    assert result.outputs["value"] == "done"
    assert result.outputs["fields"] == {"status": "done"}


def test_the_named_field_is_there_even_when_nothing_is_remembered(run):
    """The bug this output exists for: a guard has to hold on the first run
    too, and on a first run no name is in the record."""
    result, _ctx, _step = run("memory.recall", key="QA-1", field="status")
    assert result.outputs["value"] == ""


def test_a_condition_on_the_value_resolves_on_a_first_run(run):
    """What the cycle file actually writes. Reaching into `fields` for a name
    that is not there raises, and the executor turns that into a failed step."""
    result, _ctx, _step = run("memory.recall", key="QA-1", field="status")
    root = _scope(result)

    assert conditions.evaluate("${steps.m.outputs.value} != 'done'", root)
    with pytest.raises(Exception):
        conditions.evaluate("${steps.m.outputs.fields.status} != 'done'", root)


def test_outputs_are_flat_rather_than_nested_one_level_too_deep(run):
    """succeeded() takes keyword arguments. A dict passed as `outputs=` becomes
    an output called "outputs", and every reference in the cycle misses."""
    result, _ctx, _step = run("memory.recall", key="QA-1")
    assert "outputs" not in result.outputs
    assert set(result.outputs) >= {"known", "value", "fields", "holder"}


def test_recall_says_who_holds_the_key(run, store):
    memory.claim("QA-1", "somebody-else", 60, store)
    result, _ctx, _step = run("memory.recall", key="QA-1")
    assert result.outputs["holder"] == "somebody-else"


def test_recall_writes_nothing(run, store):
    run("memory.recall", key="QA-1")
    assert not os.path.exists(store), "a step that only reads should not create it"


def test_a_key_is_required():
    assert registry.get("memory.recall").problems({})


# ----------------------------------------------------------------- remember
def test_remembering_puts_it_where_a_later_run_will_find_it(run, store):
    result, _ctx, _step = run("memory.remember", key="QA-1",
                              fields={"status": "done"})
    assert result.ok
    assert memory.recall("QA-1", store) == {"status": "done"}


def test_remembering_merges_with_what_is_already_there(run, store):
    memory.remember("QA-1", {"plan": "x"}, store)
    run("memory.remember", key="QA-1", fields={"status": "done"})
    assert memory.recall("QA-1", store) == {"plan": "x", "status": "done"}


def test_a_counter_moves(run, store):
    run("memory.remember", key="QA-1", bump=["runs"])
    result, _ctx, _step = run("memory.remember", key="QA-1", bump=["runs"])
    assert result.outputs["fields"]["runs"] == 2


def test_counters_written_as_one_line_are_split_rather_than_spelled_out(run,
                                                                        store):
    """`bump: runs` in a hand-written file is a string, and iterating a string
    gives one counter per letter - which it silently did."""
    run("memory.remember", key="QA-1", bump="runs attempts")

    assert sorted(memory.recall("QA-1", store)) == ["attempts", "runs"]


def test_counters_that_are_not_names_at_all_are_refused():
    assert registry.get("memory.remember").problems(
        {"key": "k", "bump": {"a": 1}})


# -------------------------------------------------------------------- claim
def test_a_claim_is_taken_and_reported(run, store):
    result, _ctx, _step = run("memory.claim", key="QA-1")
    assert result.ok
    assert result.outputs["claimed"] is True
    assert memory.holder("QA-1", store) == "run-a"


def test_a_claim_somebody_else_holds_fails_the_step(run, store):
    memory.claim("QA-1", "run-b", 60, store)
    result, _ctx, _step = run("memory.claim", key="QA-1")

    assert not result.ok
    assert "run-b" in result.message
    assert "not a failure of the work" in result.message


def test_the_owner_defaults_to_the_run(run, store):
    run("memory.claim", key="QA-1", run_id="20260919-120000-nightly")
    assert memory.holder("QA-1", store) == "20260919-120000-nightly"


def test_an_owner_of_your_own_lets_a_later_run_take_it_back(run, store):
    run("memory.claim", key="QA-1", owner="the nightly build")
    result, _ctx, _step = run("memory.claim", key="QA-1", run_id="a different run",
                              owner="the nightly build")
    assert result.ok


def test_the_claim_is_held_so_the_run_gives_it_back(run, store):
    """The whole reason it is a ManagedPlugin: the run stops what it holds, so
    a crash, a timeout and a Ctrl+C all release it without the plugin knowing
    about any of them."""
    _result, context, step = run("memory.claim", key="QA-1")
    assert [one[0] for one in context.held()] == ["m"]

    plugin, handle = context.held()[0][1], context.held()[0][2]
    plugin.stop(context, step, handle)
    assert memory.holder("QA-1", store) == ""


def test_a_run_that_fails_still_releases_the_claim(tmp_path, store):
    """Driven through the executor, because the release is its cleanup pass
    doing it rather than anything in the plugin."""
    raw = {"id": "c", "steps": [
        {"id": "hold", "plugin": "memory.claim", "with": {"key": "QA-1"}},
        {"id": "boom", "plugin": "command.shell", "needs": ["hold"],
         "with": {"command": "exit 3"}}]}
    workspace = create("20260919-000000-x", str(tmp_path / "runs"))

    run = executor.run_cycle(parse_cycle(raw, "c"), workspace,
                             run_id="run-a", memory_path=store,
                             observer=bus.NullObserver())

    assert run.steps["boom"].status == "failed"
    assert memory.holder("QA-1", store) == "", "a failed run must not keep it"


def test_a_second_run_of_the_same_cycle_is_turned_away(tmp_path, store):
    raw = {"id": "c", "steps": [
        {"id": "hold", "plugin": "memory.claim", "with": {"key": "QA-1"}}]}
    memory.claim("QA-1", "somebody-else", 600, store)
    workspace = create("20260919-000000-y", str(tmp_path / "runs"))

    run = executor.run_cycle(parse_cycle(raw, "c"), workspace, run_id="mine",
                             memory_path=store, observer=bus.NullObserver())

    assert run.steps["hold"].status == "failed"
    assert "somebody-else" in run.steps["hold"].message


def test_a_claim_that_is_good_for_no_time_at_all_is_refused():
    plugin = registry.get("memory.claim")
    for seconds in (0, -1, "later"):
        assert plugin.problems({"key": "k", "seconds": seconds})


def test_a_reference_is_left_for_the_run_to_resolve():
    assert registry.get("memory.claim").problems(
        {"key": "${vars.task}", "seconds": "${vars.lease}"}) == []


# ------------------------------------------------------------------- shared
def test_reading_says_it_reads_and_writing_says_it_writes():
    """A step that touches what later runs believe should declare it."""
    assert registry.get("memory.recall").metadata.permissions == ()
    for one in ("memory.remember", "memory.claim"):
        assert "filesystem.write" in registry.get(one).metadata.permissions


def test_the_metadata_survives_the_wire():
    import json

    for one in ("memory.recall", "memory.remember", "memory.claim"):
        json.dumps(registry.get(one).metadata.to_dict())


def _scope(result):
    """The scope a condition would be evaluated against, after this step."""
    from domain.cycle import StepRun

    step_run = StepRun("m", status="success", outputs=dict(result.outputs))
    return variables.scope(CycleRun(id="r", cycle_id="c"), None, {"m": step_run},
                           env={})
