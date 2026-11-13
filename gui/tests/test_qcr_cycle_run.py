"""Cycle runs asked through QCR (ADR-003 3.2): the main window's side.

The window's own methods on a stand-in window, as for scenarios: the Cycles
page's command line, the same busy rule, the run id and each step followed,
the run's end with every step's status, a run stopped by QCR.
"""

import functools
import types

from cms_gui.main_window import MainWindow
from test_qcr_scenario_run import INVENTORY, Recorder, owner_events, window as scenario_window

CYCLES = [{"id": "nightly", "name": "Nightly", "steps": 2, "problems": []},
          {"id": "bad/id", "name": "Unnameable"}]


def window(blocked=False):
    fake, host, process = scenario_window()
    fake.inventory = types.SimpleNamespace(scenarios=INVENTORY.scenarios, envs=INVENTORY.envs, users=INVENTORY.users,
                                           payload={"cycles": CYCLES})
    fake.core.secrets_flag = lambda: ["--cycle-secrets-file=/tmp/s.json"]
    fake.core.memory_flag = lambda: []
    fake.core.is_configured = lambda: True
    fake.settings.project_folder = ""
    fake.run_state.reset_cycle = lambda: None
    fake.run_state.cycle = None
    fake._cycle_blocked = lambda: blocked
    fake._release_launcher = lambda: None
    for name in ("_qcr_cycle_execute", "_cycle_run_args", "_begin_cycle_run", "_qcr_label"):
        setattr(fake, name, functools.partial(getattr(MainWindow, name), fake))
    fake.services = types.SimpleNamespace(label_of=lambda resource_id: "")
    return fake, host, process


def test_cycles_go_into_the_inventory_and_are_named_in_the_inspector(qapp):
    fake, _host, _process = window()
    assert fake._qcr_inventory()["cycles"] == [{"id": "nightly", "name": "Nightly", "description": None,
                                                 "steps": 2, "problems": []}]
    assert fake._qcr_label("nightly") == "Cycle · Nightly"


def test_a_busy_launcher_fails_the_cycle_at_once(qapp):
    fake, host, process = window(blocked=True)
    fake._qcr_cycle_execute("e1", "nightly", {})
    assert owner_events(host)[0][1]["error"]["code"] == "launcher_busy"
    assert not [c for c in process.calls if c[0] == "start"]


def test_a_cycle_run_is_the_cycles_page_s_command_line_and_its_steps_are_followed(qapp):
    fake, host, process = window()
    fake._qcr_cycle_execute("e1", "nightly", {"variables": {"branch": "main"}, "execution_mode": "important"})
    argv = [c for c in process.calls if c[0] == "start"][0][1][0]
    assert argv[2:] == fake._cycle_run_args("nightly", variables={"branch": "main"}, execution_mode="important")
    for part in ("--cycle-run=nightly", "--cycle-var=branch=main", "--cycle-mode=important", "--events=-"):
        assert part in argv
    fake.run_state.cycle = {"run_id": "r-1", "status": "failed",
                            "steps": {"fetch": {"status": "succeeded"}, "test": {"status": "failed"}}}
    for event in ({"kind": "cycle.run.start", "run_id": "r-1"},
                  {"kind": "cycle.step.end", "step": "fetch", "status": "succeeded"},
                  {"kind": "run.finished", "run": "r-1", "status": "failed", "exit_code": 1}):
        fake._qcr_run_event(event)
    events = owner_events(host)
    assert events[0][0] == "accepted"
    assert events[1] == ("progress", {"kind": "run", "payload": {"run_id": "r-1"}})
    assert events[2] == ("progress", {"kind": "step", "payload": {"id": "fetch", "status": "succeeded"}})
    state, fields = events[-1]
    assert state == "succeeded" and fields["output"] == {
        "run_id": "r-1", "status": "failed", "exit_code": 1, "history_entry": "h-1",
        "steps": [{"id": "fetch", "status": "succeeded"}, {"id": "test", "status": "failed"}]}


def test_a_cycle_stopped_by_qcr_reports_nothing_more(qapp):
    fake, host, process = window()
    fake._qcr_cycle_execute("e1", "nightly", {})
    process.is_running = lambda: True
    fake._qcr_run_stop()
    assert ("stop", (), {}) in process.calls
    fake._qcr_run_event({"kind": "run.finished", "status": "stopped", "exit_code": 130})
    assert [state for state, _ in owner_events(host)] == ["accepted"]


def test_a_launcher_that_ends_early_fails_the_cycle_run(qapp):
    fake, host, _process = window()
    fake._qcr_cycle_execute("e1", "nightly", {})
    fake._qcr_run_end(-9, ended_early=True)
    state, fields = owner_events(host)[-1]
    assert state == "failed" and fields["error"]["code"] == "run_ended"
