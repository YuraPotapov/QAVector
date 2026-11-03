"""Pause holds a step's processes where they stand, and Continue lets them go on."""

import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from cycle import executor, freeze, registry, restrict
from test_cycle_phase_controls import Fake, definition, live, node

pytestmark = pytest.mark.skipif(not freeze.SUPPORTED, reason="holding reads /proc")

RUN_ID = "phase-controls"

#: Starts a child that does the counting, so holding the step must reach a
#: process it did not start itself.
TICKER = r"""
import subprocess, sys, time
if sys.argv[1] == "child":
    path = sys.argv[2]
    n = 0
    while n < int(sys.argv[3]):
        n += 1
        with open(path, "w") as handle:
            handle.write(str(n))
        time.sleep(0.02)
else:
    sys.exit(subprocess.call([sys.executable, __file__, "child"] + sys.argv[1:]))
"""


def _state(pid):
    try:
        with open("/proc/%d/stat" % pid) as handle:
            raw = handle.read()
    except OSError:
        return ""
    return raw[raw.rfind(")") + 2:].split()[0]


def _count(path):
    try:
        return int(path.read_text() or 0)
    except (OSError, ValueError):
        return 0


def _until(done, seconds=5):
    deadline = time.monotonic() + seconds
    while not done() and time.monotonic() < deadline:
        time.sleep(0.02)
    return done()


def _ticker(tmp_path, ticks=100000):
    script = tmp_path / "ticker.py"
    script.write_text(TICKER, encoding="utf-8")
    return [sys.executable, str(script), str(tmp_path / "count"), str(ticks)]


def test_holding_stops_the_whole_tree_and_releasing_lets_it_go_on(tmp_path):
    steps = freeze.Steps()
    previous = freeze.enter(steps, "work")
    try:
        process = restrict.popen(_ticker(tmp_path), start_new_session=True)
    finally:
        freeze.leave(previous)
    counter = tmp_path / "count"
    try:
        assert _until(lambda: _count(counter) > 3)
        assert steps.hold() == ["work"]
        assert _state(process.pid) == "T"
        stopped = _count(counter)
        time.sleep(0.3)
        assert _count(counter) == stopped          # the child is held too

        steps.release()
        assert _until(lambda: _count(counter) > stopped)
        assert _state(process.pid) in ("S", "R")
    finally:
        steps.release()
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(5)


def test_a_process_started_while_held_is_held_at_once(tmp_path):
    steps = freeze.Steps()
    assert steps.hold() == []
    previous = freeze.enter(steps, "late")
    try:
        process = restrict.popen(_ticker(tmp_path), start_new_session=True)
    finally:
        freeze.leave(previous)
    try:
        assert _state(process.pid) == "T"
    finally:
        steps.release()
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(5)


def test_every_process_started_by_one_step_is_held():
    steps = freeze.Steps()
    processes = []
    try:
        for _ in range(2):
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            processes.append(process)
            steps.add("work", process.pid)
        assert steps.hold() == ["work"]
        assert _until(lambda: all(_state(p.pid) == "T" for p in processes))
        steps.release()
        assert _until(lambda: all(_state(p.pid) in ("R", "S") for p in processes))
    finally:
        steps.release()
        for process in processes:
            process.kill()
            process.wait(5)


