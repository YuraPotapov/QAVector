"""Scenario runs asked through QCR (ADR-003 3.1): the main window's side.

The window's own methods run on a stand-in window, so what is checked is the
real routing - busy launcher, the command line the Run button would build, each
scenario's end, the run's end, a run stopped by QCR - without opening Chrome. A
live test then carries ``scenario.run`` through the real host process.
"""

import functools
import os
import types

import pytest

from cms_gui import qcrhost
from cms_gui.launcherfeed import LauncherFeed
from cms_gui.main_window import MainWindow

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORE_PYTHON = os.path.join(ROOT, ".venv", "bin", "python")

INVENTORY = types.SimpleNamespace(
    scenarios=[{"id": "login_test", "name": "Login", "tags": ["smoke"]}, {"id": "bad/id", "name": "Nested"}],
    envs=[{"alias": "local", "value": "localhost:8069"}, {"alias": "dev", "value": "dev.example.invalid"}],
    users=[{"env": "localhost:8069", "login": "admin"}, {"env": "localhost:8069", "login": "agent"},
           {"env": "dev.example.invalid", "login": "admin"}],
    payload={})


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append((name, args, kwargs))


def window(running=False):
    host, process = Recorder(), Recorder()
    process.is_running = lambda: running
    core = types.SimpleNamespace(argv=lambda *args: ["python3", "session_launcher.py"] + list(args),
                                 display_argv=lambda *args: "session_launcher.py " + " ".join(args),
                                 spawn_dir=lambda: "/tmp")
    fake = types.SimpleNamespace(
        qcr_host=host, process=process, core=core, inventory=INVENTORY, _qcr_run=None, _entry_id=None,
        settings=types.SimpleNamespace(project_id="p"), history=types.SimpleNamespace(begin=lambda *a: "h-1"),
        run=Recorder(), run_state=types.SimpleNamespace(reset=lambda: None, cycle_running=False, cycle=None),
        log=types.SimpleNamespace(clear=lambda: None), status_right=types.SimpleNamespace(setText=lambda t: None),
        _stopping=False, _step_keys_sent=set(), launcher_feed=LauncherFeed())
    for name in ("_qcr_inventory", "_qcr_scenario_run", "_qcr_run_event", "_qcr_run_end", "_qcr_run_stop",
                 "_stop_running"):
        setattr(fake, name, functools.partial(getattr(MainWindow, name), fake))
    return fake, host, process


def owner_events(host):
    return [(args[1], kwargs) for name, args, kwargs in host.calls if name == "owner"]


def test_the_inventory_names_environments_with_their_accounts_and_skips_unnameable_ids(qapp):
    fake, _host, _process = window()
    inventory = fake._qcr_inventory()
    assert [s["id"] for s in inventory["scenarios"]] == ["login_test"]
    assert inventory["environments"] == [{"name": "local", "accounts": ["admin", "agent"]},
                                         {"name": "dev", "accounts": ["admin"]}]


def test_a_busy_launcher_fails_the_run_at_once(qapp):
    fake, host, process = window(running=True)
    fake._qcr_scenario_run("e1", "login_test", {"environment": "local"})
    assert owner_events(host)[0][0] == "failed"
    assert owner_events(host)[0][1]["error"]["code"] == "launcher_busy"
    assert not [c for c in process.calls if c[0] == "start"]


def test_a_run_is_the_run_button_s_command_line_and_its_end_is_reported(qapp):
    fake, host, process = window()
    fake._qcr_scenario_run("e1", "login_test", {"environment": "local", "accounts": ["admin"], "jobs": 2})
    argv = [c for c in process.calls if c[0] == "start"][0][1][0]
    for part in ("--run-tests=login_test", "--env=local", "--filter-users=admin", "--jobs=2", "--close-after",
                 "--events=-", "--control=-"):
        assert part in argv
    assert owner_events(host)[0] == ("accepted", {"payload": {"history_entry": "h-1"}})
    fake.launcher_feed.begin("--run-tests=login_test", "flows")       # what _on_started does
    tree = {"id": "0", "label": "Login", "kind": "group", "children": [
        {"id": "0/0", "label": "goto /web/login", "kind": "step", "step_index": 0},
        {"id": "0/1", "label": "access.open_app", "kind": "group", "children": [
            {"id": "0/1/0", "label": "click Sign in", "kind": "step", "step_index": 1}]}]}
    for event in ({"kind": "run.dir", "dir": "/runs/1"},
                  {"kind": "flow.start", "session": "s1", "id": "login_test", "scenario": "Login", "tree": tree,
                   "steps": 2},
                  {"kind": "step.end", "session": "s1", "index": 0, "status": "pass", "attempts": 1},
                  {"kind": "step.end", "session": "s1", "index": 1, "status": "fail", "attempts": 2,
                   "message": "Timeout 5000ms waiting for #signin"},
                  {"kind": "flow.end", "session": "s1", "status": "fail", "passed": 1, "total": 2},
                  {"kind": "artifacts.written", "session": "s1", "dir": "/runs/1/s1",
                   "files": ["step-1.png", "page.mhtml"]},
                  {"kind": "run.finished", "exit_code": 1}):
        fake.launcher_feed.event(event)               # _on_event feeds the digest first
        fake._qcr_run_event(event)
    state, fields = owner_events(host)[-1]
    assert state == "succeeded"
    assert fields["output"] == {"exit_code": 1, "outcome": "failed", "history_entry": "h-1", "run_dir": "/runs/1",
                                "scenarios": [{
                                    "id": "login_test", "name": "Login", "session": "s1", "status": "fail",
                                    "passed": 1, "total": 2,
                                    "failed_step": {"index": 1, "label": "click Sign in", "blocks": ["access.open_app"],
                                                    "status": "fail", "attempts": 2,
                                                    "message": "Timeout 5000ms waiting for #signin"},
                                    "artifacts": {"dir": "/runs/1/s1", "files": ["step-1.png", "page.mhtml"]}}]}
    if os.path.exists(CORE_PYTHON):                      # what the host will check it against
        import json
        import subprocess
        checked = subprocess.run([CORE_PYTHON, "-c", "import json, sys; from qcr.adapters.scenarios import RUN_OUTPUT; "
                                  "from qavector_sdk.validation import compile_output; "
                                  "compile_output(RUN_OUTPUT)(json.load(sys.stdin))"],
                                 input=json.dumps(fields["output"]), text=True, capture_output=True, cwd=ROOT)
        assert checked.returncode == 0, checked.stderr
    assert fake._qcr_run is None


