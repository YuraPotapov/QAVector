"""The task listener: when it asks, what it keeps, and what it never does."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import core as core_mod, cyclewatch
from cms_gui.core import Inventory


class Clock(object):
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeCore(object):
    """Answers --cycle-watch from a list the test fills in."""

    def __init__(self):
        self.answers = {}
        self.asked = []
        self.seen = []

    def cycle_watch(self, cycle_id, trigger_id=""):
        self.asked.append((cycle_id, trigger_id))
        answer = self.answers.get(cycle_id, {"ok": True, "new": []})
        if isinstance(answer, Exception):
            raise answer
        return answer

    def cycle_watch_seen(self, cycle_id, key, trigger_id=""):
        self.seen.append((cycle_id, key, trigger_id))
        return {"ok": True}


def inventory(*rows):
    return Inventory({"cycles": list(rows)})


def row(cycle_id, every=300, problems=()):
    return {"id": cycle_id, "name": cycle_id.title(), "problems": list(problems),
            "triggers": [{"id": "new_task", "watch": "todo", "every": every,
                          "pin": "task_key"}]}


@pytest.fixture
def listening(qapp):
    core, clock = FakeCore(), Clock()
    listener = cyclewatch.Listener(core, clock=clock)
    yield listener, core, clock
    listener.shutdown()


def settle(listener):
    if listener.thread is not None:
        listener.thread.wait(5000)
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()


def test_a_trigger_is_checked_as_soon_as_it_is_known(listening):
    listener, core, _clock = listening
    listener.set_inventory(inventory(row("dev")))
    settle(listener)
    assert core.asked == [("dev", "new_task")]


def test_the_next_check_waits_for_the_trigger_s_own_every(listening):
    """How often is the cycle file's decision: 600 there means 600 here."""
    listener, core, clock = listening
    listener.set_inventory(inventory(row("dev", every=600)))
    settle(listener)
    assert listener.seconds_to_next() == pytest.approx(600)
    assert listener.timer.isActive()
    assert abs(listener.timer.remainingTime() - 600 * 1000) < 2000

    clock.now += 599
    listener.tick()
    settle(listener)
    assert len(core.asked) == 1, "not due yet"
    clock.now += 1
    listener.tick()
    settle(listener)
    assert len(core.asked) == 2


def test_each_trigger_keeps_its_own_schedule(listening):
    listener, core, clock = listening
    listener.set_inventory(inventory(row("fast", every=60), row("slow", every=900)))
    settle(listener)
    clock.now += 60
    listener.tick()
    settle(listener)
    assert [one[0] for one in core.asked] == ["fast", "slow", "fast"]
    assert listener.seconds_to_next() == pytest.approx(60)


def test_a_new_task_becomes_an_offer_carrying_how_to_run_it(listening):
    listener, core, _clock = listening
    core.answers["dev"] = {"ok": True, "pin": "task_key", "new": [
        {"key": "QA-6", "title": "Free returns", "url": "https://x.invalid/QA-6"}]}
    offered = []
    listener.offers_changed.connect(lambda: offered.append(True))
    listener.set_inventory(inventory(row("dev")))
    settle(listener)

    assert offered
    offer = listener.next_offer()
    assert (offer["cycle"], offer["key"], offer["pin"], offer["title"]) == (
        "dev", "QA-6", "task_key", "Free returns")
    assert listener.next_offer() is None


def test_one_task_is_offered_once_while_it_waits(listening):
    listener, core, clock = listening
    core.answers["dev"] = {"ok": True, "new": [{"key": "QA-6", "title": "t"}]}
    listener.set_inventory(inventory(row("dev", every=60)))
    settle(listener)
    clock.now += 60
    listener.tick()
    settle(listener)
    assert len(listener.queue) == 1


def test_ignore_and_start_mark_it_seen_and_not_now_does_not(listening):
    listener, core, _clock = listening
    offer = {"cycle": "dev", "trigger": "new_task", "key": "QA-6"}
    listener.seen(offer)
    assert core.seen == [("dev", "QA-6", "new_task")]


def test_a_failing_check_is_kept_quietly_not_offered(listening):
    listener, core, _clock = listening
    core.answers["dev"] = {"ok": False, "problems": ["Cannot reach Jira"]}
    listener.set_inventory(inventory(row("dev")))
    settle(listener)
    assert listener.problems[("dev", "new_task")] == "Cannot reach Jira"
    assert listener.queue == []

    core.answers["dev"] = core_mod.CoreError("core crashed")
    listener.last.clear()
    listener.tick()
    settle(listener)
    assert "core crashed" in listener.problems[("dev", "new_task")]


def test_a_cycle_with_problems_is_not_listened_for(listening):
    listener, core, _clock = listening
    listener.set_inventory(inventory(row("dev", problems=["broken"])))
    settle(listener)
    assert core.asked == [] and not listener.timer.isActive()


def test_turned_off_it_asks_nothing(listening):
    listener, core, _clock = listening
    listener.set_enabled(False)
    listener.set_inventory(inventory(row("dev")))
    settle(listener)
    assert core.asked == [] and not listener.timer.isActive()
    listener.set_enabled(True)
    settle(listener)
    assert core.asked == [("dev", "new_task")]


def test_offers_for_a_trigger_that_is_gone_are_dropped(listening):
    listener, core, _clock = listening
    core.answers["dev"] = {"ok": True, "new": [{"key": "QA-6", "title": "t"}]}
    listener.set_inventory(inventory(row("dev")))
    settle(listener)
    assert listener.queue
    listener.set_inventory(inventory())
    assert listener.queue == [] and not listener.timer.isActive()


