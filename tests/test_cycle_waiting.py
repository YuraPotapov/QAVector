"""A step that says "not yet" instead of finishing, and the scheduler's answer.

This is the third thing a step can be. Before it there were two - in flight, or
finished - and every loop in the scheduler was written against that. So the
tests here are mostly about the seams that assumption left: the loop that used
to end when nothing was in flight, the deadline that used to be per-turn, the
verdict passes that used to see only PENDING and in-flight steps.

The property that pays for all of it is the first one: **a waiting step gives
its worker back**. A cycle with more waits than jobs is the ordinary case - let
four deploys settle - and it either works or the run deadlocks against itself.

Times here are short (hundredths of a second) because what is being tested is
the arithmetic and the transitions, not the clock.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import bus, executor, registry                          # noqa: E402
from cycle.context import CancelToken                              # noqa: E402
from cycle.registry import CyclePlugin, PluginMetadata             # noqa: E402
from cycle.workspace import create                                 # noqa: E402
from domain.cycle import (CANCELLED, SUCCESS, TIMEOUT, WAITING,     # noqa: E402
                          Cycle, CycleStep)


class _Registry(object):
    """A registry of exactly the plugins a test wrote. Nothing else is visible."""

    def __init__(self, **plugins):
        self._plugins = plugins

    def get(self, plugin_id):
        return self._plugins.get(plugin_id)


class _Counter(CyclePlugin):
    """Waits a fixed number of turns, then succeeds. Counts what it was asked."""

    metadata = PluginMetadata(id="t.counter", name="Counter",
                              category=registry.UTILITY, summary="counts")

    def __init__(self, turns=1, seconds=0.02):
        self.turns = turns
        self.seconds = seconds
        self.calls = 0
        self.at_once = 0
        self._live = 0
        self._lock = threading.Lock()

    def problems(self, settings):
        return []

    def execute(self, context, step):
        with self._lock:
            self.calls += 1
            self._live += 1
            self.at_once = max(self.at_once, self._live)
            mine = self.calls
        try:
            time.sleep(0.005)
            if mine <= self.turns:
                return registry.waiting(self.seconds, message="turn %d" % mine,
                                        seen=mine)
            return registry.succeeded(message="done", seen=mine)
        finally:
            with self._lock:
                self._live -= 1


class _Forever(CyclePlugin):
    """Never finishes. Only its step's own deadline ever ends it."""

    metadata = PluginMetadata(id="t.forever", name="Forever",
                              category=registry.UTILITY, summary="waits")

    def __init__(self):
        self.calls = 0

    def problems(self, settings):
        return []

    def execute(self, context, step):
        self.calls += 1
        return registry.waiting(0.02, message="still nothing")


class _Quick(CyclePlugin):
    metadata = PluginMetadata(id="t.quick", name="Quick",
                              category=registry.UTILITY, summary="finishes")

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def problems(self, settings):
        return []

    def execute(self, context, step):
        self.calls += 1
        if self.fail:
            return registry.failed("no")
        return registry.succeeded(message="yes")


def _cycle(*steps):
    return Cycle(id="c", name="c", steps=list(steps))


@pytest.fixture
def go(tmp_path):
    def run(cycle, plugins, jobs=4, cancel=None, observer=None):
        workspace = create("20260921-000000-w", str(tmp_path / "runs"))
        return executor.run_cycle(
            cycle, workspace, registry=_Registry(**plugins), jobs=jobs,
            cancel=cancel, observer=observer)
    return run


# ------------------------------------------------------- it comes back to it
def test_a_waiting_step_is_run_again_and_then_finishes(go):
    counter = _Counter(turns=2)
    run = go(_cycle(CycleStep(id="a", plugin="t.counter")),
             {"t.counter": counter})

    assert run.steps["a"].status == SUCCESS
    assert counter.calls == 3, "two waits and the turn that finished"
    assert run.steps["a"].outputs["seen"] == 3


