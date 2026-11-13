"""Cycles through QCR (ADR-003 3.2), with the test as the GUI on the host's pipe."""

import json

import pytest

from qcr.host import Host
from test_qcr_host import USER, Timers, row, snapshot, WEB
from test_qcr_scenarios import inventory as scenario_inventory

RUNNER = USER + [{"permission": "cycles.read"}, {"permission": "cycles.run"}]


def inventory():
    one = scenario_inventory()
    one["cycles"] = [{"id": "nightly", "name": "Nightly", "description": "Everything", "steps": 4, "problems": []},
                     {"id": "broken", "name": "Broken", "steps": 1, "problems": ["step x: unknown plugin"]}]
    return one


class Gui:
    def __init__(self, tmp_path, actors=None, inventory_=None):
        self.out, self.timers, self._id = [], Timers(), 0
        self.host = Host("p", str(tmp_path / "e.sqlite"), lambda line: self.out.append(json.loads(line)),
                         schedule=self.timers)
        self.send({"type": "snapshot", "snapshot": snapshot(row(WEB, "web"))})
        self.send({"type": "inventory", "inventory": inventory_ or inventory()})
        self.send({"type": "hello", "protocol": 1, "actors": actors or {"gui-user": RUNNER}})

    def send(self, message):
        self.host.feed(json.dumps(message))

    def ask(self, kind, actor="gui-user", **fields):
        self._id += 1
        before = len(self.out)
        self.send(dict({"type": kind, "id": self._id, "actor": actor}, **fields))
        return [m for m in self.out[before:] if m.get("type") == "reply" and m.get("id") == self._id][0]

    def execute(self, cycle="nightly", actor="gui-user", **given):
        return self.ask("invoke", actor=actor, capability="cycles.%s.execute" % cycle, input=given)

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


def test_cycles_become_capabilities_beside_the_scenarios(gui):
    caller = gui.host._callers["gui-user"]
    ids = sorted(c.capability_id for c in gui.host.registry.list_capabilities(caller, "p", namespace="cycles"))
    assert ids == ["cycles.broken.describe", "cycles.broken.execute", "cycles.broken.pending",
                   "cycles.nightly.describe", "cycles.nightly.execute", "cycles.nightly.pending"], \
        "answer needs cycles.approve, which this caller does not hold"
    described = gui.host.registry.read(caller, "p", "cycles.broken.describe", {})
    assert described["problems"] == ["step x: unknown plugin"] and described["steps"] == 1


def test_a_run_goes_to_the_owner_and_its_steps_are_followed(gui):
    reply = gui.execute(variables={"branch": "main"}, execution_mode="important")
    execution_id = reply["execution"]["execution_id"]
    assert gui.of("dispatch")[-1] == {"type": "dispatch", "execution_id": execution_id, "op": "cycle.execute",
                                      "resource_id": "nightly", "actor": "gui-user",
                                      "input": {"variables": {"branch": "main"}, "execution_mode": "important"}}
    gui.owner(execution_id, "accepted")
    gui.owner(execution_id, "running", payload={"run_id": "r-1"})
    gui.owner(execution_id, "progress", kind="step", payload={"id": "fetch", "status": "succeeded"})
    gui.owner(execution_id, "succeeded", output={"run_id": "r-1", "status": "failed", "exit_code": 1,
                                                 "steps": [{"id": "fetch", "status": "succeeded"},
                                                           {"id": "test", "status": "failed"}]})
    done = gui.get(execution_id)
    assert done["state"] == "succeeded" and done["output"]["status"] == "failed", "the run ended; the cycle failed"
    steps = [e for e in gui.ask("events", execution_id=execution_id)["events"] if e["type"] == "owner.step"]
    assert steps[0]["payload"] == {"id": "fetch", "status": "succeeded"}


@pytest.mark.parametrize("cycle, given, code", [
    ("broken", {}, "cycle_invalid"),
    ("nightly", {"variables": {"1bad": "x"}}, "invalid_input"),
    ("nightly", {"execution_mode": "yolo"}, "invalid_input"),
    ("nightly", {"timeout_seconds": 10 ** 9}, "invalid_input"),
])
def test_a_run_that_cannot_go_is_refused_before_anything_is_written(gui, cycle, given, code):
    assert gui.execute(cycle=cycle, **given)["error"]["code"] == code
    assert gui.ask("list")["executions"] == [] and gui.of("dispatch") == []


def test_without_the_run_grant_a_cycle_is_only_readable(tmp_path):
    gui = Gui(tmp_path, actors={"gui-user": RUNNER, "reader": [{"permission": "cycles.read"}]})
    assert gui.execute(actor="reader")["error"]["code"] == "not_found"
    gui.host.close()


def test_cancel_and_the_deadline_stop_the_run(gui):
    first = gui.execute(timeout_seconds=600)["execution"]["execution_id"]
    gui.owner(first, "accepted")
    gui.timers.fire(615)
    assert gui.get(first)["state"] == "timed_out" and gui.of("stop_waiting")[-1]["execution_id"] == first
    second = gui.execute()["execution"]["execution_id"]
    gui.owner(second, "accepted")
    assert gui.ask("cancel", execution_id=second)["execution"]["state"] == "canceled"
    assert gui.of("stop_waiting")[-1]["execution_id"] == second


def test_an_inventory_without_cycles_still_serves_the_scenarios(tmp_path):
    gui = Gui(tmp_path, inventory_=scenario_inventory())
    caller = gui.host._callers["gui-user"]
    assert gui.host.registry.list_capabilities(caller, "p", namespace="cycles") == []
    gui.host.close()
