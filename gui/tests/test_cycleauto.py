"""Cycles that start on a schedule: when they fall due, and what waits."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import core as core_mod, cycleauto, cyclewatch, history as history_mod
from cms_gui.core import Inventory


class Clock(object):
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def row(cycle_id, every=3600, start="auto", problems=(), enabled=True):
    trigger = {"id": "hourly", "start": start, "every": every, "enabled": enabled}
    if start != "auto":
        trigger.update(id="new_task", watch="todo", pin="task_key")
    return {"id": cycle_id, "name": cycle_id.title(), "problems": list(problems),
            "triggers": [trigger]}


@pytest.fixture
def schedule(qapp):
    clock = Clock()
    starts = cycleauto.AutoStarts(clock=clock)
    fired = []
    starts.run_due.connect(fired.append)
    yield starts, clock, fired
    starts.set_enabled(False)


def test_the_first_run_comes_a_full_every_after_it_is_seen(schedule):
    """A restart of the window must not start a run of every scheduled cycle."""
    starts, clock, fired = schedule
    starts.set_project("a", "Alpha", [row("health")])
    starts.tick()
    assert fired == []
    assert starts.seconds_to_next() == pytest.approx(3600)
    clock.now += 3599
    starts.tick()
    assert fired == []
    clock.now += 1
    starts.tick()
    assert fired == [{"project": "a", "project_name": "Alpha", "cycle": "health",
                      "trigger": "hourly", "name": "Health"}]
    starts.tick()
    assert len(fired) == 1, "counted again from the run it just started"


def test_nothing_is_started_while_it_is_off(schedule):
    starts, clock, fired = schedule
    starts.set_project("a", "Alpha", [row("health", every=60)])
    starts.set_enabled(False)
    clock.now += 600
    starts.tick()
    assert fired == [] and not starts.timer.isActive()


def test_only_auto_triggers_that_can_run_are_scheduled(schedule):
    starts, _clock, _fired = schedule
    starts.set_project("a", "Alpha", [
        row("health"), row("dev", start="ask"), row("broken", problems=["bad"]),
        row("off", enabled=False)])
    assert starts.cycles_of("a") == {"health"}


def test_every_project_keeps_its_own_schedule(schedule):
    """Reading one project again must not reset - or drop - another's."""
    starts, clock, fired = schedule
    starts.set_project("a", "Alpha", [row("health", every=600)])
    clock.now += 300
    starts.set_project("b", "Beta", [row("health", every=600)])
    starts.set_project("a", "Alpha", [row("health", every=600)])
    clock.now += 300
    starts.tick()
    assert [(one["project"], one["cycle"]) for one in fired] == [("a", "health")]
    starts.keep_projects(["a"])
    assert set(starts.triggers) == {("a", "health", "hourly")}


def test_the_status_line_names_another_project(schedule):
    starts, _clock, _fired = schedule
    starts.set_project("a", "Alpha", [row("health")])
    starts.set_project("b", "Beta", [row("ping", every=60)])
    line, detail = starts.status("a")
    assert line == "Scheduled: 2 cycles - next run 1:00"
    assert "Health: next run in 1:00:00" in detail
    assert "Ping (Beta): next run in 1:00" in detail


def test_the_listener_does_not_ask_about_an_auto_trigger(qapp):
    class Core(object):
        asked = []

        def cycle_watch(self, cycle_id, trigger_id=""):
            self.asked.append(cycle_id)
            return {"ok": True, "new": []}

    core = Core()
    listener = cyclewatch.Listener(core, clock=Clock())
    listener.set_inventory(Inventory({"cycles": [row("health"), row("dev", start="ask")]}))
    if listener.thread is not None:
        listener.thread.wait(5000)
    assert list(listener.triggers) == [("dev", "new_task")]
    listener.shutdown()


# -- running them ---------------------------------------------------------------
class Window(object):
    """What ScheduledRuns asks of the main window, answered by a test."""

    def __init__(self, tmp_path, script="print('ok')"):
        self.tmp_path = tmp_path
        self.script = script
        self.commands = []
        self.blocked = set()
        self.histories = {}

    def scheduled_command(self, entry):
        self.commands.append(entry)
        if self.script is None:
            raise core_mod.CoreError("The core was not found.")
        return [sys.executable, "-c", self.script], "python -c", str(self.tmp_path)

    def scheduled_history(self, project_id):
        folder = self.tmp_path / project_id
        folder.mkdir(exist_ok=True)
        return history_mod.History(str(folder))

    def scheduled_blocked(self, entry):
        return (entry["project"], entry["cycle"]) in self.blocked


