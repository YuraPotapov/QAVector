"""scenario.run: the command line it builds, and what it reads back.

No launcher is started here. What the launcher does is covered by
tests/test_session_launcher.py and tests/test_runner.py; repeating it through a
subprocess would make these slow and prove nothing extra. What is worth checking
is the boundary: that the fields become the right flags, that the child's event
stream is read correctly, and that a cancelled step actually signals it.
"""

import json
import os

import pytest

from cycle import registry, workspace
from cycle.context import CancelToken, RunContext
from cycle.plugins.scenario import ScenarioRun, launcher_argv
from domain.cycle import CycleRun, CycleStep


@pytest.fixture
def context(tmp_path):
    path = workspace.create("20260917-000000-demo", str(tmp_path))
    return RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                      None, path, flows_dir=str(tmp_path / "flows"))


class _Child:
    """A launcher that does not exist: it writes an event stream and exits."""

    def __init__(self, code=0, events=(), stall=False):
        self.code = code
        self.events = list(events)
        self.stall = stall
        self.argv = None
        self.signalled = []
        self.pid = 4242
        self._waits = 0

    def __call__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        for arg in argv:
            if arg.startswith("--events="):
                path = arg.split("=", 1)[1]
                with open(path, "w", encoding="utf-8") as handle:
                    for event in self.events:
                        handle.write(json.dumps(event) + "\n")
        return self

    def wait(self, timeout=None):
        self._waits += 1
        if self.stall and self._waits < 3:
            import subprocess
            raise subprocess.TimeoutExpired("launcher", timeout or 0)
        return self.code

    def terminate(self):
        self.signalled.append("term")

    def kill(self):
        self.signalled.append("kill")


@pytest.fixture
def child(monkeypatch):
    """Install a fake launcher; the test sets what it says before running."""
    fake = _Child()
    monkeypatch.setattr("subprocess.Popen", fake)
    # The step signals a process group; there is no group behind a fake pid.
    monkeypatch.setattr(os, "killpg",
                        lambda pid, sig: fake.signalled.append(sig))
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    return fake


def run(context, child, **settings):
    settings.setdefault("scenarios", "smoke_odoo")
    step = CycleStep(id="run", plugin="scenario.run", settings=settings)
    return ScenarioRun().execute(context, step)


def flags(child):
    """The child's argv as ``{flag: value}``, for readable assertions."""
    found = {}
    for arg in child.argv or []:
        if arg.startswith("--") and "=" in arg:
            key, value = arg.split("=", 1)
            found[key] = value
        elif arg.startswith("--"):
            found[arg] = True
    return found


def passed(scenario="smoke_odoo", session="localhost-admin"):
    return [{"kind": "run.dir", "dir": "/tmp/reports/20260917-000000"},
            {"kind": "session.start", "session": session,
             "scenarios": [scenario]},
            {"kind": "flow.end", "session": session, "status": "pass",
             "passed": 1, "total": 1}]


# ------------------------------------------------------------- what it runs
def test_the_scenarios_asked_for_reach_the_launcher(context, child):
    child.events = passed()
    run(context, child, scenarios="smoke_odoo,claim75")
    assert flags(child)["--run-tests"] == "smoke_odoo,claim75"


def test_a_tag_is_passed_through_untouched(context, child):
    child.events = passed()
    run(context, child, scenarios="tag:smoke")
    assert flags(child)["--run-tests"] == "tag:smoke"


def test_reports_go_under_the_step_s_own_directory(context, child, tmp_path):
    """Two scenario steps in one cycle must not write into one tree."""
    child.events = passed()
    run(context, child)
    reports = flags(child)["--reports-dir"]
    assert reports.endswith(os.path.join("steps", "run", "reports"))
    assert str(tmp_path) in reports


def test_the_child_writes_its_events_to_a_file_not_a_pipe(context, child):
    """Nothing here is reading that pipe, and a child blocking on a full one
    would hang the step."""
    child.events = passed()
    run(context, child)
    assert flags(child)["--events"].endswith("events.jsonl")


def test_the_environment_and_users_are_passed_when_given(context, child):
    child.events = passed()
    run(context, child, env="localhost", users="admin,agent")
    assert flags(child)["--env"] == "localhost"
    assert flags(child)["--filter-users"] == "admin,agent"


def test_nothing_is_passed_for_a_field_left_blank(context, child):
    child.events = passed()
    run(context, child, env="", users="")
    assert "--env" not in flags(child)
    assert "--filter-users" not in flags(child)


def test_the_cycle_s_flows_directory_is_handed_down(context, child):
    """So a cycle and the scenarios it runs read the same tree."""
    child.events = passed()
    run(context, child)
    assert flags(child)["--flows-dir"] == context.flows_dir


def test_windows_at_once_is_passed_and_defaults_to_one(context, child):
    child.events = passed()
    run(context, child)
    assert flags(child)["--jobs"] == "1"

    run(context, child, jobs="auto")
    assert flags(child)["--jobs"] == "auto"


def test_a_run_without_a_browser_says_so(context, child):
    child.events = passed()
    run(context, child, browser=False)
    assert "--no-browser" in flags(child)


def test_the_windows_are_closed_afterwards_by_default(context, child):
    """Leaving them open is rarely what a cycle wants."""
    child.events = passed()
    run(context, child)
    assert "--close-after" in flags(child)

    run(context, child, close_after=False)
    assert "--close-after" not in flags(child)


