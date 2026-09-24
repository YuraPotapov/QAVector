"""Holding a branch of the graph until a time.

The scheduler's half of this is tested in ``test_cycle_waiting.py``. What is
left for here is the arithmetic, and the one property that is easy to get
wrong: the plugin keeps **no state between turns**, because each turn is a
fresh call. So "how long have I been waiting" has to be answered from the run
record every time, and a step woken early has to work out what is left rather
than starting its wait again.

The tests drive ``execute`` directly with a run record they control, which is
exactly what the executor hands it - and lets a wait of an hour be checked in
no time at all.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import registry                                         # noqa: E402
from cycle.context import CancelToken, RunContext                  # noqa: E402
from cycle.plugins.wait import _at, _rough                         # noqa: E402
from cycle.workspace import create                                 # noqa: E402
from domain.cycle import CycleRun, CycleStep, StepRun, WAITING      # noqa: E402


@pytest.fixture
def wait(tmp_path):
    """Run ``time.wait`` once, as though the step began ``ago`` seconds back."""
    def go(ago=0.0, **settings):
        workspace = create("20260921-000000-p", str(tmp_path / "runs"))
        run = CycleRun(id="r", cycle_id="c", workspace=workspace,
                       steps={"w": StepRun("w", plugin="time.wait",
                                           started_at=time.time() - ago)})
        context = RunContext(run, None, workspace, cancel=CancelToken())
        step = CycleStep(id="w", plugin="time.wait", settings=dict(settings))
        return registry.get("time.wait").execute(context, step)
    return go


# ---------------------------------------------------------------- a duration
def test_it_asks_to_wait_when_the_time_is_not_up(wait):
    result = wait(seconds=60)

    assert result.unfinished is True
    assert result.status == WAITING
    assert 55 < result.resume_at - time.monotonic() <= 60


def test_it_succeeds_once_the_time_is_up(wait):
    result = wait(ago=61, seconds=60)

    assert result.ok
    assert result.outputs["waited_seconds"] >= 60


def test_the_wait_is_measured_from_the_first_start_not_from_this_turn(wait):
    """The property that makes it stateless. Measured from now, a step woken
    four times would wait its full minute four times over."""
    result = wait(ago=45, seconds=60)

    assert result.unfinished
    assert 10 < result.resume_at - time.monotonic() <= 15, "15s left, not 60"


def test_being_woken_early_asks_again_for_what_is_left(wait):
    first = wait(ago=0, seconds=10)
    later = wait(ago=9, seconds=10)

    assert first.unfinished and later.unfinished
    assert (later.resume_at - time.monotonic()) < (first.resume_at
                                                   - time.monotonic())


def test_zero_seconds_finishes_rather_than_waiting(wait):
    assert wait(seconds=0).ok


# -------------------------------------------------------------- a clock time
def test_it_waits_until_a_time_of_day(wait):
    soon = time.strftime("%H:%M", time.localtime(time.time() + 3600))
    result = wait(until=soon)

    assert result.unfinished
    assert 3000 < result.resume_at - time.monotonic() <= 3660


def test_a_time_that_has_gone_by_today_means_tomorrow(wait):
    gone = time.strftime("%H:%M", time.localtime(time.time() - 7200))
    result = wait(until=gone)

    assert result.unfinished
    # Roughly a day less the two hours, with room for the minute's rounding.
    assert 79000 < result.resume_at - time.monotonic() < 80000


def test_a_full_date_and_time_is_taken_as_written(wait):
    result = wait(until="2099-01-01T09:00")

    assert result.unfinished
    assert result.outputs["until"].startswith("2099-01-01 09:00")


def test_a_date_in_the_past_finishes_at_once(wait):
    assert wait(until="2000-01-01T09:00").ok


def test_a_time_wins_over_a_duration_when_both_are_written(wait):
    """The more specific of the two, and refusing the pair would fail a cycle
    over something with an obvious reading."""
    result = wait(until="2099-01-01T09:00", seconds=1)

    assert result.unfinished, "the duration would have finished by now"


def test_the_moment_it_is_waiting_for_is_an_output(wait):
    assert wait(seconds=60).outputs["until"]


# ------------------------------------------------------------- what it says
def test_it_says_what_it_is_waiting_for(wait):
    result = wait(seconds=600, reason="letting the deploy settle")

    assert "letting the deploy settle" in result.message


def test_a_wait_with_nothing_to_say_still_says_how_long(wait):
    assert "10m" in wait(seconds=600).message


# ---------------------------------------------------------------- refusals
def test_neither_a_duration_nor_a_time_is_refused(wait):
    """Not a wait of zero - a step nobody finished writing."""
    problems = registry.get("time.wait").problems({})
    assert problems and "seconds" in problems[0]


def test_a_time_that_is_not_a_time_is_refused_before_the_run():
    problems = registry.get("time.wait").problems({"until": "tea time"})
    assert problems and "tea time" in problems[0]


def test_a_negative_duration_is_refused():
    assert registry.get("time.wait").problems({"seconds": -5}) != []


def test_a_reference_is_left_for_the_run_to_resolve():
    assert registry.get("time.wait").problems(
        {"until": "${vars.when}", "reason": "${steps.a.outputs.why}"}) == []


def test_an_unknown_setting_is_refused():
    assert registry.get("time.wait").problems({"seconds": 1, "nope": 1}) != []


# ------------------------------------------------------------- the helpers
def test_a_time_only_is_read_against_the_step_s_own_start():
    """Not against now, or a step woken a moment early would decide the time
    it was waiting for is tomorrow's."""
    began = time.mktime(time.strptime("2026-09-21 08:00:00",
                                      "%Y-%m-%d %H:%M:%S"))
    assert _at("09:00", began) == began + 3600


def test_a_time_earlier_than_the_start_rolls_to_the_next_day():
    began = time.mktime(time.strptime("2026-09-21 10:00:00",
                                      "%Y-%m-%d %H:%M:%S"))
    assert _at("09:00", began) == began + 23 * 3600


def test_durations_are_spoken_at_the_precision_anybody_cares_about():
    assert _rough(45) == "45s"
    assert _rough(600) == "10m"
    assert _rough(7200) == "2.0h"


# --------------------------------------------------------------- the shape
def test_it_declares_no_permissions():
    """It reaches nothing. Waiting is the whole of what it does."""
    assert registry.get("time.wait").metadata.permissions == ()


def test_its_metadata_survives_the_wire():
    import json
    json.dumps(registry.get("time.wait").metadata.to_dict())