def test_what_it_produced_while_waiting_is_kept(go):
    """A plugin that looked, found nothing and said so may still have learned
    something worth reading."""
    seen = []

    class Watcher(bus.NullObserver):
        def step_waiting(self, step_run, seconds):
            seen.append(dict(step_run.outputs))

    go(_cycle(CycleStep(id="a", plugin="t.counter")),
       {"t.counter": _Counter(turns=1)}, observer=Watcher())

    assert seen and seen[0]["seen"] == 1


def test_the_run_does_not_end_while_a_step_is_waiting(go):
    """The loop used to end the moment nothing was in flight, which would have
    cancelled the waiting step instead of coming back to it."""
    run = go(_cycle(CycleStep(id="a", plugin="t.counter")),
             {"t.counter": _Counter(turns=3, seconds=0.03)})

    assert run.status == SUCCESS
    assert run.steps["a"].status == SUCCESS


def test_a_waiting_step_does_not_hold_a_worker(go):
    """The reason this exists at all. Six waiting steps through two jobs: if a
    wait held its worker the run would deadlock against itself."""
    counters = {"t.counter": _Counter(turns=2, seconds=0.02)}
    steps = [CycleStep(id="s%d" % n, plugin="t.counter") for n in range(6)]

    run = go(_cycle(*steps), counters, jobs=2)

    assert all(run.steps[one.id].status == SUCCESS for one in steps)
    assert counters["t.counter"].at_once <= 2, "the pool is still the pool"


def test_the_record_says_how_many_times_it_actually_ran(go):
    run = go(_cycle(CycleStep(id="a", plugin="t.counter")),
             {"t.counter": _Counter(turns=2)})

    assert run.steps["a"].attempts == 3
    assert run.steps["a"].metrics["waits"] == 2


def test_a_wait_is_not_spent_as_a_retry(go):
    """A step that asked to be come back to has not failed. Retrying it there
    would call it again at once, spending the whole retry budget in a moment
    and never letting the scheduler see the wait."""
    counter = _Counter(turns=1, seconds=0.3)
    recorder = bus.Recorder()
    run = go(_cycle(CycleStep(id="a", plugin="t.counter", retry_attempts=3,
                              retry_delay=0)),
             {"t.counter": counter}, observer=recorder)

    assert run.steps["a"].status == SUCCESS
    assert counter.calls == 2, "one wait and the turn that finished, not four"
    assert "step_retry" not in recorder.kinds()


def test_a_step_that_waits_may_still_be_retried_when_it_actually_fails(go):
    """The budget is untouched by waiting, not disabled by it."""
    class Flaky(CyclePlugin):
        metadata = PluginMetadata(id="t.flaky", name="Flaky",
                                  category=registry.UTILITY, summary="")

        def __init__(self):
            self.calls = 0

        def problems(self, settings):
            return []

        def execute(self, context, step):
            self.calls += 1
            if self.calls == 1:
                return registry.waiting(0.3, message="not yet")
            if self.calls == 2:
                return registry.failed("no")
            return registry.succeeded(message="yes")

    flaky = Flaky()
    run = go(_cycle(CycleStep(id="a", plugin="t.flaky", retry_attempts=3,
                              retry_delay=0)), {"t.flaky": flaky})

    assert run.steps["a"].status == SUCCESS
    assert flaky.calls == 3


def test_a_step_is_prepared_once_however_many_times_it_waits(go):
    """`cleanup` is called exactly once for every step that reached `prepare`,
    so preparing per turn would take four things and give one back."""
    class Counted(CyclePlugin):
        metadata = PluginMetadata(id="t.counted", name="Counted",
                                  category=registry.UTILITY, summary="")

        def __init__(self):
            self.prepares = 0
            self.cleanups = 0
            self.calls = 0

        def problems(self, settings):
            return []

        def prepare(self, context, step):
            self.prepares += 1

        def cleanup(self, context, step):
            self.cleanups += 1

        def execute(self, context, step):
            self.calls += 1
            if self.calls <= 3:
                return registry.waiting(0.02, message="not yet")
            return registry.succeeded(message="done")

    counted = Counted()
    go(_cycle(CycleStep(id="a", plugin="t.counted")), {"t.counted": counted})

    assert counted.calls == 4
    assert counted.prepares == 1
    assert counted.cleanups == 1


