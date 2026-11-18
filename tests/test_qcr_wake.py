"""Waking the conversation that handed work on (ADR-009), the core's side.

* A task may wait for others (``after``): it starts when they succeed, and is
  skipped when one of them does not.
* An agent connection works on at most a few tasks at once; more wait their
  turn instead of being refused.
* The conversation is woken - ``round_finished`` to its owner - when what it
  asked for has ended (``wake_when``), or else when all of a turn's tasks
  have, but only once that turn is over and only for results it did not see.
* The answer to handing work on says what comes next.

Fake agent programs: no model is asked anything. Waiting here reads the
journal, never the agent's ``wait`` - which would count as the agent seeing it.
"""

import time

import pytest

from qcr.adapters import delegation
from test_qcr_delegation import ACTOR, World, _codex_turns


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.jsonl"))
    made = []

    def make(wake=None, **kwargs):
        one = World(tmp_path, **kwargs)
        if wake:
            turn(one, wake=wake)
        made.append(one)
        return one
    yield make
    for one in made:
        one.close()


def turn(w, wake="ask", targets=("codex", "claude")):
    """A new turn of the delegating conversation: its actor issued again."""
    grants = [{"permission": "agents.delegate", "resource_id": delegation.target_resource(one)} for one in targets]
    grants.append({"permission": "agents.delegate", "resource_id": delegation.TASKS})
    w.host._on_actor({"actor": ACTOR, "grants": grants, "agents": {
        "session": "s-parent", "connection": "claude", "name": "Claude", "folder": str(w.folder),
        "mode": "full", "others": "auto", "restrictions": {}, "wake": wake}})


def end_turn(w):
    w.host._on_revoke({"actor": ACTOR})


