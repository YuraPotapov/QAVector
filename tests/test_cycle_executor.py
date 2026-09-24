"""The scheduler: what runs, in what order, at the same time as what, and why not.

Nothing here runs a real command. Every test uses a fake plugin whose behaviour
is written into it, so a test that means "these two overlap" says so with a
``threading.Barrier`` - which either happens or times out - rather than with a
sleep and a stopwatch. There are no sleeps long enough to be flaky and no
assertions on wall-clock duration.
"""

import threading

import pytest

from cycle import registry as registry_mod
from cycle.bus import Recorder
from cycle.context import CancelToken
from cycle.executor import DEFAULT_JOBS, request_stop, run_cycle
from cycle.model import parse_cycle
from cycle.registry import CyclePlugin, PluginMetadata, PluginResult
from domain.cycle import (CANCELLED, FAILED, SKIPPED, SUCCESS, TIMEOUT)


class _Registry:
    """A table holding only what a test put in it."""

    def __init__(self, **plugins):
        self._plugins = {}
        for name, plugin in plugins.items():
            plugin_id = name.replace("_", ".")
            plugin.metadata = PluginMetadata(
                id=plugin_id, name=plugin_id,
                concurrency_group=getattr(plugin, "group", ""),
                reusable=getattr(plugin, "reusable", True),
                asks_when_overdue=getattr(plugin, "asks", True))
            self._plugins[plugin_id] = plugin

    def get(self, plugin_id):
        return self._plugins.get(plugin_id)

    def all_plugins(self):
        return list(self._plugins.values())


class _Fake(CyclePlugin):
    """Does what it was told to, and writes down that it was asked."""

    group = ""

    def __init__(self, behaviour=None, group=""):
        self.group = group
        self._behaviour = behaviour or (lambda context, step: None)
        self.ran = []
        self.prepared = []
        self.cleaned = []
        self._lock = threading.Lock()

    def prepare(self, context, step):
        self.prepared.append(step.id)

    def execute(self, context, step):
        with self._lock:
            self.ran.append(step.id)
        outcome = self._behaviour(context, step)
        if isinstance(outcome, PluginResult):
            return outcome
        return registry_mod.succeeded(**(outcome or {}))

    def cleanup(self, context, step):
        self.cleaned.append(step.id)


def cycle_of(*steps):
    return parse_cycle({"id": "demo", "steps": list(steps)}, "demo")


def step(step_id, plugin="test.fake", **extra):
    entry = {"id": step_id, "plugin": plugin}
    entry.update(extra)
    return entry


def go(cycle, registry, tmp_path, **extra):
    extra.setdefault("observer", Recorder())
    return run_cycle(cycle, str(tmp_path), registry=registry, **extra), extra["observer"]


# ----------------------------------------------------------------------- order
def test_a_chain_runs_in_order(tmp_path):
    plugin = _Fake()
    cycle = cycle_of(step("a"), step("b", needs=["a"]), step("c", needs=["b"]))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path)

    assert plugin.ran == ["a", "b", "c"]
    assert run.status == SUCCESS
    assert run.exit_code == 0


def test_every_step_is_recorded_with_how_long_it_took(tmp_path):
    plugin = _Fake()
    run, _seen = go(cycle_of(step("a")), _Registry(test_fake=plugin), tmp_path)

    only = run.steps["a"]
    assert only.status == SUCCESS
    assert only.attempts == 1
    assert only.started_at and only.ended_at
    assert only.duration_ms >= 0


def test_what_a_step_produced_is_kept(tmp_path):
    plugin = _Fake(lambda context, step: {"port": 5432})
    run, _seen = go(cycle_of(step("pg")), _Registry(test_fake=plugin), tmp_path)
    assert run.steps["pg"].outputs == {"port": 5432}


def test_a_run_with_no_steps_at_all_succeeds_rather_than_hanging(tmp_path):
    run, _seen = go(parse_cycle({"id": "demo", "steps": []}, "demo"),
                    _Registry(), tmp_path)
    assert run.status == SUCCESS


# -------------------------------------------------------------------- parallel
def test_two_independent_branches_run_at_the_same_time(tmp_path):
    """The whole point of the feature, proven rather than timed.

    A barrier of two only opens when both steps are inside it. If the scheduler
    ran them one after the other, the first would wait there until the timeout
    and the test would fail - deterministically, with no sleep anywhere.
    """
    barrier = threading.Barrier(2, timeout=10)

    def meet(context, step):
        barrier.wait()

    plugin = _Fake(meet)
    cycle = cycle_of(step("a"), step("b", needs=["a"]), step("c", needs=["a"]))
    # "a" has nothing to meet, so it must not be part of the barrier.
    plugin._behaviour = lambda context, s: None if s.id == "a" else meet(context, s)

    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path, jobs=4)
    assert run.status == SUCCESS