def test_extra_flags_are_split_the_way_a_shell_would(context, child):
    child.events = passed()
    run(context, child, extra="--report-always --log-level=DEBUG")
    assert "--report-always" in (child.argv or [])
    assert "--log-level=DEBUG" in (child.argv or [])


def test_the_launcher_is_found_without_guessing_an_interpreter():
    argv = launcher_argv()
    assert argv
    assert argv[0]     # whatever this process is running under


# ------------------------------------------------------- what it reads back
def test_a_run_where_everything_passed_succeeds(context, child):
    child.events = passed()
    result = run(context, child)

    assert result.ok
    assert result.outputs["exit_code"] == 0
    assert result.outputs["passed"] == 1
    assert result.outputs["failed"] == 0
    assert "1 scenario(s) passed" in result.message


def test_the_scenarios_that_actually_ran_are_reported(context, child):
    child.events = passed(scenario="smoke_odoo") + [
        {"kind": "session.start", "session": "other", "scenarios": ["claim75"]},
        {"kind": "flow.end", "session": "other", "status": "pass"}]
    result = run(context, child)
    assert result.outputs["scenarios"] == "smoke_odoo,claim75"


def test_a_failed_scenario_fails_the_step_and_is_counted(context, child):
    child.code = 1
    child.events = [
        {"kind": "session.start", "session": "s", "scenarios": ["smoke_odoo"]},
        {"kind": "flow.end", "session": "s", "status": "fail"},
        {"kind": "flow.end", "session": "s", "status": "pass"}]
    result = run(context, child)

    assert not result.ok
    assert result.outputs["failed"] == 1
    assert result.outputs["passed"] == 1
    assert result.message == "1 scenario(s) failed"


def test_a_launcher_that_failed_for_another_reason_quotes_what_it_said(
        context, child, tmp_path):
    """An exit code alone sends somebody to a log they have not been told about."""
    child.code = 2
    child.events = []
    result = run(context, child)

    assert not result.ok
    assert "exited 2" in result.message


def test_the_run_directory_is_recorded_relative_to_the_workspace(context, child,
                                                                 tmp_path):
    child.events = [{"kind": "run.dir",
                     "dir": os.path.join(context.workspace, "steps", "run",
                                         "reports", "20260917-000000")}]
    result = run(context, child)
    assert result.outputs["run_dir"] == os.path.join("steps", "run", "reports",
                                                     "20260917-000000")


def test_the_event_stream_and_the_log_are_kept_as_artifacts(context, child):
    """The detail lives in the child's own stream; this step reports a result."""
    child.events = passed()
    result = run(context, child)

    names = {artifact.name for artifact in result.artifacts}
    assert names == {"events", "launcher"}
    assert all(not os.path.isabs(one.path) for one in result.artifacts)
    assert all(one.producer == "run" for one in result.artifacts)


def test_an_unreadable_event_stream_does_not_break_the_step(context, child):
    """A child killed mid-write leaves a partial line; the exit code still counts."""
    child.events = []
    child.code = 0

    def write_rubbish(argv, **kwargs):
        child.argv = list(argv)
        for arg in argv:
            if arg.startswith("--events="):
                with open(arg.split("=", 1)[1], "w", encoding="utf-8") as handle:
                    handle.write('{"kind": "run.dir"\n{ broken\n')
        return child

    import subprocess
    subprocess.Popen = write_rubbish
    result = run(context, child)
    assert result.ok


def test_a_run_that_produced_no_events_at_all_is_still_reported(context, child):
    child.events = []
    result = run(context, child)
    assert result.ok
    assert result.outputs["passed"] == 0


# -------------------------------------------------------------------- stopping
def test_a_cancelled_step_signals_the_launcher(context, child):
    """SIGINT, not SIGTERM: the launcher handles it and closes its windows in
    order, giving Chrome time to write its cookies."""
    import signal

    child.stall = True
    context.cancel = CancelToken()
    context.cancel.set("the run was stopped")
    result = run(context, child)

    assert not result.ok
    assert signal.SIGINT in child.signalled
    assert "signalled and did not finish" in result.message


def test_a_launcher_that_cannot_be_started_fails_rather_than_raising(context,
                                                                     monkeypatch):
    def refuse(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr("subprocess.Popen", refuse)
    step = CycleStep(id="run", plugin="scenario.run",
                     settings={"scenarios": "x"})
    result = ScenarioRun().execute(context, step)

    assert not result.ok
    assert "cannot run the launcher" in result.message


# -------------------------------------------------------------------- the table
def test_the_plugin_is_in_the_table_and_queues_against_its_own_kind():
    """Each of these opens real browser windows; two at once is more machine
    than most have, and the inner --jobs is where parallelism belongs."""
    plugin = registry.get("scenario.run")
    assert plugin is not None
    assert plugin.metadata.category == registry.SCENARIO
    assert plugin.metadata.concurrency_group == "scenarios"
    assert "browser" in plugin.metadata.permissions


def test_scenarios_are_required():
    assert ScenarioRun().problems({}) == ["Scenarios is required."]


def test_a_nonsense_window_count_is_complained_about():
    assert ScenarioRun().problems({"scenarios": "x", "jobs": "soon"})
    assert ScenarioRun().problems({"scenarios": "x", "jobs": 0})
    assert ScenarioRun().problems({"scenarios": "x", "jobs": "auto"}) == []
    assert ScenarioRun().problems({"scenarios": "x", "jobs": 4}) == []
