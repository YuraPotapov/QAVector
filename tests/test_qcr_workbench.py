"""The workbench's view of delegated work (ADR-012 D4/D5), the core's side.

* One row a task, every conversation's, newest first: its answers folded in,
  its state as the person would act on it - waits for approval, asks, runs,
  done, stopped - and whether it needs the person.
* A task's history, oldest first: handed on, each turn, each question and its
  answer, each verdict, how it ended.
* The journal lists by capability prefix, so the board never reads the rest.

Fake agent programs: no model is asked anything.
"""

from qcr.adapters import delegation
from test_qcr_delegation import ACTOR
from test_qcr_wake import ended, hand, turn, until, world  # noqa: F401 - the fixture


def board(w):
    return w.host._on_tasks({})["tasks"]


def test_one_row_a_task_with_its_answers_folded_in_and_its_state(world):
    w = world(wake="continue")
    done = hand(w, goal="Run the tests", name="tests")["execution"]["execution_id"]
    ended(w, done)
    asking = hand(w, goal="WONDER about the tests")["execution"]["execution_id"]
    ended(w, asking)
    rows = board(w)
    assert [row["task"] for row in rows] == [asking, done]               # newest first
    assert rows[0]["state"] == "asks" and rows[0]["needs_person"] is True and rows[0]["questions"] == 1
    assert rows[0]["question"] == "Which database should the tests use?"
    assert rows[1]["state"] == "succeeded" and rows[1]["needs_person"] is False and rows[1]["name"] == "tests"
    assert rows[1]["target_name"] == "Codex" and rows[1]["from_name"] == "Claude"
    assert rows[1]["from_session"] == "s-parent" and rows[1]["child_session"] and rows[1]["criteria"] == 2

    said = w.ask("invoke", capability="agents.tasks.reply", input={"task": asking, "answer": "Use sqlite."})
    reply = said["execution"]["execution_id"]
    ended(w, reply)
    [row] = [one for one in board(w) if one["task"] == asking]
    assert row["latest"] == reply and row["state"] == "succeeded" and row["questions"] == 1
    assert len(board(w)) == 2                                            # the answer is not a task of its own


def test_a_task_waiting_for_approval_needs_the_person_and_a_stopped_one_says_so(world):
    w = world(wake="continue")
    w.grant(others="approve")
    waits = hand(w, goal="Run the tests")["execution"]["execution_id"]
    assert until(lambda: any(row["state"] == "approval" for row in board(w)))
    [row] = [one for one in board(w) if one["task"] == waits]
    assert row["needs_person"] is True
    turn(w, wake="continue")
    asking = hand(w, goal="WONDER about it")["execution"]["execution_id"]
    ended(w, asking)
    w.host._on_task_stop({"task": asking})
    [row] = [one for one in board(w) if one["task"] == asking]
    assert row["state"] == "stopped" and row["needs_person"] is False
    w.host._on_agent_answer({"execution_id": waits, "approve": False})


def test_a_task_s_history_tells_it_in_order(world):
    w = world(wake="continue")
    asking = hand(w, goal="WONDER about the tests")["execution"]["execution_id"]
    ended(w, asking)
    said = w.ask("invoke", capability="agents.tasks.reply", input={"task": asking, "answer": "Use sqlite."})
    ended(w, said["execution"]["execution_id"])
    check = w.ask("invoke", capability="agents.tasks.verify",
                  input={"task": asking, "criterion": 0, "verdict": "passed"})
    ended(w, check["execution"]["execution_id"])
    told = w.host._on_task_history({"task": asking})
    kinds = [one["kind"] for one in told["entries"]]
    assert kinds == ["handed", "started", "asked", "answer", "started", "done", "verified"], kinds
    entries = {one["kind"]: one for one in told["entries"]}
    assert entries["handed"]["to"] == "Codex" and entries["handed"]["by"] == "Claude"
    assert entries["asked"]["text"] == "Which database should the tests use?" and entries["asked"]["number"] == 1
    assert entries["answer"]["text"] == "Use sqlite." and entries["answer"]["by"] == "Claude"
    assert entries["verified"]["text"] == "Criterion 1 passed"
    assert told["task"]["task"] == asking and told["usage"].get("turns") == 2
    assert told["entries"] == sorted(told["entries"], key=lambda one: one["at"])


def test_the_journal_lists_by_capability_prefix(world):
    w = world(wake="continue")
    task = hand(w, goal="Run the tests")["execution"]["execution_id"]
    ended(w, task)
    rows = w.host.store.list("p", capability_prefix="agents.", limit=50)
    assert rows and all(row["capability_id"].startswith("agents.") for row in rows)
    assert w.host.store.list("p", capability_prefix="services.", limit=50) == []
    assert ACTOR == rows[0]["actor_id"] and delegation.BOARD_LIMIT >= 100


def test_an_answer_and_a_verdict_are_part_of_their_task(world):
    w = world(wake="continue")
    asking = hand(w, goal="WONDER about it")["execution"]["execution_id"]
    ended(w, asking)
    said = w.ask("invoke", capability="agents.tasks.reply", input={"task": asking, "answer": "Use sqlite."},
                 parent_id="made-up")
    assert said["execution"]["parent_id"] == asking            # the host says it, not the caller
    ended(w, said["execution"]["execution_id"])
    check = w.ask("invoke", capability="agents.tasks.verify", input={"task": asking, "criterion": 0,
                                                                     "verdict": "passed"})
    assert check["execution"]["parent_id"] == asking
    other = hand(w, goal="WONDER again")["execution"]["execution_id"]
    ended(w, other)
    person = w.host._on_task_reply({"task": other, "answer": "Yes."})["execution"]
    assert person["parent_id"] == other