def test_a_diamond_waits_for_both_branches_before_the_join(tmp_path):
    order = []
    lock = threading.Lock()

    def note(context, step):
        with lock:
            order.append(step.id)

    plugin = _Fake(note)
    cycle = cycle_of(step("a"), step("b", needs=["a"]), step("c", needs=["a"]),
                     step("d", needs=["b", "c"]))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path, jobs=4)

    assert run.status == SUCCESS
    assert order[0] == "a"
    assert order[-1] == "d"
    assert set(order[1:3]) == {"b", "c"}


def test_a_step_starts_as_soon_as_its_own_dependencies_are_done(tmp_path):
    """Readiness, not layers: b must not wait for c, which it knows nothing about.

    `a -> b` and a separate slow `c`. If the scheduler worked in generations, b
    would sit behind c; it does not, so b reaches the barrier while c is still
    inside its own.
    """
    both_running = threading.Barrier(2, timeout=10)
    release_c = threading.Event()

    def behaviour(context, step):
        if step.id == "c":
            both_running.wait()
            release_c.wait(10)
        elif step.id == "b":
            both_running.wait()
            release_c.set()

    plugin = _Fake(behaviour)
    cycle = cycle_of(step("a"), step("b", needs=["a"]), step("c"))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path, jobs=4)
    assert run.status == SUCCESS


def test_only_as_many_steps_run_at_once_as_were_allowed(tmp_path):
    """jobs=1 is a real constraint, not a hint."""
    inside = []
    peak = [0]
    lock = threading.Lock()

    def behaviour(context, step):
        with lock:
            inside.append(step.id)
            peak[0] = max(peak[0], len(inside))
        threading.Event().wait(0.05)
        with lock:
            inside.remove(step.id)

    plugin = _Fake(behaviour)
    cycle = cycle_of(step("a"), step("b"), step("c"), step("d"))
    go(cycle, _Registry(test_fake=plugin), tmp_path, jobs=1)
    assert peak[0] == 1


def test_the_default_number_of_workers_is_used_when_none_is_given(tmp_path):
    plugin = _Fake()
    _run, seen = go(cycle_of(step("a")), _Registry(test_fake=plugin), tmp_path)
    assert seen.of("run_start")[0]["jobs"] == DEFAULT_JOBS


def test_a_concurrency_group_keeps_its_members_apart(tmp_path):
    """For something with module-level state, like engine.runner's stop flag."""
    inside = []
    overlapped = [False]
    lock = threading.Lock()

    def behaviour(context, step):
        with lock:
            if inside:
                overlapped[0] = True
            inside.append(step.id)
        threading.Event().wait(0.05)
        with lock:
            inside.remove(step.id)

    plugin = _Fake(behaviour, group="scenarios")
    cycle = cycle_of(step("a"), step("b"), step("c"))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path, jobs=4)

    assert run.status == SUCCESS
    assert not overlapped[0]


def test_a_group_does_not_hold_up_anything_outside_it(tmp_path):
    """Grouping one plugin must not serialise the whole run."""
    free_ran = threading.Event()
    grouped_waiting = threading.Barrier(2, timeout=10)

    def grouped_behaviour(context, step):
        grouped_waiting.wait()

    def free_behaviour(context, step):
        free_ran.set()
        grouped_waiting.wait()

    grouped = _Fake(grouped_behaviour, group="serial")
    free = _Fake(free_behaviour)
    cycle = cycle_of(step("g", plugin="test.grouped"), step("f", plugin="test.free"))
    run, _seen = go(cycle, _Registry(test_grouped=grouped, test_free=free),
                    tmp_path, jobs=4)

    assert run.status == SUCCESS
    assert free_ran.is_set()


# -------------------------------------------------------------------- failures
def test_a_failed_step_stops_the_run_by_default(tmp_path):
    plugin = _Fake(lambda context, step:
                   registry_mod.failed("no") if step.id == "a" else None)
    cycle = cycle_of(step("a"), step("b", needs=["a"]), step("c"))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path, jobs=1)

    assert run.status == FAILED
    assert run.exit_code == 1
    assert run.steps["a"].status == FAILED
    assert run.steps["b"].status == CANCELLED


def test_a_failed_step_skips_what_depended_on_it_transitively(tmp_path):
    plugin = _Fake(lambda context, step:
                   registry_mod.failed("no") if step.id == "a" else None)
    cycle = cycle_of(step("a", on_failure="continue"),
                     step("b", needs=["a"]), step("c", needs=["b"]))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path)

    assert run.steps["b"].status == SKIPPED
    assert run.steps["c"].status == SKIPPED
    assert "a failed" in run.steps["b"].message


def test_continuing_leaves_unrelated_branches_running(tmp_path):
    plugin = _Fake(lambda context, step:
                   registry_mod.failed("no") if step.id == "a" else None)
    cycle = cycle_of(step("a", on_failure="continue"),
                     step("b", needs=["a"]), step("c"))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path)

    assert run.status == FAILED             # the run still failed
    assert run.steps["c"].status == SUCCESS  # but c was allowed to run
    assert "c" in plugin.ran