def test_keep_windows_leaves_them_open(qapp):
    fake, _host, process = window()
    fake._qcr_scenario_run("e1", "login_test", {"environment": "local", "keep_windows": True})
    assert "--close-after" not in [c for c in process.calls if c[0] == "start"][0][1][0]


def test_a_stop_from_qcr_stops_the_run_and_reports_nothing_more(qapp):
    fake, host, process = window()
    fake._qcr_scenario_run("e1", "login_test", {"environment": "local"})
    process.is_running = lambda: True
    fake._qcr_run_stop()
    assert ("stop", (), {}) in process.calls and fake._qcr_run is None
    fake._qcr_run_event({"kind": "run.finished", "exit_code": 0})
    assert [state for state, _ in owner_events(host)] == ["accepted"]


def test_a_launcher_that_ends_before_its_run_finished_fails_it(qapp):
    fake, host, _process = window()
    fake._qcr_scenario_run("e1", "login_test", {"environment": "local"})
    fake._qcr_run_end(-9, ended_early=True)
    state, fields = owner_events(host)[-1]
    assert state == "failed" and fields["error"]["code"] == "run_ended" and fields["output"]["outcome"] == "error"


@pytest.mark.skipif(not os.path.exists(CORE_PYTHON), reason="needs the core's .venv")
def test_scenario_run_travels_through_the_real_host(qapp, tmp_path):
    import time
    from cms_gui import servicesfile as sf
    from cms_gui.services import ServiceSupervisor
    supervisor = ServiceSupervisor()
    supervisor.sync([sf.ProjectRow(name="Group", runners=[])])
    host = qcrhost.QcrHost(supervisor)
    asked = []

    def handler(execution_id, resource_id, given):
        asked.append((resource_id, given))
        host.owner(execution_id, "accepted")
        host.owner(execution_id, "succeeded", output={"exit_code": 0, "outcome": "passed",
                                                      "scenarios": [{"id": resource_id, "status": "pass"}]})
    host.set_handler("scenario.run", handler)
    fake, _h, _p = window()
    host.set_inventory(fake._qcr_inventory())
    host.start([CORE_PYTHON, os.path.join(ROOT, "session_launcher.py"), "--qcr-host", "--qcr-socket=off", "--qcr-project=p",
                "--qcr-store=" + qcrhost.store_path(str(tmp_path))], "p")
    ended, replies = [], []
    try:
        end = time.monotonic() + 20
        while host.state != qcrhost.READY and time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.01)
        # An actor with scenario rights, as an agent's will be (6a); asked as it, not as the window.
        host._send({"type": "actor", "id": 900, "actor": "agent:test",
                    "grants": [{"permission": "scenarios.read"}, {"permission": "scenarios.run"}]})
        request_id = next(host._ids)
        host._replies[request_id] = replies.append
        host._send({"type": "invoke", "id": request_id, "actor": "agent:test", "capability": "scenarios.login_test.run",
                    "input": {"environment": "local"}})
        host.execution_changed.connect(lambda e: ended.append(e) if e["state"] in qcrhost.TERMINAL else None)
        end = time.monotonic() + 20
        while not ended and time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.01)
        assert replies and replies[0]["ok"]
        assert asked == [("login_test", {"environment": "local"})]
        assert ended[0]["state"] == "succeeded" and ended[0]["actor_id"] == "agent:test"
        assert ended[0]["output"]["outcome"] == "passed"
    finally:
        host.stop()
        supervisor.shutdown(1000)