def entry(project="a", cycle="health"):
    return {"project": project, "project_name": project.upper(), "cycle": cycle,
            "trigger": "hourly", "name": cycle.title()}


def finish(runs, qapp):
    import time
    end = time.monotonic() + 20
    while (runs.current is not None or runs.waiting) and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()


def test_a_due_run_goes_into_its_own_project_s_history(qapp, tmp_path):
    window = Window(tmp_path, "print('checked')")
    runs = cycleauto.ScheduledRuns(window)
    ended = []
    runs.finished.connect(lambda one, code: ended.append((one["project"], code)))
    assert runs.due(entry("b"))
    finish(runs, qapp)
    assert ended == [("b", 0)]
    recorded = history_mod.History(str(tmp_path / "b")).entries()
    assert len(recorded) == 1
    assert recorded[0]["status"] == history_mod.OK
    assert recorded[0]["config"] == {"cycle": "health", "trigger": "schedule:hourly"}
    with open(recorded[0]["log_file"], encoding="utf-8") as handle:
        assert "checked" in handle.read()
    assert not (tmp_path / "a").exists()


def test_a_trigger_waits_once_and_runs_one_at_a_time(qapp, tmp_path):
    window = Window(tmp_path, "import time; time.sleep(0.3)")
    runs = cycleauto.ScheduledRuns(window)
    assert runs.due(entry("a"))
    assert runs.current is not None
    assert runs.due(entry("a")), "one waits behind the one running"
    assert not runs.due(entry("a")), "but never two"
    assert runs.due(entry("b"))
    assert runs.status() == "running Health"
    finish(runs, qapp)
    assert [one["project"] for one in window.commands] == ["a", "a", "b"]


def test_a_cycle_the_window_is_running_waits_for_it(qapp, tmp_path):
    window = Window(tmp_path)
    window.blocked.add(("a", "health"))
    runs = cycleauto.ScheduledRuns(window)
    runs.due(entry("a"))
    runs.due(entry("b", "ping"))
    finish_other = [one["cycle"] for one in runs.waiting]
    assert runs.current["cycle"] == "ping" and finish_other == ["health"]
    import time
    while runs.current is not None:
        qapp.processEvents()
        time.sleep(0.01)
    assert [one["cycle"] for one in runs.waiting] == ["health"], "still running there"
    window.blocked.clear()
    runs.start_next()
    finish(runs, qapp)
    assert [one["cycle"] for one in window.commands] == ["ping", "health"]


def test_a_run_that_cannot_be_built_is_recorded_and_the_next_goes(qapp, tmp_path):
    window = Window(tmp_path, None)
    runs = cycleauto.ScheduledRuns(window)
    runs.due(entry("a"))
    qapp.processEvents()
    recorded = history_mod.History(str(tmp_path / "a")).entries()
    assert recorded[0]["status"] == history_mod.ERROR
    assert "core was not found" in recorded[0]["summary"]
    assert runs.current is None


def test_closing_stops_a_run_still_going(qapp, tmp_path):
    window = Window(tmp_path, "import time; time.sleep(30)")
    runs = cycleauto.ScheduledRuns(window)
    runs.due(entry("a"))
    runs.due(entry("b"))
    runs.shutdown(wait_ms=5000)
    assert not runs.process.is_running() and runs.waiting == []
    recorded = history_mod.History(str(tmp_path / "a")).entries()
    assert recorded[0]["status"] == history_mod.STOPPED


def test_a_project_taken_off_the_list_mid_run_is_written_nowhere(qapp, tmp_path):
    """Its history is not the one on screen's, so it goes to no history at all."""
    window = Window(tmp_path, "import time; time.sleep(0.2)")
    runs = cycleauto.ScheduledRuns(window)
    runs.due(entry("gone"))
    window.scheduled_history = lambda _project: (_ for _ in ()).throw(
        core_mod.CoreError("no longer on the list"))
    ended = []
    runs.finished.connect(lambda one, code: ended.append(code))
    finish(runs, qapp)
    assert ended == [0]
