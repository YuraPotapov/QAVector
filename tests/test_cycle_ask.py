"""Asking a person something mid-run, and the gate that acts on the answer.

The property worth defending above every other one here: **not answered is not
approved**. There are five ways not to get an answer - no application, nobody
there, the run stopped, the run ended, the question abandoned - and a test for
each, because the failure they would share is silent. A gate that approved on
any of them would still look like a gate in the cycle file, on the canvas, and
in the run record; the only place it would differ is the one nobody checks
until something has already been committed.

The transport itself is exercised the way the service one is: the event goes
out through ``engine.events``, an answer is delivered from another thread, and
the waiting call returns it.
"""

import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import ask as ask_mod                                   # noqa: E402
from cycle import registry                                         # noqa: E402
from cycle.context import CancelToken, RunContext                  # noqa: E402
from cycle.workspace import create                                 # noqa: E402
from domain.cycle import CycleRun, CycleStep                       # noqa: E402
from engine import events                                          # noqa: E402


@pytest.fixture
def asking(tmp_path):
    """Asking turned on, writing its events where the test can read them."""
    out = tmp_path / "events.jsonl"
    events.configure(str(out))
    ask_mod.reset()
    ask_mod.configure(True)
    yield out
    ask_mod.reset()
    events.configure(None)


def _emitted(path, kind="cycle.ask"):
    if not path.exists():
        return []
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        one = json.loads(line)
        if one.get("kind") == kind:
            found.append(one)
    return found


def _answer_soon(request_id, answer, who="", within=5.0):
    """Answer from another thread, the way the control reader does.

    It keeps trying until the question exists. Firing once after a fixed pause
    made this flaky: a gate creates its workspace on disk before it asks, and
    an answer that arrives first is ignored - correctly - leaving the test to
    sit out the timeout.
    """
    def later():
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if ask_mod.deliver(request_id, answer=answer, who=who):
                return
            time.sleep(0.01)
    threading.Thread(target=later, daemon=True).start()


# ------------------------------------------------------------ the question
def test_the_question_goes_out_with_the_answers_it_takes(asking):
    _answer_soon(1, "Approve")
    ask_mod.ask("Commit this?", detail="one file", options=["Yes", "No"],
                step="gate")

    one = _emitted(asking)[0]
    assert one["question"] == "Commit this?"
    assert one["detail"] == "one file"
    assert one["options"] == ["Yes", "No"]
    assert one["step"] == "gate"
    assert one["id"] == 1


def test_an_answer_comes_back_to_the_caller_that_waited(asking):
    _answer_soon(1, "Approve", who="tester")
    answer = ask_mod.ask("Go on?")

    assert answer["answered"] is True
    assert answer["answer"] == "Approve"
    assert answer["who"] == "tester"


def test_two_questions_at_once_get_their_own_answers(asking):
    """Steps run in parallel, so one question blocking on another's answer
    would deadlock two independent gates against each other."""
    answers = {}

    def put(name):
        answers[name] = ask_mod.ask(name)

    first = threading.Thread(target=put, args=("one",))
    second = threading.Thread(target=put, args=("two",))
    first.start()
    second.start()
    for _ in range(100):
        if len(_emitted(asking)) == 2:
            break
        time.sleep(0.02)

    asked = {one["question"]: one["id"] for one in _emitted(asking)}
    ask_mod.deliver(asked["two"], answer="B")
    ask_mod.deliver(asked["one"], answer="A")
    first.join(5)
    second.join(5)

    assert answers["one"]["answer"] == "A"
    assert answers["two"]["answer"] == "B"


# ------------------------------------------------- the ways of not knowing
def test_with_no_application_it_says_so_at_once_rather_than_waiting(tmp_path):
    ask_mod.reset()
    started = time.monotonic()
    answer = ask_mod.ask("Commit?", timeout_ms=600000)

    assert answer["answered"] is False
    assert time.monotonic() - started < 1.0, "it must not wait out the timeout"
    assert "--control" in answer["message"]


def test_nobody_answering_is_not_an_answer(asking):
    answer = ask_mod.ask("Commit?", timeout_ms=150)

    assert answer["answered"] is False
    assert answer["answer"] == ""
    assert "nobody answered" in answer["message"]


def test_a_stopped_run_stops_waiting_rather_than_sitting_out_the_timeout(asking):
    """A ten minute question would otherwise keep the launcher open for ten
    minutes after somebody pressed Stop."""
    cancel = CancelToken()
    threading.Timer(0.05, cancel.set).start()

    started = time.monotonic()
    answer = ask_mod.ask("Commit?", timeout_ms=600000, cancel=cancel)

    assert answer["answered"] is False
    assert time.monotonic() - started < 5.0


