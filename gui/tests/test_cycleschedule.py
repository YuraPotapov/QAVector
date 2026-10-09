"""Starting a planned entry later: the window that asks when, and the timer."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QTime                                 # noqa: E402

from cms_gui import cycleschedule as cs                           # noqa: E402

DAY = 24 * 3600
#: A Tuesday morning, local time, so "today at 18:00" is still to come.
NOW = time.mktime((2026, 10, 6, 9, 30, 0, 0, 0, -1))


class Clock(object):
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def scheduler(qapp):
    clock = Clock()
    made = cs.PlanScheduler(clock=clock)
    said = []
    made.due.connect(lambda item, missed: said.append((item["id"], missed)))
    yield made, clock, said
    made.timer.stop()


def entry(name, run_at):
    return {"id": "task:dev:t:" + name, "kind": "task", "key": name, "run_at": run_at}


# ----------------------------------------------------------------- the timer
def test_nothing_is_due_before_its_time(scheduler):
    made, _clock, said = scheduler
    made.set_planned([entry("QA-1", NOW + 600)])
    assert said == [] and made.timer.isActive()
    assert made.timer.remainingTime() <= 600 * 1000 + 100


def test_on_time_it_is_due_and_not_missed(scheduler):
    made, clock, said = scheduler
    made.set_planned([entry("QA-1", NOW + 600)])
    clock.now = NOW + 601
    made.tick()
    assert said == [("task:dev:t:QA-1", False)]


def test_each_time_is_said_once(scheduler):
    made, clock, said = scheduler
    made.set_planned([entry("QA-1", NOW + 600)])
    clock.now = NOW + 601
    made.tick()
    made.set_planned([entry("QA-1", NOW + 600)])
    assert len(said) == 1
    made.set_planned([entry("QA-1", NOW + 900)])
    clock.now = NOW + 901
    made.tick()
    assert len(said) == 2, "a new time for the same entry is a new one"


def test_a_time_that_passed_while_the_application_was_closed_is_missed(scheduler):
    made, _clock, said = scheduler
    made.set_planned([entry("QA-1", NOW - 3600)])
    assert said == [("task:dev:t:QA-1", True)]


def test_a_timer_that_fired_long_after_its_time_is_missed(scheduler):
    """The machine was asleep: nobody is watching for that start."""
    made, clock, said = scheduler
    made.set_planned([entry("QA-1", NOW + 60)])
    clock.now = NOW + 60 + cs.LATE + 1
    made.tick()
    assert said == [("task:dev:t:QA-1", True)]


def test_anything_further_back_than_three_days_is_never_started(scheduler):
    """Opened a month later, the application does not start last month's work."""
    made, _clock, said = scheduler
    made.set_planned([entry("QA-1", NOW - 3 * DAY - 1), entry("QA-2", NOW - 30 * DAY)])
    assert said == [] and not made.timer.isActive()


def test_entries_without_a_time_are_left_alone(scheduler):
    made, _clock, said = scheduler
    made.set_planned([{"id": "task:dev:t:QA-1", "key": "QA-1"}])
    assert said == [] and not made.timer.isActive()


def test_a_long_wait_is_measured_again_within_the_hour(scheduler):
    made, _clock, _said = scheduler
    made.set_planned([entry("QA-1", NOW + 2 * DAY)])
    assert made.timer.remainingTime() <= cs.LONGEST_WAIT * 1000 + 100


# ------------------------------------------------------- choosing when
def test_the_default_is_this_evening_or_an_hour_on():
    assert time.localtime(cs.default_start(NOW)).tm_hour == cs.DEFAULT_HOUR
    late = time.mktime((2026, 10, 6, 19, 2, 0, 0, 0, -1))
    picked = cs.default_start(late)
    assert 3600 <= picked - late <= 3600 + 300 and picked % 300 == 0


def test_the_days_offered_are_today_and_the_three_after():
    offered = cs.days(NOW)
    assert [label.split(",")[0] for label, _ in offered][:2] == ["Today", "Tomorrow"]
    assert len(offered) == 4
    assert offered[3][1] - offered[0][1] == pytest.approx(3 * DAY, abs=3600)


def test_the_window_refuses_a_time_past_the_horizon_or_gone(qapp):
    dialog = cs.WhenDialog(None, {"id": "x"}, now=NOW, title="QA-1")
    try:
        assert dialog.agree_button.isEnabled(), "this evening is fine"
        assert dialog.run_at() == time.mktime((2026, 10, 6, 18, 0, 0, 0, 0, -1))
        dialog.day.setCurrentIndex(3)
        dialog.at.setTime(QTime.fromString("23:59", "HH:mm"))
        assert not dialog.agree_button.isEnabled()
        assert "At most 3 days" in dialog.problem.text()
        dialog.day.setCurrentIndex(0)
        dialog.at.setTime(QTime.fromString("08:00", "HH:mm"))
        assert not dialog.agree_button.isEnabled()
        assert "already passed" in dialog.problem.text()
    finally:
        dialog.deleteLater()


def test_a_scheduled_entry_can_have_its_time_taken_off(qapp):
    item = {"id": "x", "run_at": NOW + 3600}
    dialog = cs.WhenDialog(None, item, now=NOW, title="QA-1")
    try:
        labels = [one.text() for one in dialog.choice_buttons]
        assert labels == [cs.CLEAR, cs.START_NOW]
        assert dialog.run_at() == NOW + 3600 - (NOW + 3600) % 60
    finally:
        dialog.deleteLater()


def test_an_unscheduled_entry_offers_start_now_and_schedule(qapp):
    dialog = cs.WhenDialog(None, {"id": "x"}, now=NOW, title="QA-1")
    try:
        assert [one.text() for one in dialog.choice_buttons] == [cs.START_NOW]
        assert dialog.agree_button.text() == cs.SCHEDULE
        assert dialog.message.text() == "Start QA-1?"
    finally:
        dialog.deleteLater()


def test_ask_when_says_what_was_chosen(qapp, monkeypatch):
    class Picked(cs.WhenDialog):
        def exec(self):
            self.chosen = cs.SCHEDULE
            return 1

    monkeypatch.setattr(cs, "WhenDialog", Picked)
    answer, run_at = cs.ask_when(None, {"id": "x"}, now=NOW)
    assert answer == cs.SCHEDULE
    assert run_at == time.mktime((2026, 10, 6, 18, 0, 0, 0, 0, -1))