def test_the_schedule_has_no_interval_of_its_own():
    """Every wait comes from a trigger's every; the one constant left is how
    long closing the window waits for a check already under way."""
    assert not hasattr(cyclewatch, "TICK_MS")
    assert cyclewatch.CLOSE_WAIT_MS > 0


def test_a_trigger_turned_off_in_the_step_s_settings_is_not_listened_for(listening):
    listener, core, _clock = listening
    off = row("dev")
    off["triggers"][0]["enabled"] = False
    listener.set_inventory(inventory(off))
    settle(listener)
    assert core.asked == [] and not listener.timer.isActive()


# ----------------------------------------------------------- the status line
def test_nothing_is_said_when_nothing_is_listened_for(listening):
    listener, _core, _clock = listening
    assert listener.status() == ("", "", False)


def test_the_status_says_how_many_and_when_next(listening):
    listener, _core, clock = listening
    listener.set_inventory(inventory(row("dev", every=120)))
    settle(listener)
    clock.now += 18
    line, detail, trouble = listener.status()
    assert line == "Listening: 1 cycle - next check 1:42"
    assert "Dev: next check in 1:42" in detail and not trouble


def test_a_failing_check_is_what_the_status_says(listening):
    listener, core, _clock = listening
    core.answers["dev"] = {"ok": False, "problems": ["Cannot reach http://127.0.0.1:8765"]}
    listener.set_inventory(inventory(row("dev")))
    settle(listener)
    line, detail, trouble = listener.status()
    assert trouble and line.startswith("Listening: Dev - Cannot reach")
    assert "last check failed" in detail


def test_a_task_being_asked_about_is_not_queued_again(listening):
    """A check that lands while its window is open used to queue the same task
    again, so answering it - even Ignore - brought the same window back."""
    listener, core, clock = listening
    core.answers["dev"] = {"ok": True, "new": [{"key": "QA-6", "title": "t"}]}
    listener.set_inventory(inventory(row("dev", every=60)))
    settle(listener)
    offer = listener.next_offer()
    clock.now += 60
    listener.tick()
    settle(listener)
    assert listener.queue == [], "the one on screen is not queued twice"
    listener.answered(offer)
    assert listener.asking is None and listener.next_offer() is None


def test_answering_drops_any_copy_still_waiting(listening):
    listener, _core, _clock = listening
    listener.triggers = {("dev", "t"): {"every": 60, "pin": "k", "name": "Dev"}}
    listener.queue = [{"cycle": "dev", "trigger": "t", "key": "QA-6"},
                      {"cycle": "dev", "trigger": "t", "key": "QA-7"},
                      {"cycle": "dev", "trigger": "t", "key": "QA-6"}]
    offer = listener.next_offer()
    listener.answered(offer)
    assert [one["key"] for one in listener.queue] == ["QA-7"]


def whole(*keys):
    return {"key": ",".join(keys), "title": "%d tasks" % len(keys), "whole": True,
            "items": [{"key": one, "title": "Task " + one} for one in keys]}


def test_a_whole_answer_replaces_the_one_still_waiting(listening):
    """A step that does not read the pin is offered whole: the latest answer is
    what a run would take, so it is one offer, never one per check."""
    listener, core, clock = listening
    core.answers["rel"] = {"ok": True, "new": [whole("QA-1")]}
    listener.set_inventory(inventory(row("rel", every=60)))
    settle(listener)
    core.answers["rel"] = {"ok": True, "new": [whole("QA-1", "QA-2")]}
    clock.now += 60
    listener.tick()
    settle(listener)

    [offer] = listener.queue
    assert offer["key"] == "QA-1,QA-2" and offer["whole"] is True
    assert [one["key"] for one in offer["items"]] == ["QA-1", "QA-2"]
    assert offer["pin"] == "task_key"


def test_a_whole_answer_is_not_queued_while_one_is_asked(listening):
    listener, core, clock = listening
    core.answers["rel"] = {"ok": True, "new": [whole("QA-1")]}
    listener.set_inventory(inventory(row("rel", every=60)))
    settle(listener)
    listener.next_offer()
    core.answers["rel"] = {"ok": True, "new": [whole("QA-1", "QA-2")]}
    clock.now += 60
    listener.tick()
    settle(listener)
    assert listener.queue == [], "asked again at the next check if not answered"


def test_planning_a_whole_answer_keeps_its_tasks(listening):
    listener, core, _clock = listening
    kept = []
    core.cycle_watch_plan = lambda *args, **kw: kept.append((args, kw))
    offer = dict(whole("QA-1", "QA-2"), cycle="rel", trigger="t", name="Rel", pin="k",
                 url="")
    assert listener.plan(offer)
    (args, kw), = kept
    assert args[:3] == ("rel", "QA-1,QA-2", "t")
    assert [one["key"] for one in kw["items"]] == ["QA-1", "QA-2"]


def test_the_question_about_a_whole_answer_lists_every_task(qapp, monkeypatch):
    from cms_gui import widgets
    made = {}

    class Seen(widgets.Message):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            made.update(args=args, kw=kw)

        def exec(self):
            return 0

    monkeypatch.setattr(widgets, "Message", Seen)
    offer = dict(whole("QA-1", "QA-2"), cycle="rel", trigger="t",
                 name="Ready To Release", pin="k", url="")
    cyclewatch.ask_about(None, offer)
    assert made["args"][2] == "Start Ready To Release on 2 tasks?"
    assert made["kw"]["output"] == "QA-1  Task QA-1\nQA-2  Task QA-2"