def test_a_step_that_never_waits_is_untouched_by_any_of_this(go):
    quick = _Quick()
    run = go(_cycle(CycleStep(id="a", plugin="t.quick")), {"t.quick": quick})

    assert run.steps["a"].status == SUCCESS
    assert run.steps["a"].attempts == 1
    assert "waits" not in (run.steps["a"].metrics or {})


# ------------------------------------------------------ what waits for what
def test_everything_downstream_waits_too(go):
    """Waiting is what `needs` already means, said about time."""
    order = []

    class Noting(bus.NullObserver):
        def step_end(self, step_run):
            order.append(step_run.step_id)

    run = go(_cycle(CycleStep(id="a", plugin="t.counter"),
                    CycleStep(id="b", plugin="t.quick", needs=("a",))),
             {"t.counter": _Counter(turns=2), "t.quick": _Quick()},
             observer=Noting())

    assert run.steps["b"].status == SUCCESS
    assert order == ["a", "b"], "b must not start until a is finished"


def test_a_step_beside_it_runs_while_it_waits(go):
    quick = _Quick()
    run = go(_cycle(CycleStep(id="a", plugin="t.counter"),
                    CycleStep(id="b", plugin="t.quick")),
             {"t.counter": _Counter(turns=3, seconds=0.05), "t.quick": quick},
             jobs=2)

    assert run.steps["b"].status == SUCCESS
    assert quick.calls == 1


# --------------------------------------------------------------- the bounds
def test_the_step_s_timeout_bounds_the_whole_wait_not_each_turn(go):
    """Otherwise `timeout:` would stop bounding anything: a step that waits
    four times would quietly get four times its deadline."""
    forever = _Forever()
    started = time.monotonic()
    run = go(_cycle(CycleStep(id="a", plugin="t.forever", timeout=0.3)),
             {"t.forever": forever})

    assert run.steps["a"].status == TIMEOUT
    assert time.monotonic() - started < 3.0, "it must not have restarted its clock"
    assert "timed out" in run.steps["a"].message


def test_a_timed_out_wait_says_it_was_waiting(go):
    run = go(_cycle(CycleStep(id="a", plugin="t.forever", timeout=0.2)),
             {"t.forever": _Forever()})

    assert "waiting" in run.steps["a"].message


def test_a_wait_past_its_deadline_is_not_slept_out_first(go):
    """Asking to wait an hour under `timeout: 0.2` must end at the deadline,
    not at the hour."""
    class Hour(CyclePlugin):
        metadata = PluginMetadata(id="t.hour", name="Hour",
                                  category=registry.UTILITY, summary="")

        def problems(self, settings):
            return []

        def execute(self, context, step):
            return registry.waiting(3600, message="see you next hour")

    started = time.monotonic()
    run = go(_cycle(CycleStep(id="a", plugin="t.hour", timeout=0.2)),
             {"t.hour": Hour()})

    assert run.steps["a"].status == TIMEOUT
    assert time.monotonic() - started < 5.0


def test_a_stopped_run_does_not_sit_out_a_wait(go):
    """A run stopped a minute into an hour-long wait has to end now."""
    class Hour(CyclePlugin):
        metadata = PluginMetadata(id="t.hour", name="Hour",
                                  category=registry.UTILITY, summary="")

        def problems(self, settings):
            return []

        def execute(self, context, step):
            return registry.waiting(3600, message="see you next hour")

    cancel = CancelToken()
    threading.Timer(0.2, lambda: cancel.set("the run was stopped")).start()

    started = time.monotonic()
    run = go(_cycle(CycleStep(id="a", plugin="t.hour")), {"t.hour": Hour()},
             cancel=cancel)

    assert time.monotonic() - started < 5.0
    assert run.steps["a"].status == CANCELLED


