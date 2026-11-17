"""Budgets: the person sees what agent work costs, and decides (ADR-013), the core's side.

* A budget is dollars, tokens, or both; whichever is reached first counts.
  Without one nothing changes; an explicit "none" is not the default.
* A task's budget is its connection's default, never more than its
  conversation has left, lower when the agent says so.
* At the limit the next turn waits for the person - an answer's turn, or a new
  task from a conversation at its own limit - and the person is told.
  Continue raises the budget by what it was first set to; Stop ends the task.
  A running turn is never cut off (best effort).
* The board says a task held at its budget needs the person; a woken
  conversation is told how far into its budget it is.

Fake agent programs: no model is asked anything.
"""

import pytest

from integrations.agents import budgets, sessions, store
from test_qcr_delegation import ACTOR
from test_qcr_wake import ended, hand, turn, until, world  # noqa: F401 - the fixture


def test_a_budget_is_dollars_tokens_or_both_and_the_first_reached_counts(tmp_path):
    assert budgets.normal(None) == {} and budgets.normal({"usd": 0, "tokens": -1}) == {}
    made = budgets.normal({"usd": "5", "tokens": 10000})
    assert made == {"usd": 5.0, "step_usd": 5.0, "tokens": 10000, "step_tokens": 10000}
    at = budgets.state({"usd": 1.0, "tokens": 9000}, made)
    assert at["share"] == pytest.approx(0.9) and at["warn"] and not at["reached"]
    assert budgets.state({"usd": 5.1, "tokens": 10}, made)["reached"]
    assert budgets.state({"usd": 99, "tokens": 99}, {})["share"] is None       # none: never reached
    assert budgets.raised(made)["usd"] == 10.0 and budgets.raised(budgets.raised(made))["tokens"] == 30000
    assert budgets.smaller({"usd": 5}, {"usd": 2, "tokens": 100}, {}) == budgets.normal({"usd": 2, "tokens": 100})
    assert budgets.text({"usd": 1.2, "tokens": 12345}) == "$1.20 / 12k"
    assert budgets.text({"usd": 0, "tokens": 1400000}) == "- / 1.4M"


def test_a_conversation_s_own_budget_beats_its_connection_s_and_none_is_none(tmp_path):
    root = str(tmp_path / "sessions")
    owner = {"id": "claude", "name": "Claude", "kind": "claude", "budget_conversation": {"usd": 3}}
    made = sessions.create(root, owner, str(tmp_path), "read")
    assert budgets.load(root, made["id"], owner) == budgets.normal({"usd": 3})     # the default
    budgets.save(root, made["id"], {"tokens": 500})
    assert budgets.load(root, made["id"], owner) == budgets.normal({"tokens": 500})
    budgets.save(root, made["id"], {})
    assert budgets.load(root, made["id"], owner) == {}                              # none, not the default
    assert store.normalize({"name": "X", "kind": "claude", "budget_task": {"usd": 2}})["budget_task"]["usd"] == 2


def asking_task(w, **budget):
    task = hand(w, goal="WONDER-ALWAYS about it", **budget)["execution"]["execution_id"]
    done = ended(w, task)
    assert done["output"]["budget"]["spent"]["tokens"] > 0
    return done


def held(w, execution_id):
    return until(lambda: "budget.requested" in [one.get("type") for one in w.events(execution_id)])