def test_a_plugin_that_raises_fails_its_step_rather_than_the_run(tmp_path):
    def explode(context, step):
        raise RuntimeError("something broke")

    plugin = _Fake(explode)
    run, _seen = go(cycle_of(step("a")), _Registry(test_fake=plugin), tmp_path)

    assert run.steps["a"].status == FAILED
    assert "RuntimeError" in run.steps["a"].message
    assert "something broke" in run.steps["a"].message


def test_a_step_whose_plugin_is_missing_fails_and_says_which(tmp_path):
    cycle = cycle_of(step("a", plugin="not.installed"))
    run, _seen = go(cycle, _Registry(), tmp_path)

    assert run.steps["a"].status == FAILED
    assert "not.installed" in run.steps["a"].message


def test_a_reference_to_something_that_is_not_there_fails_the_step(tmp_path):
    plugin = _Fake()
    cycle = cycle_of(step("a", **{"with": {"x": "${steps.ghost.outputs.y}"}}))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path)

    assert run.steps["a"].status == FAILED
    assert "ghost" in run.steps["a"].message


def test_the_run_says_which_step_ended_it(tmp_path):
    plugin = _Fake(lambda context, step: registry_mod.failed("disk full"))
    run, _seen = go(cycle_of(step("build")), _Registry(test_fake=plugin), tmp_path)
    assert "build" in run.message and "disk full" in run.message


# --------------------------------------------------------------------- outputs
def test_outputs_flow_from_one_step_into_the_next_step_s_settings(tmp_path):
    """The reason steps compose without knowing about each other."""
    seen = {}

    def behaviour(context, step):
        if step.id == "pg":
            return {"port": 5432}
        seen["settings"] = dict(step.settings)
        return None

    plugin = _Fake(behaviour)
    cycle = cycle_of(step("pg"),
                     step("app", needs=["pg"],
                          **{"with": {"port": "${steps.pg.outputs.port}",
                                      "url": "db:${steps.pg.outputs.port}"}}))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path)

    assert run.status == SUCCESS
    assert seen["settings"]["port"] == 5432          # kept its type
    assert seen["settings"]["url"] == "db:5432"      # embedded, so a string


def test_a_cycle_variable_reaches_a_step(tmp_path):
    seen = {}
    plugin = _Fake(lambda context, step: seen.update(step.settings) or None)
    cycle = parse_cycle({"id": "demo", "variables": {"branch": "main"},
                         "steps": [step("a", **{"with": {"b": "${vars.branch}"}})]},
                        "demo")
    go(cycle, _Registry(test_fake=plugin), tmp_path)
    assert seen["b"] == "main"


def test_a_run_variable_overrides_the_cycle_s_default(tmp_path):
    seen = {}
    plugin = _Fake(lambda context, step: seen.update(step.settings) or None)
    cycle = parse_cycle({"id": "demo", "variables": {"branch": "main"},
                         "steps": [step("a", **{"with": {"b": "${vars.branch}"}})]},
                        "demo")
    go(cycle, _Registry(test_fake=plugin), tmp_path,
       variables_={"branch": "release"})
    assert seen["b"] == "release"


# ------------------------------------------------------------------ conditions
def test_a_step_whose_condition_is_false_is_skipped(tmp_path):
    plugin = _Fake(lambda context, step:
                   registry_mod.failed("no") if step.id == "tests" else None)
    cycle = cycle_of(step("tests", on_failure="continue"),
                     step("report", **{"if": "${steps.tests.status} == 'success'"},
                          needs=[]))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path, jobs=1)

    assert run.steps["report"].status == SKIPPED
    assert "was not true" in run.steps["report"].message


def test_a_step_whose_condition_is_true_runs(tmp_path):
    """The design document's own example: a debug step that runs on failure.

    It has to both wait for the tests and survive their failing, which is why
    an explicit `if:` overrides the rule that a failed dependency skips its
    dependents.
    """
    plugin = _Fake(lambda context, step:
                   registry_mod.failed("no") if step.id == "tests" else None)
    cycle = cycle_of(step("tests", on_failure="continue"),
                     step("debug", needs=["tests"],
                          **{"if": "${steps.tests.status} == 'failed'"}))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path, jobs=1)

    assert run.steps["debug"].status == SUCCESS
    assert plugin.ran == ["tests", "debug"]


def test_a_condition_can_also_decline_after_a_failed_dependency(tmp_path):
    """The other half: `if:` takes responsibility, so it may still say no."""
    plugin = _Fake(lambda context, step:
                   registry_mod.failed("no") if step.id == "tests" else None)
    cycle = cycle_of(step("tests", on_failure="continue"),
                     step("report", needs=["tests"],
                          **{"if": "${steps.tests.status} == 'success'"}))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path, jobs=1)

    assert run.steps["report"].status == SKIPPED


def test_without_a_condition_a_failed_dependency_still_skips_its_dependents(tmp_path):
    """The exception is for steps that asked for it, not a change of default."""
    plugin = _Fake(lambda context, step:
                   registry_mod.failed("no") if step.id == "tests" else None)
    cycle = cycle_of(step("tests", on_failure="continue"),
                     step("deploy", needs=["tests"]))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path, jobs=1)

    assert run.steps["deploy"].status == SKIPPED