def test_a_wait_asked_for_on_a_stopping_run_is_refused(go):
    """Otherwise a plugin could keep a stopped run alive by asking nicely."""
    class Rude(CyclePlugin):
        metadata = PluginMetadata(id="t.rude", name="Rude",
                                  category=registry.UTILITY, summary="")

        def problems(self, settings):
            return []

        def execute(self, context, step):
            context.cancel.wait(5.0)          # until the run is stopped
            return registry.waiting(3600, message="one more hour please")

    cancel = CancelToken()
    threading.Timer(0.2, lambda: cancel.set("the run was stopped")).start()
    run = go(_cycle(CycleStep(id="a", plugin="t.rude")), {"t.rude": Rude()},
             cancel=cancel)

    assert run.steps["a"].status == CANCELLED
    assert run.steps["a"].status != WAITING


def test_a_failure_elsewhere_ends_a_waiting_step(go):
    run = go(_cycle(CycleStep(id="a", plugin="t.counter"),
                    CycleStep(id="b", plugin="t.quick")),
             {"t.counter": _Counter(turns=50, seconds=0.05),
              "t.quick": _Quick(fail=True)}, jobs=2)

    assert run.steps["a"].status == CANCELLED


# ------------------------------------------------------------ what is said
def test_waiting_is_announced_as_itself_rather_than_as_a_retry(go):
    """A reader who cannot tell the two apart sees a healthy cycle as one
    failing over and over."""
    recorder = bus.Recorder()
    go(_cycle(CycleStep(id="a", plugin="t.counter")),
       {"t.counter": _Counter(turns=2)}, observer=recorder)

    assert recorder.kinds().count("step_waiting") == 2
    assert "step_retry" not in recorder.kinds()


def test_the_waiting_event_says_how_long_is_left(go):
    # Comfortably above MIN_WAIT, or the floor would be what is measured here
    # rather than the number the plugin asked for.
    recorder = bus.Recorder()
    go(_cycle(CycleStep(id="a", plugin="t.counter")),
       {"t.counter": _Counter(turns=1, seconds=0.4)}, observer=recorder)

    waited = recorder.of("step_waiting")[0]
    assert 0.3 < waited["seconds"] <= 0.4
    assert waited["message"] == "turn 1"


def test_the_record_on_disk_says_waiting_while_it_waits(go, tmp_path):
    """A run parked for an hour would otherwise spend that hour claiming the
    step was running - and that is the question somebody reading it has."""
    from cycle import run as run_mod

    seen = []

    class Peeking(bus.NullObserver):
        """Reads the file back at the moment the step parks itself.

        Second in the Tee, which fans out in order, so the Persister before it
        has already written by the time this looks.
        """

        def __init__(self, workspace):
            self.workspace = workspace

        def step_waiting(self, step_run, seconds):
            found = run_mod.load(self.workspace)
            seen.append(found.steps[step_run.step_id].status if found else None)

    persister = run_mod.Persister(root=str(tmp_path / "runs"))
    peeking = Peeking("")

    class Noting(bus.NullObserver):
        def run_start(self, run, graph, jobs=1):
            peeking.workspace = run.workspace

    go(_cycle(CycleStep(id="a", plugin="t.counter")),
       {"t.counter": _Counter(turns=1)},
       observer=bus.Tee([Noting(), persister, peeking]))

    assert seen == [WAITING]


def test_a_waiting_step_is_not_announced_as_ended(go):
    """`step_end` means a verdict, and a waiting step has not reached one."""
    recorder = bus.Recorder()
    go(_cycle(CycleStep(id="a", plugin="t.counter")),
       {"t.counter": _Counter(turns=2)}, observer=recorder)

    assert recorder.kinds().count("step_end") == 1


# ------------------------------------------------------------- the helpers
def test_a_plugin_cannot_ask_to_spin_hot():
    """Zero is a bug or a condition about to change; either way it must not
    turn the scheduler into a busy loop."""
    result = registry.waiting(0)
    assert result.resume_at - time.monotonic() >= registry.MIN_WAIT * 0.9


def test_waiting_is_not_a_verdict():
    assert registry.waiting(1).unfinished is True
    assert registry.waiting(1).ok is False
    assert registry.succeeded().unfinished is False
    assert registry.failed("x").unfinished is False
