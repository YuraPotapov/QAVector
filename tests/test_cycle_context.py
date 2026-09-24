"""The cancel token, and what a plugin can reach through its context."""

import os
import threading
import time

import pytest

from cycle.context import CancelToken, Cancelled, RunContext
from domain.cycle import Artifact, CycleRun, StepRun, SUCCESS


@pytest.fixture
def context(tmp_path):
    run = CycleRun(id="20260916-120000-demo", cycle_id="demo",
                   workspace=str(tmp_path))
    return RunContext(run, None, str(tmp_path))


# ------------------------------------------------------------------- the token
def test_a_fresh_token_is_not_set():
    assert not CancelToken().is_set()


def test_setting_a_token_sets_it():
    token = CancelToken()
    token.set()
    assert token.is_set()


def test_setting_is_idempotent():
    token = CancelToken()
    token.set("first")
    token.set("second")
    assert token.is_set()
    assert token.reason == "first"          # the first reason is the true one


def test_a_child_fires_when_its_parent_does():
    """Stopping the run stops every step."""
    parent = CancelToken()
    child = parent.child()
    assert not child.is_set()
    parent.set("the run was cancelled")
    assert child.is_set()


def test_a_child_firing_leaves_its_parent_and_its_siblings_alone():
    """One step's deadline is not every step's."""
    parent = CancelToken()
    one, other = parent.child(), parent.child()
    one.set("timed out")
    assert one.is_set()
    assert not other.is_set()
    assert not parent.is_set()


def test_a_child_inherits_its_parent_s_reason_when_it_has_none_of_its_own():
    parent = CancelToken()
    child = parent.child()
    parent.set("the user pressed Stop")
    assert child.reason == "the user pressed Stop"


def test_a_child_with_its_own_reason_keeps_it():
    parent = CancelToken()
    child = parent.child("timed out after 30s")
    parent.set("the user pressed Stop")
    assert child.reason == "timed out after 30s"


def test_waiting_returns_at_once_when_it_is_already_set():
    token = CancelToken()
    token.set()
    started = time.monotonic()
    assert token.wait(5) is True
    assert time.monotonic() - started < 0.5


def test_waiting_gives_up_when_nothing_happens():
    """What a plugin uses instead of sleep, so a delay is interruptible."""
    started = time.monotonic()
    assert CancelToken().wait(0.2) is False
    assert 0.15 < time.monotonic() - started < 1.5


def test_waiting_wakes_when_the_token_is_set_from_another_thread():
    token = CancelToken()
    threading.Timer(0.15, token.set).start()
    started = time.monotonic()
    assert token.wait(5) is True
    assert time.monotonic() - started < 2


def test_a_child_wakes_when_its_parent_is_set_from_another_thread():
    """An Event cannot wait on two things, so this is the case that could hang."""
    parent = CancelToken()
    child = parent.child()
    threading.Timer(0.15, parent.set).start()
    started = time.monotonic()
    assert child.wait(5) is True
    assert time.monotonic() - started < 2


def test_a_child_gives_up_on_time_when_nobody_sets_anything():
    started = time.monotonic()
    assert CancelToken().child().wait(0.2) is False
    assert 0.15 < time.monotonic() - started < 1.5


def test_raising_is_the_other_way_to_notice():
    """For a plugin that checks between units of work rather than waiting."""
    token = CancelToken()
    token.raise_if_set()                    # must not raise
    token.set("stopped")
    with pytest.raises(Cancelled):
        token.raise_if_set()


# ------------------------------------------------------------------ where things go
def test_a_step_gets_a_directory_of_its_own(context, tmp_path):
    path = context.step_dir("checkout")
    assert os.path.isdir(path)
    assert path == os.path.join(str(tmp_path), "steps", "checkout")


def test_a_step_directory_can_be_reached_into(context):
    assert context.step_dir("a", "reports").endswith(os.path.join("a", "reports"))
    assert os.path.isdir(context.step_dir("a", "reports"))


def test_asking_twice_for_a_step_directory_is_fine(context):
    assert context.step_dir("a") == context.step_dir("a")


def test_the_run_has_somewhere_to_put_artifacts(context):
    assert os.path.isdir(context.artifacts_dir())


def test_a_path_is_recorded_relative_to_the_workspace(context, tmp_path):
    """A run directory has to stay readable after it is zipped and moved."""
    assert context.relative(str(tmp_path / "steps" / "a" / "out.log")) == (
        os.path.join("steps", "a", "out.log"))


def test_a_path_outside_the_workspace_is_left_alone(context):
    """A string of `..` means less than the path it replaced."""
    assert context.relative("/etc/hosts") == "/etc/hosts"


# ------------------------------------------------------------------ what came before
def test_a_finished_step_s_outputs_are_reachable(context):
    context.run.steps["pg"] = StepRun("pg", status=SUCCESS,
                                      outputs={"port": 5432})
    assert context.outputs_of("pg") == {"port": 5432}
    assert context.status_of("pg") == SUCCESS


def test_a_step_that_has_not_run_has_no_outputs_and_no_status(context):
    assert context.outputs_of("ghost") == {}
    assert context.status_of("ghost") == ""


def test_the_outputs_handed_out_are_a_copy(context):
    """A plugin must not be able to rewrite another step's record."""
    context.run.steps["pg"] = StepRun("pg", outputs={"port": 5432})
    context.outputs_of("pg")["port"] = 1
    assert context.run.steps["pg"].outputs == {"port": 5432}


# -------------------------------------------------------------------- saying things
def test_output_reaches_the_observer(context):
    seen = []

    class Watcher:
        def step_log(self, step_id, stream, lines):
            seen.append((step_id, stream, lines))

    context.observer = Watcher()
    context.log("a", "out", ["one", "two"])
    context.log("a", "out", "just one")
    assert seen == [("a", "out", ["one", "two"]), ("a", "out", ["just one"])]


def test_an_observer_that_breaks_does_not_break_the_step(context):
    """Observers are diagnostics; one must never be the reason a run fails."""
    class Broken:
        def step_log(self, *args):
            raise RuntimeError("no")

    context.observer = Broken()
    context.log("a", "out", ["line"])       # must not raise


def test_saying_nothing_reaches_nobody(context):
    seen = []

    class Watcher:
        def step_log(self, step_id, stream, lines):
            seen.append(lines)

    context.observer = Watcher()
    context.log("a", "out", [])
    assert seen == []


def test_output_with_no_observer_is_simply_dropped(context):
    context.log("a", "out", ["line"])       # must not raise


# ------------------------------------------------------------ holding things open
def test_what_is_held_comes_back_in_the_order_it_should_be_released(context):
    """A service started after the database it needs is stopped before it."""
    context.hold("pg", "plugin-a", {"h": 1})
    context.hold("app", "plugin-b", {"h": 2})
    assert [entry[0] for entry in context.held()] == ["app", "pg"]


def test_a_step_can_give_back_what_it_was_holding(context):
    """For service.stop after service.start: the cleanup pass must not try again."""
    context.hold("pg", "plugin", {"h": 1})
    context.release("pg")
    assert context.held() == []


def test_releasing_something_that_is_not_held_is_harmless(context):
    context.release("never-held")


def test_holding_is_safe_from_several_threads(context):
    """Independent branches of a DAG start services at the same time."""
    def hold(index):
        context.hold("step-%d" % index, "plugin", {"h": index})

    threads = [threading.Thread(target=hold, args=(index,)) for index in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(context.held()) == 40