def test_a_condition_that_cannot_be_evaluated_fails_the_step(tmp_path):
    plugin = _Fake()
    cycle = cycle_of(step("a", **{"if": "${steps.ghost.status} == 'x'"}))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path)
    assert run.steps["a"].status == FAILED


def test_a_disabled_step_is_skipped_and_so_is_what_needed_it(tmp_path):
    plugin = _Fake()
    cycle = cycle_of(step("a", disabled=True), step("b", needs=["a"]))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path)

    assert run.steps["a"].status == SKIPPED
    assert run.steps["a"].message == "disabled"
    assert run.steps["b"].status == SKIPPED
    assert plugin.ran == []


# ----------------------------------------------------------------------- retry
def test_a_step_is_tried_again_and_the_attempts_are_counted(tmp_path):
    tries = []

    def behaviour(context, step):
        tries.append(1)
        return None if len(tries) >= 3 else registry_mod.failed("not yet")

    plugin = _Fake(behaviour)
    cycle = cycle_of(step("a", retry={"attempts": 3}))
    run, seen = go(cycle, _Registry(test_fake=plugin), tmp_path)

    assert run.steps["a"].status == SUCCESS
    assert run.steps["a"].attempts == 3
    assert len(seen.of("step_retry")) == 2


def test_a_step_that_never_works_fails_after_its_last_attempt(tmp_path):
    tries = []
    plugin = _Fake(lambda context, step:
                   tries.append(1) or registry_mod.failed("no"))
    cycle = cycle_of(step("a", retry={"attempts": 2}))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path)

    assert run.steps["a"].status == FAILED
    assert len(tries) == 2


def test_a_step_that_raises_is_tried_again_too(tmp_path):
    tries = []

    def behaviour(context, step):
        tries.append(1)
        if len(tries) < 2:
            raise RuntimeError("flaky")

    plugin = _Fake(behaviour)
    cycle = cycle_of(step("a", retry={"attempts": 2}))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path)

    assert run.steps["a"].status == SUCCESS
    assert len(tries) == 2


def test_a_step_that_works_first_time_is_not_tried_again(tmp_path):
    plugin = _Fake()
    cycle = cycle_of(step("a", retry={"attempts": 5}))
    run, seen = go(cycle, _Registry(test_fake=plugin), tmp_path)

    assert run.steps["a"].attempts == 1
    assert seen.of("step_retry") == []


# --------------------------------------------------------------------- timeout
def test_a_step_that_outstays_its_timeout_is_told_to_stop(tmp_path):
    def behaviour(context, step):
        # Waits on its own token, which is what every built-in plugin does.
        context.cancel.wait(20)
        return registry_mod.failed("was stopped")

    plugin = _Fake(behaviour)
    cycle = cycle_of(step("slow", timeout=0.2))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path)

    assert run.steps["slow"].status == TIMEOUT
    assert "timed out" in run.steps["slow"].message


def test_a_timeout_reaches_the_plugin_as_its_own_cancel_token(tmp_path):
    """Not the run's: a shared token would mean one deadline stopped everything."""
    noticed = threading.Event()

    def behaviour(context, step):
        if context.cancel.wait(20):
            noticed.set()

    plugin = _Fake(behaviour)
    cycle = cycle_of(step("slow", timeout=0.2))
    go(cycle, _Registry(test_fake=plugin), tmp_path)
    assert noticed.is_set()


def test_one_step_timing_out_does_not_stop_a_step_beside_it(tmp_path):
    finished = threading.Event()

    def behaviour(context, step):
        if step.id == "slow":
            context.cancel.wait(20)
            return registry_mod.failed("stopped")
        threading.Event().wait(0.4)
        finished.set()

    plugin = _Fake(behaviour)
    cycle = cycle_of(step("slow", timeout=0.1, on_failure="continue"),
                     step("beside"))
    go(cycle, _Registry(test_fake=plugin), tmp_path, jobs=4)
    assert finished.is_set()


def test_a_step_with_no_timeout_is_left_alone(tmp_path):
    plugin = _Fake(lambda context, step: threading.Event().wait(0.3))
    run, _seen = go(cycle_of(step("a")), _Registry(test_fake=plugin), tmp_path)
    assert run.steps["a"].status == SUCCESS


# ------------------------------------------------------- past its timeout, asked
@pytest.fixture
def asking(tmp_path):
    """Somebody there to ask, with the questions written where a test reads them."""
    from cycle import ask as ask_mod
    from engine import events

    out = tmp_path / "events.jsonl"
    events.configure(str(out))
    ask_mod.reset()
    ask_mod.configure(True)
    yield out
    ask_mod.reset()
    events.configure(None)


def _emitted(path, kind):
    import json

    if not path.exists():
        return []
    return [one for one in map(json.loads, path.read_text(encoding="utf-8").splitlines())
            if one.get("kind") == kind]