def test_outside_a_step_nothing_is_tracked(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    assert freeze.track(process) is process
    process.wait(5)


def test_a_finished_step_is_not_held(tmp_path):
    steps = freeze.Steps()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        steps.add("done", process.pid)
        steps.forget("done")
        assert steps.hold() == []
        assert _state(process.pid) != "T"
    finally:
        steps.release()
        process.kill()
        process.wait(5)


class Ticking(registry.CyclePlugin):
    """A step whose work is a process, the way command.shell runs one."""

    def __init__(self, argv):
        self.metadata = registry.PluginMetadata(id="test.ticking", name="ticking")
        self.argv = argv
        self.pids = []

    def execute(self, context, step):
        process = restrict.popen(self.argv, start_new_session=True)
        self.pids.append(process.pid)
        while process.poll() is None:
            if context.cancel.is_set():
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(5)
                return registry.failed("stopped")
            time.sleep(0.02)
        return registry.succeeded() if process.returncode == 0 else registry.failed("exit %s" % process.returncode)


def test_pause_holds_the_step_in_flight_and_continue_finishes_it(tmp_path):
    plugin = Ticking(_ticker(tmp_path, ticks=60))
    counter = tmp_path / "count"
    cycle = definition([node("work", "test.ticking", timeout=60), node("after", needs=["work"])])
    with live(tmp_path, cycle, plugin, Fake()) as (events, result):
        assert _until(lambda: _count(counter) > 3)
        assert not executor.control_run(RUN_ID, "pause")
        state = events.wait("cycle.run.state", lambda e: e["action"] == "paused")
        # Paused while the step is still in flight - not when it is done.
        assert state["held_steps"] == ["work"]
        assert _state(plugin.pids[0]) == "T"
        stopped = _count(counter)
        time.sleep(0.3)
        assert _count(counter) == stopped

        assert not executor.control_run(RUN_ID, "continue")
        events.wait("cycle.run.end")
    run = result["run"]
    assert run.ok and run.held_steps == []
    assert run.steps["work"].status == run.steps["after"].status == "success"
    assert _count(counter) == 60
    titles = [e["title"] for e in events.events if e["kind"] == "cycle.step.stage"]
    assert titles == ["Paused here", "Continued"]


def test_stopping_a_paused_run_reaches_the_held_step(tmp_path):
    plugin = Ticking(_ticker(tmp_path))
    counter = tmp_path / "count"
    with live(tmp_path, definition([node("work", "test.ticking")]), plugin) as (events, result):
        assert _until(lambda: _count(counter) > 3)
        assert not executor.control_run(RUN_ID, "pause")
        events.wait("cycle.run.state", lambda e: e["action"] == "paused")
        started = time.monotonic()
    # live() cancels the run on the way out and requires it to end in time.
    assert time.monotonic() - started < 5
    assert result["run"].steps["work"].status in ("failed", "cancelled")
    assert _until(lambda: _state(plugin.pids[0]) in ("", "Z"))


def test_pause_longer_than_timeout_preserves_the_active_steps_budget(tmp_path):
    plugin = Ticking(_ticker(tmp_path, ticks=50))
    counter = tmp_path / "count"
    cycle = definition([node("work", "test.ticking", timeout=2)])
    with live(tmp_path, cycle, plugin) as (events, result):
        assert _until(lambda: _count(counter) > 3)
        assert not executor.control_run(RUN_ID, "pause")
        events.wait("cycle.run.state", lambda e: e["action"] == "paused")
        time.sleep(2.2)
        assert not executor.control_run(RUN_ID, "continue")
        events.wait("cycle.run.end")
    assert result["run"].ok
    assert _count(counter) == 50


def test_process_started_during_pause_updates_the_visible_held_steps(tmp_path):
    ready, launch = threading.Event(), threading.Event()

    class LateTicking(Ticking):
        def execute(self, context, step):
            ready.set()
            while not launch.wait(0.02):
                if context.cancel.is_set():
                    return registry.failed("stopped")
            return super().execute(context, step)

    plugin = LateTicking(_ticker(tmp_path, ticks=10))
    with live(tmp_path, definition([node("work", "test.ticking")]), plugin) as (events, result):
        assert ready.wait(5)
        assert not executor.control_run(RUN_ID, "pause")
        state = events.wait("cycle.run.state", lambda e: e["action"] == "paused")
        assert state["held_steps"] == []
        launch.set()
        events.wait("cycle.run.state", lambda e: e["action"] == "paused" and e["held_steps"] == ["work"])
        assert _state(plugin.pids[0]) == "T"
        assert not executor.control_run(RUN_ID, "continue")
        events.wait("cycle.run.end")
    assert result["run"].ok
