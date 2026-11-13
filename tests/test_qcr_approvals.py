"""Cycle approvals through QCR (ADR-003 3.3): what is open, and answering it.

An answer is ``cycles.<id>.answer`` - high risk, the ``cycles.approve`` grant
(the user's decision: an agent holding it may answer all four kinds). Every
check is made before anything is written.
"""

import pytest

from test_qcr_cycles import RUNNER, Gui

APPROVER = RUNNER + [{"permission": "cycles.approve"}]

PENDING = {"cycle": "nightly", "run_id": "r-1", "items": [
    {"id": "node:abc", "kind": "node_approval", "step": "deploy", "label": "Deploy", "question": "Run Deploy?",
     "inputs": {"target": "staging"}},
    {"id": "ask:1", "kind": "approval", "step": "gate", "question": "Ship it?", "options": ["Approve", "Reject"]},
    {"id": "ask:2", "kind": "revision", "step": "plan", "question": "Accept the plan?", "options": ["Approve", "Reject"],
     "revision": {"enabled": True}},
    {"id": "ask:3", "kind": "input", "step": "why", "question": "Why?", "options": ["Send"], "text": {"required": True}},
    {"id": "ask:4", "kind": "choice", "step": "pick", "question": "Which?", "options": ["Choose", "Cancel"],
     "choice": {"mode": "single", "columns": ["Name"], "rows": [["a"], ["b"]]}},
]}


@pytest.fixture
def gui(tmp_path):
    one = Gui(tmp_path, actors={"gui-user": RUNNER, "agent:claude/s1": APPROVER})
    one.send({"type": "pending", "pending": PENDING})
    yield one
    one.host.close()


def answer(gui, item, answer=None, actor="agent:claude/s1", cycle="nightly", **extra):
    return gui.ask("invoke", actor=actor, capability="cycles.%s.answer" % cycle,
                   input=dict({"item": item, "answer": answer}, **extra))


def test_what_is_open_is_read_per_cycle(gui):
    caller = gui.host._callers["gui-user"]
    pending = gui.host.registry.read(caller, "p", "cycles.nightly.pending", {})
    assert pending["run_id"] == "r-1" and [i["id"] for i in pending["items"]] == [
        "node:abc", "ask:1", "ask:2", "ask:3", "ask:4"]
    assert gui.host.registry.read(caller, "p", "cycles.broken.pending", {}) == {"run_id": "", "items": []}


def test_answering_needs_the_approve_grant(gui):
    assert answer(gui, "node:abc", "approve", actor="gui-user")["error"]["code"] == "not_found"
    assert gui.of("dispatch") == []


def test_an_answer_goes_to_the_owner_with_who_gave_it(gui):
    reply = answer(gui, "node:abc", "approve")
    execution_id = reply["execution"]["execution_id"]
    assert gui.of("dispatch")[-1] == {"type": "dispatch", "execution_id": execution_id, "op": "cycle.answer",
                                      "resource_id": "nightly~approvals", "actor": "agent:claude/s1",
                                      "input": {"item": "node:abc", "answer": "approve"}}
    gui.owner(execution_id, "accepted")
    gui.owner(execution_id, "succeeded", output={"item": "node:abc", "decided": "approved"})
    done = gui.get(execution_id)
    assert done["state"] == "succeeded" and done["actor_id"] == "agent:claude/s1"


@pytest.mark.parametrize("item, given, code", [
    ("node:gone", {"answer": "approve"}, "not_pending"),
    ("node:abc", {"answer": "yes"}, "invalid_input"),
    ("ask:1", {"answer": "Maybe"}, "invalid_input"),
    ("ask:1", {"answer": "revise", "feedback": "do more"}, "invalid_input"),
    ("ask:2", {"answer": "revise"}, "invalid_input"),
    ("ask:3", {"answer": "Send"}, "invalid_input"),
    ("ask:4", {"answer": "Choose", "chosen": [5]}, "invalid_input"),
    ("ask:4", {"answer": "Choose", "chosen": [0, 1]}, "invalid_input"),
])
def test_an_answer_the_question_does_not_take_is_refused_before_anything_is_written(gui, item, given, code):
    assert answer(gui, item, **given)["error"]["code"] == code
    assert gui.ask("list")["executions"] == [] and gui.of("dispatch") == []


@pytest.mark.parametrize("item, given", [
    ("node:abc", {"answer": "decline"}), ("ask:1", {"answer": "Reject"}),
    ("ask:2", {"answer": "revise", "feedback": "split the second step"}),
    ("ask:3", {"answer": "Send", "text": "because"}), ("ask:4", {"answer": "Choose", "chosen": [1]}),
])
def test_every_kind_takes_its_own_answers(gui, item, given):
    assert answer(gui, item, **given)["ok"]


def test_an_answer_is_not_busy_behind_the_cycle_s_own_run(gui):
    run = gui.ask("invoke", actor="agent:claude/s1", capability="cycles.nightly.execute", input={})
    assert run["ok"] and run["execution"]["state"] != "succeeded", "the run is still under way"
    assert answer(gui, "node:abc", "approve")["ok"]


def test_two_answers_on_one_cycle_go_one_at_a_time(gui):
    first = answer(gui, "ask:1", "Approve")["execution"]["execution_id"]
    second = answer(gui, "ask:2", "Approve")
    assert second["error"]["code"] == "busy" and second["error"]["execution_id"] == first


def test_nothing_open_any_more_means_nothing_to_answer(gui):
    gui.send({"type": "pending", "pending": None})
    assert answer(gui, "node:abc", "approve")["error"]["code"] == "not_pending"