def test_an_answer_at_the_task_s_budget_waits_for_the_person_and_continue_raises_it(world):
    w = world(wake="continue")
    asked = asking_task(w, budget_tokens=10)
    child = asked["output"]["session_id"]
    assert budgets.load(w.root, child) == budgets.normal({"tokens": 10})            # the agent's, lower
    said = w.ask("invoke", capability="agents.tasks.reply", input={"task": asked["execution_id"], "answer": "Go on"})
    reply = said["execution"]["execution_id"]
    assert held(w, reply)
    assert w.host.store.get(reply)["state"] == "dispatched"                          # nothing ran
    [told] = [one for one in w.sent if one.get("type") == "budget_reached"]
    assert told["scope"] == "task" and told["execution_id"] == reply and told["task"] == asked["execution_id"]
    assert told["enforcement"] == "best_effort" and told["spent"]["tokens"] > 10
    [row] = [one for one in w.host._on_tasks({})["tasks"] if one["task"] == asked["execution_id"]]
    assert row["state"] == "budget" and row["needs_person"] is True and row["budget"]["reached"]
    w.host._on_budget_answer({"execution_id": reply, "go": True, "by": "you"})
    assert ended(w, reply)["state"] == "succeeded"
    assert budgets.load(w.root, child)["tokens"] == 20                              # + what it was set to
    kinds = [one["kind"] for one in w.host._on_task_history({"task": asked["execution_id"]})["entries"]]
    assert "budget" in kinds and "budget_raised" in kinds


def test_stop_at_the_budget_ends_the_task(world):
    w = world(wake="continue")
    asked = asking_task(w, budget_tokens=10)
    reply = w.ask("invoke", capability="agents.tasks.reply",
                  input={"task": asked["execution_id"], "answer": "Go on"})["execution"]["execution_id"]
    assert held(w, reply)
    w.host._on_budget_answer({"execution_id": reply, "go": False, "by": "you"})
    assert ended(w, reply)["error"]["code"] == "stopped"
    again = w.ask("invoke", capability="agents.tasks.reply",
                  input={"task": asked["execution_id"], "answer": "And now?"})["execution"]["execution_id"]
    assert ended(w, again)["error"]["code"] in ("stopped", "not_waiting")
    with pytest.raises(Exception):
        w.host._on_budget_answer({"execution_id": reply, "go": True})               # nothing waits now


def test_a_task_never_gets_more_than_its_conversation_has_left_and_at_its_limit_new_tasks_wait(world, tmp_path):
    w = world(wake="continue")
    owner = dict(store.get("claude", w.agents), budget_task={"tokens": 100000})
    store.upsert(owner, w.agents)
    parent = sessions.create(w.root, owner, str(w.folder), "full")
    budgets.save(w.root, parent["id"], {"tokens": 300})
    grants = [{"permission": "agents.delegate", "resource_id": "agent-connection~codex"},
              {"permission": "agents.delegate", "resource_id": "agent-tasks"}]
    w.host._on_actor({"actor": ACTOR, "grants": grants, "agents": {
        "session": parent["id"], "connection": "claude", "name": "Claude", "folder": str(w.folder),
        "mode": "full", "others": "auto", "restrictions": {}, "wake": "continue"}})
    first = ended(w, hand(w, goal="Run the tests")["execution"]["execution_id"])
    child = first["output"]["session_id"]
    assert budgets.load(w.root, child)["tokens"] <= 300                             # what was left, not 100000
    for _ in range(40):                                                             # spend the conversation's budget
        if budgets.of_conversation(w.root, parent["id"])["reached"]:
            break
        ended(w, hand(w, goal="Run the tests again")["execution"]["execution_id"])
    late = hand(w, goal="One more")["execution"]["execution_id"]
    assert held(w, late)
    [told] = [one for one in w.sent if one.get("type") == "budget_reached"]
    assert told["scope"] == "conversation" and told["session"] == parent["id"]
    w.host._on_budget_answer({"execution_id": late, "go": True})
    assert ended(w, late)["state"] == "succeeded"
    assert budgets.load(w.root, parent["id"])["tokens"] == 600
    raised = w.host._on_budget_raise({"session": parent["id"]})["budget"]
    assert raised["tokens"] == 900


def test_a_woken_conversation_is_told_how_far_into_its_budget_it_is(world):
    w = world(wake="continue")
    task = hand(w, goal="Run the tests")["execution"]["execution_id"]
    ended(w, task)
    w.host._on_revoke({"actor": ACTOR})
    assert until(lambda: any(one.get("type") == "round_finished" for one in w.sent))
    [told] = [one for one in w.sent if one.get("type") == "round_finished"]
    assert told["budget"]["reached"] is False and told["budget"]["enforcement"] == "best_effort"
    turn(w, wake="continue")
