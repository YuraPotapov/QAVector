"""The launcher through QCR (ADR-004 L.1): status, output by cursor, stop - and
what a scenario run's result now says about a failure.

The host is in-process with its agent socket in the test's folder; the test
plays the window, answering ``owner_read`` and ``dispatch`` as it would.
"""

import threading
import time

import pytest

from qcr import host as host_module
from qcr.adapters import launcher, scenarios
from qavector_sdk.validation import compile_output
from test_qcr_agent_access import AGENT, World

READ = [{"permission": "launcher.read"}]
RUN = [{"permission": "launcher.read"}, {"permission": "launcher.run"}, {"permission": "executions.read"}]
RUN_DIGEST = {"number": 3, "label": "--run-tests=login --env=Shop", "kind": "flows", "by": "person",
              "started_at": 1.0, "ended_at": None, "exit_code": None, "execution_id": "", "history_entry": "7",
              "run_dir": "/runs/3", "scenarios": [
                  {"id": "login", "name": "Log in", "session": "u1", "status": "fail", "passed": 2, "total": 5,
                   "failed_step": {"index": 2, "label": "click Sign in", "blocks": ["access.open_app"],
                                   "status": "fail", "message": "Timeout 5000ms waiting for #signin",
                                   "attempts": 2},
                   "artifacts": {"dir": "/runs/3/u1", "files": ["step-2.png", "page.mhtml"]}}]}


@pytest.fixture
def world(tmp_path):
    one = World(tmp_path)
    yield one
    one.host.close()


def asked(world):
    return [m for m in world.out if m.get("type") == "owner_read"]


def answer_later(world, result=None, error=None, delay=0.2):
    def window():
        world.feed(dict({"type": "owner_read_reply", "id": asked(world)[-1]["id"]},
                        **({"error": error} if error else {"result": result})))
    threading.Timer(delay, window).start()


def read(world, op, **given):
    return world.host.handle_agent(AGENT, {"type": "read", "capability": "launcher.main." + op, "input": given})


def test_the_launcher_is_there_for_whoever_was_granted_it(world):
    world.grant(grants=RUN)
    listed = world.host.handle_agent(AGENT, {"type": "capabilities", "namespace": "launcher"})["capabilities"]
    assert sorted(cap["capability_id"] for cap in listed) == [
        "launcher.main.output", "launcher.main.status", "launcher.main.stop"]
    assert all(cap["resource"]["name"] == "QAVector launcher" for cap in listed)
    assert all(len(cap["description"]) <= 256 for cap in listed)


def test_without_its_grants_the_launcher_is_not_there(world):
    world.grant(grants=[{"permission": "services.read"}])
    assert world.host.handle_agent(AGENT, {"type": "capabilities", "namespace": "launcher"})["capabilities"] == []
    assert read(world, "status")["error"]["code"] == "not_found"
    assert asked(world) == []
    world.grant(grants=READ)
    stop = world.host.handle_agent(AGENT, {"type": "invoke", "capability": "launcher.main.stop", "input": {}})
    assert stop["error"]["code"] == "not_found", "reading it is not stopping it"


def test_status_is_the_window_s_word_with_the_run_s_digest(world):
    world.grant(grants=READ)
    answer_later(world, {"running": True, "run": RUN_DIGEST})
    status = read(world, "status")
    assert status["ok"], status
    failed = status["result"]["run"]["scenarios"][0]["failed_step"]
    assert failed["message"].startswith("Timeout") and failed["attempts"] == 2
    assert asked(world)[-1]["op"] == "launcher.status" and asked(world)[-1]["resource_id"] == "main"


def test_output_is_asked_by_cursor_and_its_wait_reaches_the_window(world):
    world.grant(grants=READ)
    answer_later(world, {"entries": [{"seq": 12, "at": 2.0, "type": "log", "level": "INFO", "session": "u1",
                                      "text": "attached over CDP"}], "next": 12, "dropped": False,
                         "running": True})
    out = read(world, "output", after=11, limit=50, wait_seconds=30)
    assert out["ok"] and out["result"]["next"] == 12
    question = asked(world)[-1]
    assert (question["op"], question["after"], question["limit"], question["wait_seconds"]) == (
        "launcher.output", 11, 50, 30)


def test_a_waiting_output_read_gives_the_window_the_wait_plus_its_usual_time(world, monkeypatch):
    world.grant(grants=READ)
    monkeypatch.setattr(host_module, "OWNER_READ_SECONDS", 0.2)
    answer_later(world, {"entries": [], "next": 0, "dropped": False, "running": False}, delay=0.6)
    started = time.monotonic()
    assert read(world, "output", wait_seconds=1)["ok"], "answered within wait + usual time"
    assert time.monotonic() - started < 3
    assert read(world, "output")["error"]["code"] == "owner_unavailable", "no wait: only the usual time"
    assert read(world, "output", wait_seconds=61)["error"]["code"] == "invalid_input"


def test_a_window_answer_that_breaks_the_contract_is_not_passed_on(world):
    world.grant(grants=READ)
    answer_later(world, {"entries": "everything", "next": 0, "dropped": False, "running": False})
    assert not read(world, "output")["ok"]


def test_stop_goes_to_the_window_and_ends_when_the_launcher_has(world):
    world.grant(grants=RUN)
    started = world.host.handle_agent(AGENT, {"type": "invoke", "capability": "launcher.main.stop",
                                              "input": {"reason": "asked to stop the smoke run"}})
    execution_id = started["execution"]["execution_id"]
    dispatch = world.of("dispatch")[-1]
    assert dispatch["op"] == "launcher.stop" and dispatch["actor"] == AGENT
    assert dispatch["input"] == {"reason": "asked to stop the smoke run"}
    world.feed({"type": "owner", "execution_id": execution_id, "event": "accepted"})
    world.feed({"type": "owner", "execution_id": execution_id, "event": "succeeded",
                "output": {"stopped": True, "run": dict(RUN_DIGEST, ended_at=9.0, exit_code=143)}})
    done = world.host.handle_agent(AGENT, {"type": "get", "execution_id": execution_id})["execution"]
    assert done["state"] == "succeeded" and done["output"]["stopped"] is True


def test_a_service_stop_is_still_a_service_s(world):
    """``launcher.stop`` is its own op: the window's service path never sees it as "stop"."""
    world.grant(grants=RUN)
    world.host.handle_agent(AGENT, {"type": "invoke", "capability": "launcher.main.stop", "input": {}})
    assert world.of("dispatch")[-1]["op"] != "stop"


def test_a_scenario_run_s_result_says_which_step_failed_and_what_it_left():
    check = compile_output(scenarios.RUN_OUTPUT)
    check({"exit_code": 0, "outcome": "failed", "scenarios": RUN_DIGEST["scenarios"], "run_dir": "/runs/3",
           "history_entry": "7"})
    check({"exit_code": 0, "outcome": "passed", "scenarios": [{"id": "login", "status": "pass", "session": "u1"}]})
    with pytest.raises(Exception):
        check({"exit_code": 0, "outcome": "failed", "scenarios": [
            dict(RUN_DIGEST["scenarios"][0], failed_step={"index": 2})]})


def test_every_launcher_schema_compiles_and_descriptions_fit():
    for schema in (launcher.STATUS_OUTPUT, launcher.OUTPUT_INPUT, launcher.OUTPUT_OUTPUT, launcher.STOP_INPUT,
                   launcher.STOP_OUTPUT):
        compile_output(schema)
    assert all(len(description) <= 256 for description, _, _ in launcher.READS.values())