def test_it_does_not_repeat_the_reason_the_executor_already_prints(asking):
    """Stop and the step's deadline both arrive through this token, and the
    executor puts whichever it was in front of the message. Echoing it here
    printed "timed out after 30s: timed out after 30s"."""
    cancel = CancelToken()
    threading.Timer(0.05, lambda: cancel.set("timed out after 30s")).start()
    answer = ask_mod.ask("Commit?", timeout_ms=600000, cancel=cancel)

    assert answer["message"] == "nobody answered"


def test_the_run_ending_releases_whoever_is_waiting(asking):
    """Otherwise the launcher stays open for the rest of the timeout after the
    run itself is over."""
    answers = []
    thread = threading.Thread(
        target=lambda: answers.append(ask_mod.ask("Commit?", timeout_ms=600000)))
    thread.start()
    for _ in range(100):
        if _emitted(asking):
            break
        time.sleep(0.02)

    ask_mod.abandon_all()
    thread.join(5)

    assert not thread.is_alive(), "it has to let go"
    assert answers[0]["answered"] is False
    assert "ended" in answers[0]["message"]


def test_a_dismissed_question_is_delivered_but_is_not_an_answer(asking):
    """Closing the window sends an empty answer rather than nothing, so a run
    is not held up for the rest of its deadline by somebody who has already
    decided not to look. It must not read as a choice."""
    _answer_soon(1, "")
    answer = ask_mod.ask("Commit?", timeout_ms=600000)

    assert answer["answered"] is False
    assert answer["answer"] == ""
    assert "dismissed" in answer["message"]


def test_an_answer_nobody_is_waiting_for_is_ignored_rather_than_fatal(asking):
    """It arrives on the control thread. Taking that down over a late answer
    would cost every other step its Stop."""
    assert ask_mod.deliver(999, answer="Approve") is False
    assert ask_mod.deliver("not a number", answer="Approve") is False


def test_asking_is_off_until_both_halves_of_the_pipe_are_live():
    ask_mod.reset()
    assert ask_mod.enabled() is False
    assert ask_mod.configure(True) is True
    assert ask_mod.enabled() is True
    ask_mod.reset()
    assert ask_mod.enabled() is False


# ---------------------------------------------------------------- the gate
@pytest.fixture
def gate(tmp_path):
    def run(**settings):
        # Popped first: the step's deadline is a property of the step, not one
        # of its settings, and leaving it among them would be an unknown field.
        timeout = settings.pop("_timeout", 1)
        workspace = create("20260919-000000-a", str(tmp_path / "runs"))
        context = RunContext(CycleRun(id="r", cycle_id="c", workspace=workspace),
                             None, workspace, cancel=CancelToken())
        step = CycleStep(id="gate", plugin="approval.gate", timeout=timeout,
                         settings=dict({"question": "Commit this?"}, **settings))
        return registry.get("approval.gate").execute(context, step)
    return run


def test_an_approval_passes_the_step(asking, gate):
    _answer_soon(1, "Approve", who="tester")
    result = gate()

    assert result.ok
    assert result.outputs["approved"] is True
    assert result.outputs["answer"] == "Approve"
    assert result.outputs["who"] == "tester"


def test_a_rejection_fails_the_step_and_says_what_was_chosen(asking, gate):
    _answer_soon(1, "Reject")
    result = gate()

    assert not result.ok
    assert result.outputs["approved"] is False
    assert result.outputs["answer"] == "Reject"
    assert result.message == "not approved: Reject"


def test_no_answer_fails_the_step(asking, gate):
    """The one that matters. A gate that approved here would stop being a gate
    exactly where somebody believed they had one."""
    result = gate(_timeout=0.2)

    assert not result.ok
    assert result.outputs["approved"] is False
    assert result.outputs["answer"] == ""


def test_a_dismissed_question_fails_the_gate(asking, gate):
    _answer_soon(1, "")
    result = gate()

    assert not result.ok
    assert result.outputs["approved"] is False
    assert "dismissed" in result.message


def test_with_no_application_the_gate_fails_rather_than_approving(gate):
    ask_mod.reset()
    result = gate()

    assert not result.ok
    assert result.outputs["approved"] is False
    assert "does not approve" in result.message