def _answer_when_asked(path, answer):
    """Answer the first overdue question from another thread, as the GUI would."""
    import time
    from cycle import ask as ask_mod

    def later():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            found = _emitted(path, "cycle.ask")
            if found and ask_mod.deliver(found[0]["id"], answer=answer, who="tester"):
                return
            time.sleep(0.01)
    threading.Thread(target=later, daemon=True).start()


def _slow(finish):
    """Runs until ``finish`` is set or its token is, whichever comes first."""
    def behaviour(context, step):
        while not finish.is_set():
            if context.cancel.wait(0.05):
                return registry_mod.failed("was stopped")
        return None
    return behaviour


def test_a_step_past_its_timeout_is_asked_about_and_can_carry_on(tmp_path, asking):
    finish = threading.Event()
    _answer_when_asked(asking, "Continue")

    def behaviour(context, step):
        # Keep going until the answer has been given, then finish on our own.
        while not _emitted(asking, "cycle.ask"):
            context.cancel.wait(0.05)
        # Longer than the scheduler's idle tick, so the answer is acted on
        # while the step is still going. The step's token must stay clear.
        assert not context.cancel.wait(1.2)
        finish.set()
        return _slow(finish)(context, step)

    run, seen = go(cycle_of(step("slow", timeout=0.2)),
                   _Registry(test_fake=_Fake(behaviour)), tmp_path)

    assert run.steps["slow"].status == SUCCESS, run.steps["slow"].message
    question = _emitted(asking, "cycle.ask")[0]
    assert question["options"] == ["Continue", "Cancel"]
    assert "longer than expected" in question["question"]
    stages = [call[1]["stage"]["title"] for call in seen.calls
              if call[0] == "step_stage"]
    assert any(title.startswith("Given another") for title in stages)


def test_cancelling_the_question_stops_the_step_as_a_timeout(tmp_path, asking):
    _answer_when_asked(asking, "Cancel")
    run, _seen = go(cycle_of(step("slow", timeout=0.2)),
                    _Registry(test_fake=_Fake(_slow(threading.Event()))), tmp_path)

    assert run.steps["slow"].status == TIMEOUT
    assert "timed out after 0.2s" in run.steps["slow"].message
    assert "Cancel" in run.steps["slow"].message


def test_a_window_closed_without_an_answer_is_not_a_yes(tmp_path, asking):
    _answer_when_asked(asking, "")
    run, _seen = go(cycle_of(step("slow", timeout=0.2)),
                    _Registry(test_fake=_Fake(_slow(threading.Event()))), tmp_path)
    assert run.steps["slow"].status == TIMEOUT


def test_a_step_that_finishes_while_asked_withdraws_the_question(tmp_path, asking):
    import time

    def behaviour(context, step):
        while not _emitted(asking, "cycle.ask"):
            context.cancel.wait(0.05)
        return None                           # done before anybody answered

    run, _seen = go(cycle_of(step("slow", timeout=0.2)),
                    _Registry(test_fake=_Fake(behaviour)), tmp_path)

    assert run.steps["slow"].status == SUCCESS
    asked = _emitted(asking, "cycle.ask")[0]
    deadline = time.monotonic() + 5
    while not _emitted(asking, "cycle.ask.withdrawn") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _emitted(asking, "cycle.ask.withdrawn")[0]["id"] == asked["id"]


def test_a_plugin_whose_deadline_is_its_own_is_not_asked_about(tmp_path, asking):
    plugin = _Fake(_slow(threading.Event()))
    plugin.asks = False
    run, _seen = go(cycle_of(step("slow", timeout=0.2)),
                    _Registry(test_fake=plugin), tmp_path)

    assert run.steps["slow"].status == TIMEOUT
    assert _emitted(asking, "cycle.ask") == []


def test_with_nobody_to_ask_a_timeout_stops_the_step_as_before(tmp_path):
    run, _seen = go(cycle_of(step("slow", timeout=0.2)),
                    _Registry(test_fake=_Fake(_slow(threading.Event()))), tmp_path)
    assert run.steps["slow"].status == TIMEOUT
    assert run.steps["slow"].message.startswith("timed out after 0.2s")


# ---------------------------------------------------------------- cancellation
def test_cancelling_a_run_leaves_no_step_still_running(tmp_path):
    started = threading.Event()
    token = CancelToken()

    def behaviour(context, step):
        started.set()
        context.cancel.wait(20)
        return registry_mod.failed("stopped")

    plugin = _Fake(behaviour)

    def stop_once_it_is_going():
        started.wait(10)
        token.set("stop")

    threading.Thread(target=stop_once_it_is_going, daemon=True).start()

    cycle = cycle_of(step("a"), step("b", needs=["a"]))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path, cancel=token)

    assert run.status == CANCELLED
    assert all(one.status != "running" for one in run.steps.values())
    assert run.steps["b"].status == CANCELLED


