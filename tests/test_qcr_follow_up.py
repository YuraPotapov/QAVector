"""A delegated task asks, and its answer goes on in the same conversation (ADR-011).

* A child that cannot go on ends its turn with a question line: the task ends
  asking - not failed, not done - and wakes its delegator with the question.
* ``agents.tasks.reply`` runs one more turn of the same child conversation: the
  agent program's own session is resumed, so the child keeps what it read.
* The person may answer too, from the owner pipe, as the conversation that
  delegated it - its turn may be over.
* Only a task that asks, of one's own, is answered, once. It may ask as often
  as it needs - each question counted - until the person stops it; then it
  takes no answer, and what of it still runs is canceled.
* What waits for a task that asks waits for its answer and the rest.

Fake agent programs: no model is asked anything.
"""

import pytest

from qcr.adapters import delegation
from test_qcr_delegation import ACTOR, _codex_turns
from test_qcr_wake import end_turn, ended, hand, turn, until, woken, world  # noqa: F401 - the fixture

QUESTION = "Which database should the tests use?"


def reply(w, task, answer, actor=ACTOR):
    said = w.ask("invoke", actor, capability="agents.tasks.reply", input={"task": task, "answer": answer})
    assert said["ok"], said
    return said


def test_a_question_line_ends_the_task_asking_and_wakes_its_delegator_with_it():
    assert delegation.question_of('Stuck.\n{"needs": "answer", "question": "Which one?"}') == "Which one?"
    assert delegation.question_of('```\n{"needs": "answer", "question": " Why? "}\n```') == "Why?"
    assert delegation.question_of('{"needs": "answer"}') == ""
    assert delegation.question_of('{"verdict": "approve"}') == ""


def test_a_task_that_asks_wakes_its_delegator_with_the_question(world):
    w = world(wake="continue")
    task = hand(w, name="tests", goal="WONDER about the tests")["execution"]["execution_id"]
    done = ended(w, task)
    output = done["output"]
    assert done["state"] == "succeeded" and output["needs_input"] is True
    assert output["question"] == QUESTION and output["asked"] == 1 and output["root"] == task
    end_turn(w)
    assert until(lambda: woken(w))
    [told] = woken(w)
    [item] = told["items"]
    assert item["state"] == "asks" and item["question"] == QUESTION and item["name"] == "tests"
    assert told["failed"] is False             # asking is not failing (D5)


def test_the_delegator_s_answer_goes_on_in_the_same_conversation(world, tmp_path):
    w = world(wake="continue")
    task = hand(w, name="tests", goal="WONDER about the tests")["execution"]["execution_id"]
    first = ended(w, task)["output"]
    end_turn(w)
    turn(w, wake="continue")                    # the woken turn
    said = reply(w, task, "Use the sqlite fixture.")
    assert "end your turn" in said["next"]
    answered = ended(w, said["execution"]["execution_id"])
    output = answered["output"]
    assert answered["state"] == "succeeded" and not output.get("needs_input")
    assert output["session_id"] == first["session_id"] and output["follow_up_of"] == task
    assert output["root"] == task and output["by"] == "Claude"
    assert "Use the sqlite fixture." in output["answer"] and "answers your question" in output["answer"]
    # The agent program's own session went on: nothing it read was lost.
    assert any(row.get("method") == "thread/resume" for row in _codex_turns(tmp_path))
    again = reply(w, task, "Once more")
    assert ended(w, again["execution"]["execution_id"])["error"]["code"] == "not_waiting"


def test_the_person_answers_as_the_conversation_after_its_turn_ended(world):
    w = world(wake="ask")
    task = hand(w, goal="WONDER about the tests")["execution"]["execution_id"]
    ended(w, task)
    end_turn(w)
    assert ACTOR not in w.host._callers
    made = w.host._on_task_reply({"task": task, "answer": "Use the sqlite fixture.", "by": "You"})["execution"]
    answered = ended(w, made["execution_id"])
    assert answered["state"] == "succeeded" and answered["actor_id"] == ACTOR
    assert answered["output"]["by"] == "You"
    assert ACTOR not in w.host._callers           # the caller lent for it is gone again


def test_a_finished_review_continues_in_its_existing_session(world, tmp_path):
    w = world(wake="continue")
    task = hand(w, name="review", goal="Review the commit")["execution"]["execution_id"]
    first = ended(w, task)["output"]
    end_turn(w)
    turn(w, wake="continue")
    continued = reply(w, task, "Review the fixes against your earlier findings.")
    second = ended(w, continued["execution"]["execution_id"])
    assert second["state"] == "succeeded"
    assert second["output"]["session_id"] == first["session_id"]
    assert second["output"]["root"] == task
    assert "continues this task" in second["output"]["answer"]
    assert any(row.get("method") == "thread/resume" for row in _codex_turns(tmp_path))
    third = reply(w, second["execution_id"], "Check the final adjustment.")
    assert ended(w, third["execution"]["execution_id"])["output"]["session_id"] == first["session_id"]
    duplicate = reply(w, task, "Old execution")
    assert ended(w, duplicate["execution"]["execution_id"])["error"]["code"] == "not_waiting"


