"""Subprocess transport checks use only a local Python interpreter."""

import sys
import threading
import time

from cycle.context import RunContext
from cycle.plugins._process import run_process
from cycle.plugins.command import STOP_GRACE
from domain.cycle import CycleRun, CycleStep


def test_each_invocation_keeps_separate_complete_logs(tmp_path):
    context = RunContext(CycleRun(id="r", cycle_id="demo"), None, str(tmp_path))
    step = CycleStep(id="plugin", plugin="git.checkout")
    paths = []
    for text in ("first", "retry"):
        result = run_process(context, step, [sys.executable, "-c",
                             "import sys; print(sys.argv[1]); print('error stream', file=sys.stderr)", text],
                             str(tmp_path), "command")
        assert result.ok, result.message
        paths.append(tmp_path / result.outputs["stdout_path"])
        assert (tmp_path / result.outputs["stderr_path"]).read_text().strip() == "error stream"
    assert paths[0] != paths[1]
    assert paths[0].read_text().strip() == "first"
    assert paths[1].read_text().strip() == "retry"


def test_nonzero_exit_reports_stderr_and_keeps_artifacts(tmp_path):
    context = RunContext(CycleRun(id="r", cycle_id="demo"), None, str(tmp_path))
    step = CycleStep(id="plugin", plugin="agent.review")
    result = run_process(context, step, [sys.executable, "-c",
                         "import sys; print('provider failed', file=sys.stderr); sys.exit(3)"],
                         str(tmp_path), "agent")
    assert not result.ok
    assert "provider failed" in result.message
    assert result.outputs["exit_code"] == 3
    assert all((tmp_path / one.path).is_file() for one in result.artifacts)


def test_cancellation_stops_a_waiting_child(tmp_path):
    context = RunContext(CycleRun(id="r", cycle_id="demo"), None, str(tmp_path))
    step = CycleStep(id="plugin", plugin="agent.review")
    timer = threading.Timer(0.3, lambda: context.cancel.set("stop"))
    started = time.monotonic()
    timer.start()
    try:
        result = run_process(context, step, [sys.executable, "-c", "import time; time.sleep(30)"],
                             str(tmp_path), "agent")
    finally:
        timer.cancel()
    assert not result.ok
    assert "stopped" in result.message
    assert time.monotonic() - started < STOP_GRACE + 3