def test_a_run_cancelled_before_it_starts_runs_nothing(tmp_path):
    token = CancelToken()
    token.set("stopped before it began")
    plugin = _Fake()
    run, _seen = go(cycle_of(step("a"), step("b")), _Registry(test_fake=plugin),
                    tmp_path, cancel=token)

    assert run.status == CANCELLED
    assert plugin.ran == []


def test_the_run_remembers_why_it_was_stopped(tmp_path):
    token = CancelToken()
    token.set("the user pressed Stop")
    run, _seen = go(cycle_of(step("a")), _Registry(test_fake=_Fake()), tmp_path,
                    cancel=token)
    assert run.message == "the user pressed Stop"


def test_a_run_can_be_stopped_by_its_id(tmp_path):
    """How the launcher's control thread reaches a run it holds no reference to."""
    started = threading.Event()

    def behaviour(context, step):
        started.set()
        context.cancel.wait(20)
        return registry_mod.failed("stopped")

    def stopper():
        started.wait(10)
        assert request_stop("the-run") == 1

    threading.Thread(target=stopper, daemon=True).start()
    run, _seen = go(cycle_of(step("a")), _Registry(test_fake=_Fake(behaviour)),
                    tmp_path, run_id="the-run")
    assert run.status == CANCELLED


def test_stopping_a_run_that_is_not_there_says_so_rather_than_raising():
    assert request_stop("no-such-run") == 0


def test_a_finished_run_is_no_longer_stoppable(tmp_path):
    go(cycle_of(step("a")), _Registry(test_fake=_Fake()), tmp_path,
       run_id="finished")
    assert request_stop("finished") == 0


# ---------------------------------------------------------------- housekeeping
def test_cleanup_runs_for_every_step_that_started(tmp_path):
    plugin = _Fake()
    cycle = cycle_of(step("a"), step("b", needs=["a"]))
    go(cycle, _Registry(test_fake=plugin), tmp_path)
    assert sorted(plugin.cleaned) == ["a", "b"]


def test_cleanup_runs_even_when_the_step_raised(tmp_path):
    def explode(context, step):
        raise RuntimeError("no")

    plugin = _Fake(explode)
    go(cycle_of(step("a")), _Registry(test_fake=plugin), tmp_path)
    assert plugin.cleaned == ["a"]


def test_cleanup_does_not_run_for_a_step_that_never_started(tmp_path):
    plugin = _Fake(lambda context, step:
                   registry_mod.failed("no") if step.id == "a" else None)
    cycle = cycle_of(step("a"), step("b", needs=["a"]))
    go(cycle, _Registry(test_fake=plugin), tmp_path)
    assert plugin.cleaned == ["a"]


def test_a_cleanup_that_fails_does_not_change_what_the_run_reported(tmp_path):
    """Tidying up must never mask the result the run actually came to."""
    class Messy(_Fake):
        def cleanup(self, context, step):
            raise RuntimeError("could not tidy up")

    run, _seen = go(cycle_of(step("a")), _Registry(test_fake=Messy()), tmp_path)
    assert run.status == SUCCESS


def test_what_a_step_held_open_is_stopped_when_the_run_ends(tmp_path):
    stopped = []

    class Service(_Fake):
        def execute(self, context, step):
            context.hold(step.id, self, {"name": step.id})
            return registry_mod.succeeded()

        def stop(self, context, step, handle):
            stopped.append(handle["name"])

    cycle = cycle_of(step("pg"), step("app", needs=["pg"]))
    go(cycle, _Registry(test_fake=Service()), tmp_path)

    # Reverse order: the app was started after the database it needs, so it
    # has to be stopped before it.
    assert stopped == ["app", "pg"]


def test_what_was_held_is_stopped_even_when_the_run_failed(tmp_path):
    stopped = []

    class Service(_Fake):
        def execute(self, context, step):
            if step.id == "boom":
                raise RuntimeError("no")
            context.hold(step.id, self, {"name": step.id})
            return registry_mod.succeeded()

        def stop(self, context, step, handle):
            stopped.append(handle["name"])

    cycle = cycle_of(step("pg"), step("boom", needs=["pg"]))
    run, _seen = go(cycle, _Registry(test_fake=Service()), tmp_path)

    assert run.status == FAILED
    assert stopped == ["pg"]


def test_a_stop_that_fails_is_survived(tmp_path):
    class Service(_Fake):
        def execute(self, context, step):
            context.hold(step.id, self, {})
            return registry_mod.succeeded()

        def stop(self, context, step, handle):
            raise RuntimeError("could not stop it")

    run, _seen = go(cycle_of(step("pg")), _Registry(test_fake=Service()), tmp_path)
    assert run.status == SUCCESS


# ------------------------------------------------------------------- reporting
def test_the_run_is_announced_with_the_graph_the_canvas_draws(tmp_path):
    """The same payload --cycle-show returns; there is no second model."""
    from cycle.model import to_graph

    cycle = cycle_of(step("a"), step("b", needs=["a"]))
    _run, seen = go(cycle, _Registry(test_fake=_Fake()), tmp_path)

    announced = seen.of("run_start")[0]["graph"]
    assert announced == to_graph(cycle)