def ended(w, execution_id, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        execution = w.host.store.get(execution_id)
        if execution["state"] in ("succeeded", "failed", "canceled", "timed_out"):
            return execution
        time.sleep(0.05)
    pytest.fail("did not end: %s" % w.host.store.get(execution_id)["state"])


def until(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def woken(w):
    return [one for one in w.sent if one.get("type") == "round_finished"]


def hand(w, target="codex", **task):
    answer = w.delegate(target, **task)
    assert answer["ok"], answer
    return answer


def test_a_task_after_another_starts_when_it_succeeds(world, tmp_path):
    w = world(wake="continue")
    tests = hand(w, name="tests", goal="Run the tests")["execution"]["execution_id"]
    commit = hand(w, "claude", name="commit", after=["tests"], goal="Commit the change")["execution"]["execution_id"]
    assert ended(w, tests)["state"] == "succeeded"
    done = ended(w, commit)
    assert done["state"] == "succeeded", done
    kinds = [event.get("type") for event in w.events(commit)]
    assert "waiting" in kinds and kinds.index("waiting") < kinds.index("agent.started")


def test_a_task_after_one_that_fails_is_skipped_and_never_runs(world, tmp_path):
    w = world(wake="continue")
    tests = hand(w, name="tests", goal="REFUSE to run")["execution"]["execution_id"]
    commit = hand(w, name="commit", after=["tests"], goal="Commit it")["execution"]["execution_id"]
    assert ended(w, tests)["state"] == "failed"
    done = ended(w, commit)
    assert done["error"]["code"] == "skipped" and "tests" in done["error"]["message"]
    starts = [row for row in _codex_turns(tmp_path) if row.get("method") == "turn/start"]
    assert len(starts) == 1


def test_unknown_names_and_names_in_use_are_refused(world):
    w = world(wake="continue")
    unknown = hand(w, after=["nothing"])["execution"]["execution_id"]
    assert ended(w, unknown)["error"]["code"] == "invalid_input"
    first = hand(w, name="dup", goal="HANG here")["execution"]["execution_id"]
    second = hand(w, "claude", name="dup")["execution"]["execution_id"]
    assert ended(w, second)["error"]["code"] == "invalid_input"
    w.ask("cancel", execution_id=first)


def test_more_tasks_for_one_agent_wait_their_turn(world, monkeypatch):
    monkeypatch.setattr(delegation, "MAX_PER_TARGET", 1)
    w = world(wake="continue")
    first = hand(w, goal="HANG on")["execution"]["execution_id"]
    second = hand(w, goal="Then this")["execution"]["execution_id"]
    assert until(lambda: "waiting" in [e.get("type") for e in w.events(second)])
    assert w.host.store.get(second)["state"] == "dispatched"
    w.ask("cancel", execution_id=first)
    assert ended(w, second)["state"] == "succeeded"


def test_a_turn_s_round_wakes_the_conversation_only_after_the_turn_ended(world):
    w = world(wake="continue")
    task = hand(w, name="tests", goal="Run the tests", model="fixture-model")["execution"]["execution_id"]
    ended(w, task)
    time.sleep(0.2)
    assert woken(w) == []                    # its turn is still on: it can see for itself
    end_turn(w)
    assert until(lambda: woken(w))
    [told] = woken(w)
    assert told["session"] == "s-parent" and told["condition"]["kind"] == "round"
    [item] = told["items"]
    assert item["name"] == "tests" and item["state"] == "succeeded" and item["answer"].startswith("Answer")
    assert item["model"] == "fixture-model" and item["target_name"] == "Codex" and told["failed"] is False


def test_a_result_the_agent_saw_itself_wakes_nothing(world):
    w = world(wake="continue")
    task = hand(w, goal="Run the tests")["execution"]["execution_id"]
    assert w.wait(task)["state"] == "succeeded"     # the agent's own wait: seen
    end_turn(w)
    time.sleep(0.3)
    assert woken(w) == []


def test_a_result_read_after_its_wake_was_told_says_so_to_the_owner(world):
    w = world(wake="continue")
    task = hand(w, goal="Run the tests")["execution"]["execution_id"]
    end_turn(w)
    ended(w, task)
    assert until(lambda: woken(w))
    # The person speaks before the wake is delivered; in that turn the agent reads it.
    turn(w, wake="continue")
    assert w.ask("get", execution_id=task)["ok"]
    seen = [one for one in w.sent if one.get("type") == "round_seen"]
    assert seen == [{"type": "round_seen", "session": "s-parent", "execution_id": task}]
    # Read once more: nothing new to say.
    w.ask("get", execution_id=task)
    assert len([one for one in w.sent if one.get("type") == "round_seen"]) == 1


def test_wake_when_on_failure_wakes_at_once_and_says_what_still_runs(world):
    w = world(wake="continue")
    bad = hand(w, name="lint", goal="REFUSE it")["execution"]["execution_id"]
    slow = hand(w, "claude", name="tests", goal="HANG for long")["execution"]["execution_id"]
    said = w.ask("invoke", capability="agents.tasks.wake_when",
                 input={"all": ["lint", "tests"], "on_failure": True})
    assert said["ok"], said
    assert w.wait(said["execution"]["execution_id"])["output"]["wake"] == "continue"
    end_turn(w)
    ended(w, bad)
    assert until(lambda: woken(w))
    [told] = woken(w)
    assert told["condition"]["why"] == "failure" and told["failed"] is True
    assert told["running"] == ["tests"] and [one["name"] for one in told["items"]] == ["lint"]
    w.ask("cancel", execution_id=slow)


def test_the_answer_to_handing_work_on_says_what_comes_next(world):
    w = world(wake="continue")
    assert "end your turn" in hand(w)["next"]
    turn(w, wake="off")
    assert "not continued by itself" in hand(w, "claude")["next"]


@pytest.mark.parametrize("length", [10000, delegation.ANSWER_CHARS + 500])
def test_wake_preserves_review_findings_and_marks_storage_truncation(world, monkeypatch, length):
    review = "Review finding. " * (length // 16) + "FINAL FINDING"
    monkeypatch.setattr(delegation, "run_turn", lambda *args, **kwargs: (
        {"turn": {"n": 1, "state": "completed"}, "session": {"provider_session": "review-thread"}}, review))
    w = world(wake="continue")
    task = hand(w, name="review", goal="Review the changes")["execution"]["execution_id"]
    output = ended(w, task)["output"]
    end_turn(w)
    assert until(lambda: woken(w))
    [item] = woken(w)[0]["items"]
    assert item["answer"] == review[:delegation.ANSWER_CHARS]
    assert item["answer_chars"] == len(review)
    assert item["answer_truncated"] == (len(review) > delegation.ANSWER_CHARS)
    assert output["answer_truncated"] == item["answer_truncated"]
    if length < delegation.ANSWER_CHARS:
        assert item["answer"].endswith("FINAL FINDING")


def test_large_round_bounds_answers_and_identifies_the_omitted_results(world):
    w = world(wake="continue")
    items = [{"execution_id": "review-%d" % n, "answer": "x" * delegation.ANSWER_CHARS}
             for n in range(5)]
    w.host.delegation._round_finished({"type": "round_finished", "session": "s-parent", "items": items})
    [message] = woken(w)
    assert sum(len(item["answer"]) for item in message["items"]) == delegation.WAKE_ANSWER_CHARS
    assert [item["execution_id"] for item in message["items"] if item["wake_shortened"]] == [
        "review-3", "review-4"]
    assert all(len(item["answer"]) == delegation.ANSWER_CHARS for item in items)
