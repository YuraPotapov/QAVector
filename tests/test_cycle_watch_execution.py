"""Listen executes its node and the nodes after it, with the normal executor's protections."""

import sys
import threading

import pytest

from cycle import model, registry, watch
from cycle.context import CancelToken


def listening_node(tmp_path, script="print('healthy')", **extra):
    return model.parse_cycle({
        "id": "health", "variables": {"word": "resolved"},
        "triggers": [{"id": "listen_ping", "watch": "ping"}],
        "steps": [{"id": "ping", "plugin": "command.shell",
                   "with": {"command": [sys.executable, "-c", script],
                            "env": {"CHECK_VALUE": "${vars.word}"}}, **extra},
                  {"id": "other", "plugin": "command.shell",
                   "with": {"command": [sys.executable, "-c",
                            "from pathlib import Path; Path(%r).touch()" % str(tmp_path / "other")]}},
                  {"id": "next", "plugin": "command.shell", "needs": ["ping"],
                   "with": {"command": [sys.executable, "-c",
                            "from pathlib import Path; Path(%r).touch()" % str(tmp_path / "next")]}}]}, "health")


def test_every_check_runs_the_node_and_what_waits_on_it_without_a_pin(tmp_path):
    marker = tmp_path / "calls"
    cycle = listening_node(tmp_path,
        "import os; from pathlib import Path; "
        "p=Path(%r); p.open('a').write(os.environ['CHECK_VALUE']+'\\n'); print('healthy')" % str(marker))
    assert not model.problems(cycle, registry=registry)
    for _ in range(2):
        result = watch.check(cycle, memory_path=str(tmp_path / "memory.json"))
        assert result["new"] == [] and result["pin"] == ""
        assert result["execution"]["stdout_tail"] == "healthy"
        assert result["execution"]["status"] == "success"
    assert marker.read_text() == "resolved\nresolved\n"
    assert not (tmp_path / "other").exists()
    assert (tmp_path / "next").exists()


def test_failure_is_reported_on_the_first_check_and_recovers(tmp_path):
    cycle = listening_node(tmp_path, "print('server unavailable'); raise SystemExit(1)",
                           on_failure="continue")
    with pytest.raises(watch.WatchError, match="server unavailable"):
        watch.check(cycle)
    assert not (tmp_path / "next").exists()
    cycle.steps[0].settings["command"] = [sys.executable, "-c", "print('back')"]
    assert watch.check(cycle)["execution"]["status"] == "success"
    assert (tmp_path / "next").exists()


def test_a_node_after_it_reacts_to_its_failure(tmp_path):
    cycle = listening_node(tmp_path, "raise SystemExit(1)", on_failure="continue")
    cycle.steps.append(model.parse_cycle({"id": "x", "steps": [
        {"id": "notify", "plugin": "command.shell", "needs": ["ping"],
         "if": "${steps.ping.status} == 'failed'",
         "with": {"command": [sys.executable, "-c",
                  "from pathlib import Path; Path(%r).touch()" % str(tmp_path / "notified")]}},
    ]}, "x").steps[0])
    with pytest.raises(watch.WatchError):
        watch.check(cycle)
    assert (tmp_path / "notified").exists()
    (tmp_path / "notified").unlink()
    cycle.steps[0].settings["command"] = [sys.executable, "-c", "print('back')"]
    watch.check(cycle)
    assert not (tmp_path / "notified").exists()


@pytest.mark.parametrize("extra, message", [
    ({"needs": ["other"]}, "dependencies"),
    ({"if": "${steps.other.status} == 'success'"}, "other nodes"),
    ({"disabled": True}, "disabled"),
])
def test_unavailable_prerequisites_are_refused(tmp_path, extra, message):
    cycle = listening_node(tmp_path, **extra)
    assert any(message in problem for problem in model.problems(cycle, registry=registry))
    with pytest.raises(watch.WatchError, match=message):
        watch.check(cycle)


def test_listen_does_not_bypass_node_approval(tmp_path):
    cycle = listening_node(tmp_path)
    cycle.execution = {"default_mode": "approve"}
    with pytest.raises(watch.WatchError, match="requires approval"):
        watch.check(cycle)


def test_node_timeout_stops_its_command(tmp_path):
    cycle = listening_node(tmp_path, "import time; time.sleep(30)", timeout=0.1)
    with pytest.raises(watch.WatchError, match="timed out"):
        watch.check(cycle)


def test_cancelling_listen_stops_the_running_node(tmp_path):
    token = CancelToken()
    timer = threading.Timer(0.3, lambda: token.set("Listen stopped"))
    timer.start()
    try:
        with pytest.raises(watch.WatchError, match="Listen stopped"):
            watch.check(listening_node(tmp_path, "import time; time.sleep(30)"), cancel=token)
    finally:
        timer.cancel()


def test_executor_keeps_runtime_and_folder_restrictions(tmp_path, monkeypatch):
    from cycle import executor
    cycle = listening_node(tmp_path)
    cycle.project_folder = str(tmp_path)
    cycle.restrictions = {"network": "deny"}
    # Exercise projection without depending on a host sandbox installation.
    monkeypatch.setattr(model, "problems", lambda *a, **kw: [])
    captured = []
    def run(projected, workspace, **kwargs):
        captured.append(projected)
        raise RuntimeError("executor reached")
    monkeypatch.setattr(executor, "run_cycle", run)
    with pytest.raises(RuntimeError, match="executor reached"):
        watch.check(cycle)
    assert captured[0].project_folder == cycle.project_folder
    assert captured[0].restrictions == cycle.restrictions
    assert captured[0].execution == cycle.execution
    assert [step.id for step in captured[0].steps] == ["ping", "next"]


def test_missing_secret_prevents_command_execution(tmp_path, monkeypatch):
    cycle = listening_node(tmp_path)
    cycle.secrets = ("credential",)
    cycle.steps[0].settings["env"]["TOKEN"] = "${vars.credential}"
    monkeypatch.setattr(watch.secret_store, "get", lambda *a: None)
    with pytest.raises(watch.WatchError, match="no value set for credential"):
        watch.check(cycle)