def test_every_step_is_announced_starting_and_ending(tmp_path):
    cycle = cycle_of(step("a"), step("b", needs=["a"]))
    _run, seen = go(cycle, _Registry(test_fake=_Fake()), tmp_path)

    assert {call["step"] for call in seen.of("step_start")} == {"a", "b"}
    assert {call["step"] for call in seen.of("step_end")} == {"a", "b"}


def test_a_skipped_step_is_announced_as_skipped(tmp_path):
    cycle = cycle_of(step("a", disabled=True))
    _run, seen = go(cycle, _Registry(test_fake=_Fake()), tmp_path)
    assert seen.of("step_skipped")[0]["step"] == "a"


def test_the_run_is_announced_ending(tmp_path):
    _run, seen = go(cycle_of(step("a")), _Registry(test_fake=_Fake()), tmp_path)
    assert seen.kinds()[0] == "run_start"
    assert seen.kinds()[-1] == "run_end"
    assert seen.of("run_end")[0]["status"] == SUCCESS


def test_an_artifact_a_step_produced_is_announced_and_kept(tmp_path):
    from domain.cycle import Artifact

    def behaviour(context, step):
        return PluginResult(SUCCESS, artifacts=[
            Artifact("log", "steps/a/out.log", "a", name="out")])

    run, seen = go(cycle_of(step("a")), _Registry(test_fake=_Fake(behaviour)),
                   tmp_path)

    assert seen.of("artifact")[0]["name"] == "out"
    assert run.steps["a"].artifacts[0].path == "steps/a/out.log"


def test_the_run_counts_how_its_steps_ended(tmp_path):
    plugin = _Fake(lambda context, step:
                   registry_mod.failed("no") if step.id == "b" else None)
    cycle = cycle_of(step("a"), step("b", on_failure="continue"),
                     step("c", needs=["b"]))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path, jobs=1)

    tally = run.tally()
    assert tally[SUCCESS] == 1
    assert tally[FAILED] == 1
    assert tally[SKIPPED] == 1
    # Every terminal status is present even at zero, so a summary needs no guard.
    assert set(tally) == set(__import__("domain.cycle", fromlist=["x"]).TERMINAL)


def test_an_observer_that_breaks_does_not_break_the_run(tmp_path):
    from cycle.bus import Tee

    class Broken:
        def __getattr__(self, name):
            def explode(*args, **kwargs):
                raise RuntimeError("no")
            return explode

    run = run_cycle(cycle_of(step("a")), str(tmp_path),
                    registry=_Registry(test_fake=_Fake()),
                    observer=Tee([Broken(), Recorder()]))
    assert run.status == SUCCESS


# --------------------------------------------------- running part of a cycle
# ``--cycle-only`` and ``--cycle-from`` take the steps they are not running
# from the last run of the same cycle. What may and may not be taken that way
# is the whole of what these are about: a partial run that inherits the wrong
# thing is a run that silently did not do what its file says it does.
def _earlier(**statuses):
    """A finished run to borrow from: step id -> the status it ended in."""
    from domain.cycle import CycleRun, StepRun

    return CycleRun(id="20260916-120000-demo", cycle_id="demo",
                    steps={one: StepRun(one, plugin="test.fake", status=status,
                                        outputs={"value": one})
                           for one, status in statuses.items()})


def test_a_step_this_run_is_not_doing_is_taken_from_the_last_one(tmp_path):
    plugin = _Fake()
    cycle = cycle_of(step("a"), step("b", needs=["a"]))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path,
                    only=["b"], reuse=_earlier(a=SUCCESS))

    assert plugin.ran == ["b"]
    assert run.steps["a"].status == SUCCESS
    assert run.steps["a"].outputs == {"value": "a"}
    assert "reused from 20260916-120000-demo" in run.steps["a"].message


def test_a_step_that_never_ran_back_then_is_not_a_result_to_borrow(tmp_path):
    """It was recorded as reused and marked success with empty outputs, so a
    later gate read ``${steps.x.outputs.ok}`` against nothing at all."""
    plugin = _Fake()
    cycle = cycle_of(step("a"), step("b", needs=["a"]))
    run, _seen = go(cycle, _Registry(test_fake=plugin), tmp_path,
                    only=["b"], reuse=_earlier(a=SKIPPED))

    assert run.steps["a"].status == SKIPPED
    assert run.steps["a"].message == "not part of this run"


def test_a_partial_run_is_refused_when_what_it_reads_never_ran(tmp_path):
    from cycle.executor import missing_for

    cycle = cycle_of(step("a"), step("b", needs=["a"]))
    assert missing_for(cycle, ["b"], _earlier(a=SKIPPED),
                       _Registry(test_fake=_Fake())) == ["a"]
    assert missing_for(cycle, ["b"], _earlier(a=SUCCESS),
                       _Registry(test_fake=_Fake())) == []