def test_the_answers_on_offer_are_the_step_s_own(asking, gate):
    _answer_soon(1, "Ship it")
    result = gate(options=["Ship it", "Hold", "Abandon"])

    assert _emitted(asking)[0]["options"] == ["Ship it", "Hold", "Abandon"]
    assert result.ok, "the first option is what means yes"


def test_more_than_one_answer_can_mean_yes(asking, gate):
    _answer_soon(1, "Ship it anyway")
    result = gate(options=["Ship it", "Ship it anyway", "Hold"],
                  approve=["Ship it", "Ship it anyway"])

    assert result.ok and result.outputs["approved"] is True


def test_an_answer_outside_the_approving_set_does_not_approve(asking, gate):
    _answer_soon(1, "Hold")
    assert not gate(options=["Ship it", "Hold"], approve=["Ship it"]).ok


def test_the_match_ignores_case_the_way_a_person_would(asking, gate):
    _answer_soon(1, "approve")
    assert gate().ok


def test_the_step_s_own_timeout_is_the_question_s_deadline(asking, gate):
    """Written where every other deadline in the file is, rather than in a
    field of its own."""
    _answer_soon(1, "Approve")          # or the test would wait out the 45s
    gate(_timeout=45)
    assert _emitted(asking)[0]["timeout_ms"] == 45000


def test_an_approving_answer_that_is_not_on_offer_is_refused_before_it_runs():
    """Otherwise nobody could ever approve, and the step would fail looking as
    though somebody had said no."""
    problems = registry.get("approval.gate").problems(
        {"question": "Go?", "options": ["Yes", "No"], "approve": ["Maybe"]})

    assert problems and "Maybe" in problems[0]
    assert "Yes" in problems[0], "it has to say what was on offer"


def test_a_reference_is_left_for_the_run_to_resolve():
    assert registry.get("approval.gate").problems(
        {"question": "${steps.plan.outputs.summary}",
         "detail": "${steps.diff.outputs.stdout}",
         "approve": "${vars.yes}"}) == []


def test_a_question_is_required():
    assert registry.get("approval.gate").problems({}) != []


def test_its_metadata_survives_the_wire():
    json.dumps(registry.get("approval.gate").metadata.to_dict())


def test_it_declares_no_permissions():
    """It reaches nothing: no files, no network, no process. The one thing it
    touches is a person's attention, and that is not a permission."""
    assert registry.get("approval.gate").metadata.permissions == ()


def test_an_approval_is_never_taken_from_an_earlier_run():
    """A person's agreement is a fact about the run they gave it to.

    Running part of a cycle takes the steps it is not running from the last run
    of the same cycle, and this step was taken that way like any other - so a
    gate somebody had answered an hour ago, or one that had been skipped and
    was recorded as reused anyway, passed silently in every run after it. See
    ``executor._must_run``, which also refuses to inherit anything decided on
    the strength of that old answer.
    """
    assert registry.get("approval.gate").metadata.reusable is False


def test_whether_a_step_may_be_inherited_is_published():
    """A front-end offering "run from here" has to be able to say what that
    will and will not do again."""
    assert registry.get("approval.gate").metadata.to_dict()["reusable"] is False
    assert registry.get("check.gate").metadata.to_dict()["reusable"] is True


def test_revision_feedback_is_a_separate_intent_and_requires_permission(asking):
    result = []
    thread = threading.Thread(target=lambda: result.append(ask_mod.ask(
        "Review?", timeout_ms=2000, revision={"enabled": True})))
    thread.start()
    for _ in range(100):
        if _emitted(asking):
            break
        time.sleep(0.01)
    assert not ask_mod.deliver(1, revise=True, feedback=" ")
    assert not ask_mod.deliver(1, revise=True, feedback="x" * 8001)
    assert ask_mod.deliver(1, revise=True, feedback="Cover the fallback", who="reviewer")
    thread.join(3)
    assert result[0]["revision_requested"] is True
    assert result[0]["feedback"] == "Cover the fallback"
    assert result[0]["answer"] == ""


def test_plain_approval_does_not_accept_a_revision_intent(asking):
    result = []
    thread = threading.Thread(target=lambda: result.append(ask_mod.ask("Review?", timeout_ms=2000)))
    thread.start()
    for _ in range(100):
        if _emitted(asking):
            break
        time.sleep(0.01)
    assert not ask_mod.deliver(1, revise=True, feedback="Change the plan")
    assert ask_mod.deliver(1, answer="Reject")
    thread.join(3)
    assert result[0]["answer"] == "Reject"
