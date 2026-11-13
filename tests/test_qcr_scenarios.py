"""Scenarios through QCR (ADR-003 3.1), with the test as the GUI on the host's pipe."""

import json

import pytest

from qcr.host import Host
from test_qcr_host import USER, Timers, row, snapshot, WEB

RUNNER = USER + [{"permission": "scenarios.read"}, {"permission": "scenarios.run"}]


def inventory(project="p"):
    return {"schema_version": 1, "project_id": project,
            "scenarios": [{"id": "login_test", "name": "Login", "description": "Signs in", "tags": ["smoke"]},
                          {"id": "access_admin", "name": "Access - admin", "tags": []}],
            "environments": [{"name": "local", "accounts": ["admin", "agent"]},
                             {"name": "dev", "accounts": ["admin"]}]}


class Gui:
    def __init__(self, tmp_path, actors=None):
        self.out, self.timers, self._id = [], Timers(), 0
        self.host = Host("p", str(tmp_path / "e.sqlite"), lambda line: self.out.append(json.loads(line)),
                         schedule=self.timers)
        self.send({"type": "snapshot", "snapshot": snapshot(row(WEB, "web"))})
        self.send({"type": "inventory", "inventory": inventory()})
        self.send({"type": "hello", "protocol": 1, "actors": actors or {"gui-user": RUNNER}})

    def send(self, message):
        self.host.feed(json.dumps(message))

    def ask(self, kind, actor="gui-user", **fields):
        self._id += 1
        before = len(self.out)
        self.send(dict({"type": kind, "id": self._id, "actor": actor}, **fields))
        return [m for m in self.out[before:] if m.get("type") == "reply" and m.get("id") == self._id][0]

    def run(self, scenario="login_test", actor="gui-user", **given):
        return self.ask("invoke", actor=actor, capability="scenarios.%s.run" % scenario,
                        input=dict({"environment": "local"}, **given))

    def owner(self, execution_id, event, **fields):
        self.send(dict({"type": "owner", "execution_id": execution_id, "event": event}, **fields))

    def of(self, kind):
        return [m for m in self.out if m["type"] == kind]

    def get(self, execution_id):
        return self.ask("get", execution_id=execution_id)["execution"]


@pytest.fixture
def gui(tmp_path):
    one = Gui(tmp_path)
    yield one
    one.host.close()


def test_the_inventory_becomes_capabilities_and_describe_reads_it(gui):
    described = gui.ask("invoke", capability="scenarios.login_test.describe")
    assert described["error"]["code"] == "not_invocable", "describe is a read, not an action"
    reader = gui.host.registry
    caller = gui.host._callers["gui-user"]
    ids = sorted(c.capability_id for c in reader.list_capabilities(caller, "p", namespace="scenarios"))
    assert ids == ["scenarios.access_admin.describe", "scenarios.access_admin.run",
                   "scenarios.login_test.describe", "scenarios.login_test.run"]
    answer = reader.read(caller, "p", "scenarios.login_test.describe", {})
    assert answer["name"] == "Login" and answer["tags"] == ["smoke"]
    assert [e["name"] for e in answer["environments"]] == ["local", "dev"]


def test_a_run_goes_to_the_owner_and_its_end_is_the_outcome(gui):
    reply = gui.run(accounts=["admin"], jobs=2)
    assert reply["ok"]
    execution_id = reply["execution"]["execution_id"]
    dispatch = gui.of("dispatch")[-1]
    assert dispatch == {"type": "dispatch", "execution_id": execution_id, "op": "scenario.run",
                        "resource_id": "login_test", "actor": "gui-user",
                        "input": {"environment": "local", "accounts": ["admin"], "jobs": 2}}
    gui.owner(execution_id, "accepted")
    gui.owner(execution_id, "running")
    gui.owner(execution_id, "succeeded", output={"exit_code": 1, "outcome": "failed",
                                                 "scenarios": [{"id": "login_test", "status": "fail"}]})
    done = gui.get(execution_id)
    assert done["state"] == "succeeded", "the run went through to its end"
    assert done["output"]["outcome"] == "failed", "...and its scenario failed: a separate thing"


@pytest.mark.parametrize("given, code", [
    ({"environment": "prod"}, "invalid_input"),
    ({"environment": "dev", "accounts": ["agent"]}, "invalid_input"),
    ({"environment": "local", "jobs": 99}, "invalid_input"),
    ({"environment": "local", "colour": "red"}, "invalid_input"),
])
def test_a_run_the_project_cannot_do_is_refused_before_anything_is_written(gui, given, code):
    reply = gui.ask("invoke", capability="scenarios.login_test.run", input=given)
    assert reply["error"]["code"] == code
    assert gui.ask("list")["executions"] == [] and gui.of("dispatch") == []


def test_without_the_run_grant_the_scenario_is_only_readable(tmp_path):
    gui = Gui(tmp_path, actors={"gui-user": RUNNER, "reader": [{"permission": "scenarios.read"}]})
    assert gui.run(actor="reader")["error"]["code"] == "not_found"
    gui.host.close()


def test_a_busy_launcher_is_the_owner_s_answer(gui):
    execution_id = gui.run()["execution"]["execution_id"]
    gui.owner(execution_id, "failed", error={"code": "launcher_busy", "message": "Another run holds the launcher."})
    after = gui.get(execution_id)
    assert after["state"] == "failed" and after["error"]["code"] == "launcher_busy"


def test_cancel_and_the_deadline_stop_the_run(gui):
    first = gui.run(timeout_seconds=60)["execution"]["execution_id"]
    gui.owner(first, "accepted")
    assert sorted(t.seconds for t in gui.timers.made if not t.cancelled) == [75]
    gui.timers.fire(75)
    assert gui.get(first)["state"] == "timed_out" and gui.of("stop_waiting")[-1]["execution_id"] == first

    second = gui.run()["execution"]["execution_id"]
    gui.owner(second, "accepted")
    assert gui.ask("cancel", execution_id=second)["execution"]["state"] == "canceled"
    assert gui.of("stop_waiting")[-1]["execution_id"] == second


def test_one_run_of_a_scenario_at_a_time_and_another_scenario_waits_on_the_launcher(gui):
    first = gui.run()["execution"]["execution_id"]
    assert gui.run()["error"]["execution_id"] == first
    assert gui.run(scenario="access_admin")["ok"], "the launcher, not QCR, decides whether two runs fit"


def test_an_owner_that_never_answers_fails_the_run(gui):
    execution_id = gui.run()["execution"]["execution_id"]
    gui.timers.fire(15)
    assert gui.get(execution_id)["error"]["code"] == "owner_unavailable"


def test_service_reports_still_reach_the_services(gui):
    service_run = gui.ask("invoke", capability="services.%s.start" % WEB, input={"wait_ready": False})
    execution_id = service_run["execution"]["execution_id"]
    gui.owner(execution_id, "accepted")
    gui.owner(execution_id, "succeeded", output={"service_status": "running", "readiness": "not_checked"})
    assert gui.get(execution_id)["state"] == "succeeded"