def test_partial_report_can_wait_for_a_skipped_optional_branch(tmp_path):
    from cycle.executor import missing_for

    cycle = cycle_of(step("work"), step("optional", **{"if": "'yes' == 'no'"}),
                     step("report", needs=["work", "optional"], **{"if": "${run.id}"}))
    assert missing_for(cycle, ["report"], _earlier(work=SUCCESS, optional=SKIPPED),
                       _Registry(test_fake=_Fake())) == []


def test_partial_run_never_turns_a_saved_failure_into_success(tmp_path):
    plugin = _Fake()
    cycle = cycle_of(step("a", on_failure="continue"), step("b", needs=["a"]))
    result, _ = go(cycle, _Registry(test_fake=plugin), tmp_path,
                   only=["b"], reuse=_earlier(a=FAILED))
    assert result.steps["a"].status == FAILED
    assert result.steps["b"].status == SKIPPED
    assert plugin.ran == []


def test_partial_run_materializes_saved_artifacts_in_its_workspace(tmp_path):
    from domain.cycle import Artifact

    source = tmp_path / "old"
    (source / "steps/a").mkdir(parents=True)
    (source / "steps/a/report.txt").write_text("already paid")
    earlier = _earlier(a=SUCCESS)
    earlier.workspace = str(source)
    earlier.steps["a"].artifacts = [Artifact("text", "steps/a/report.txt", "a")]
    earlier.steps["a"].outputs = {"path": "steps/a/report.txt"}
    plugin = _Fake(lambda context, step: {"text":
                   (tmp_path / "new" / step.settings["path"]).read_text()})
    cycle = cycle_of(step("a"), step("b", needs=["a"],
                    **{"with": {"path": "${steps.a.outputs.path}"}}))
    result, _ = go(cycle, _Registry(test_fake=plugin), tmp_path / "new",
                   only=["b"], reuse=earlier)
    assert result.ok and result.steps["b"].outputs["text"] == "already paid"


def test_a_step_whose_answer_belongs_to_this_run_is_never_borrowed(tmp_path):
    """An approval given an hour ago to another run is not agreement to this
    one. The gate is asked again however little of the cycle is being run."""
    gate = _Fake()
    gate.reusable = False
    plugin = _Fake()
    cycle = cycle_of(step("plan"), step("approve", plugin="test.gate",
                                        needs=["plan"]),
                     step("work", needs=["approve"]))
    run, _seen = go(cycle, _Registry(test_fake=plugin, test_gate=gate), tmp_path,
                    only=["work"], reuse=_earlier(plan=SUCCESS, approve=SUCCESS))

    assert gate.ran == ["approve"]                  # asked again, not inherited
    assert "reused" not in (run.steps["approve"].message or "")
    # And the expensive step above it is still borrowed - that is what running
    # part of a cycle is for.
    assert "reused from" in run.steps["plan"].message


def test_nothing_decided_on_an_old_approval_is_borrowed_either(tmp_path):
    """The gate below it read the old answer, so borrowing *it* would put that
    answer back into the run through the side door."""
    gate = _Fake()
    gate.reusable = False
    plugin = _Fake()
    cycle = cycle_of(step("plan"), step("approve", plugin="test.gate",
                                        needs=["plan"]),
                     step("preflight", needs=["approve"]),
                     step("work", needs=["preflight"]))
    run, _seen = go(cycle, _Registry(test_fake=plugin, test_gate=gate), tmp_path,
                    only=["work"],
                    reuse=_earlier(plan=SUCCESS, approve=SUCCESS,
                                   preflight=SUCCESS))

    assert plugin.ran == ["preflight", "work"]      # both decided again
    assert "reused from" in run.steps["plan"].message


def test_a_refused_gate_stops_a_partial_run_like_any_other(tmp_path):
    gate = _Fake(lambda context, step: registry_mod.failed("not approved"))
    gate.reusable = False
    plugin = _Fake()
    cycle = cycle_of(step("plan"), step("approve", plugin="test.gate",
                                        needs=["plan"]),
                     step("work", needs=["approve"]))
    run, _seen = go(cycle, _Registry(test_fake=plugin, test_gate=gate), tmp_path,
                    only=["work"], reuse=_earlier(plan=SUCCESS, approve=SUCCESS))

    assert run.steps["approve"].status == FAILED
    assert run.steps["work"].status == CANCELLED
    assert plugin.ran == []
    assert run.status == FAILED


def test_a_gate_this_run_does_itself_is_not_something_it_is_missing(tmp_path):
    """It is not in the earlier run at all, and the run must still be allowed:
    it is going to ask, not to inherit."""
    from cycle.executor import missing_for

    gate = _Fake()
    gate.reusable = False
    cycle = cycle_of(step("plan"), step("approve", plugin="test.gate",
                                        needs=["plan"]),
                     step("work", needs=["approve"]))
    assert missing_for(cycle, ["work"], _earlier(plan=SUCCESS),
                       _Registry(test_fake=_Fake(), test_gate=gate)) == []