def test_refusing_a_follow_up_keeps_the_child_available_for_the_person(world):
    w = world(others="approve")
    task = hand(w, goal="Review the change")["execution"]["execution_id"]
    w.host._on_agent_answer({"execution_id": task, "approve": True})
    child = ended(w, task)["output"]["session_id"]
    follow_up = reply(w, task, "Review it again")["execution"]["execution_id"]
    assert w.host.store.get(follow_up)["state"] == "dispatched"
    w.host._on_agent_answer({"execution_id": follow_up, "approve": False})
    assert ended(w, follow_up)["error"]["code"] == "denied"
    assert not any(e["type"] == "agent.started" for e in w.events(follow_up))
    assert w.host.delegation.latest(task) == task
    by_person = w.host._on_task_reply({"task": task, "answer": "Review these other changes", "by": "You"})
    result = ended(w, by_person["execution"]["execution_id"])
    assert result["output"]["session_id"] == child and result["output"]["by"] == "You"


def test_a_task_continued_before_a_restart_is_not_continued_again(world):
    w = world(wake="continue")
    task = hand(w, name="review", goal="Review the commit")["execution"]["execution_id"]
    ended(w, task)
    end_turn(w)
    turn(w, wake="continue")
    continued = reply(w, task, "Review the fixes.")["execution"]["execution_id"]
    ended(w, continued)
    # What a restarted QAVector has: the record, and nothing in memory.
    w.host.delegation._follow.clear()
    again = reply(w, task, "The old execution once more")
    assert ended(w, again["execution"]["execution_id"])["error"]["code"] == "not_waiting"
    assert w.host.delegation.latest(task) == continued
    # A refused reply is not a continuation: the latest execution still goes on.
    latest = reply(w, continued, "Check the final adjustment.")
    assert ended(w, latest["execution"]["execution_id"])["state"] == "succeeded"


def test_only_a_task_of_one_s_own_is_answered(world):
    w = world(wake="continue")
    plain = hand(w, goal="Run the tests")["execution"]["execution_id"]
    ended(w, plain)
    asking = hand(w, goal="WONDER about it")["execution"]["execution_id"]
    ended(w, asking)
    other = "agent:claude/s-other"
    w.grant(actor=other)
    stranger = reply(w, asking, "Not mine to answer", actor=other)
    assert ended(w, stranger["execution"]["execution_id"])["error"]["code"] == "not_found"
    with pytest.raises(Exception):
        w.host._on_task_reply({"task": asking, "answer": " "})


def test_a_task_asks_as_often_as_it_needs_and_each_question_is_counted(world):
    w = world(wake="continue")
    task = hand(w, goal="WONDER-ALWAYS")["execution"]["execution_id"]
    asked = ended(w, task)
    for number in range(2, 7):
        said = reply(w, asked["execution_id"], "WONDER-ALWAYS: try again")
        asked = ended(w, said["execution"]["execution_id"])
        assert asked["state"] == "succeeded" and asked["output"]["asked"] == number
    assert asked["output"]["root"] == task and asked["output"]["needs_input"] is True


def test_the_person_stops_a_task_it_takes_no_answer_and_what_runs_is_canceled(world):
    w = world(wake="continue")
    task = hand(w, goal="WONDER about it")["execution"]["execution_id"]
    ended(w, task)
    w.host._on_task_stop({"task": task, "by": "you"})
    refused = reply(w, task, "Too late")
    error = ended(w, refused["execution"]["execution_id"])["error"]
    assert error["code"] == "stopped" and "takes no more answers" in error["message"]
    # One that runs: its answer's turn is canceled.
    running = hand(w, goal="WONDER first")["execution"]["execution_id"]
    ended(w, running)
    going = reply(w, running, "HANG on this")["execution"]["execution_id"]
    assert until(lambda: w.host.store.get(going)["state"] == "running")
    stopped = w.host._on_task_stop({"task": running})["execution"]
    assert stopped["execution_id"] == going and stopped["state"] == "canceled"


def test_what_waits_for_a_task_that_asks_waits_for_its_answer(world):
    w = world(wake="continue")
    tests = hand(w, name="tests", goal="WONDER about the tests")["execution"]["execution_id"]
    commit = hand(w, "claude", name="commit", after=["tests"], goal="Commit it")["execution"]["execution_id"]
    ended(w, tests)
    assert until(lambda: "waiting" in [one.get("type") for one in w.events(commit)])
    assert w.host.store.get(commit)["state"] == "dispatched"     # not skipped: asking is not failing
    said = reply(w, tests, "Use the sqlite fixture.")
    assert ended(w, said["execution"]["execution_id"])["state"] == "succeeded"
    assert ended(w, commit)["state"] == "succeeded"
