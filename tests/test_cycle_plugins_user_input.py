"""``user.input``: asking a person to write something a later step reads.

It shares the gate's pipe and the gate's rule - not answered is not an answer -
so the tests that matter are the ways of *not* writing: nobody there, Cancel,
an empty box, no application. Each of them must fail the step, or an agent
after it would start work on a blank task.
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
    out = tmp_path / "events.jsonl"
    events.configure(str(out))
    ask_mod.reset()
    ask_mod.configure(True)
    yield out
    ask_mod.reset()
    events.configure(None)


@pytest.fixture
def step_runner(tmp_path):
    def run(**settings):
        timeout = settings.pop("_timeout", 1)
        workspace = create("20261013-000000-a", str(tmp_path / "runs"))
        context = RunContext(CycleRun(id="r", cycle_id="c", workspace=workspace),
                             None, workspace, cancel=CancelToken())
        step = CycleStep(id="ask", plugin="user.input", timeout=timeout,
                         settings=dict({"question": "What should the agent do?"},
                                       **settings))
        return registry.get("user.input").execute(context, step)
    return run


def _answer_soon(answer, text="", who=""):
    def later():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if ask_mod.deliver(1, answer=answer, text=text, who=who):
                return
            time.sleep(0.01)
    threading.Thread(target=later, daemon=True).start()


def _asked(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("kind") == "cycle.ask"]


def test_what_was_written_is_the_step_s_output(asking, step_runner):
    _answer_soon("Send", text="  Add a filter by status  ", who="tester")
    result = step_runner()

    assert result.ok
    assert result.outputs["text"] == "Add a filter by status"
    assert result.outputs["who"] == "tester"


def test_the_question_asks_for_text_and_offers_send_and_cancel(asking, step_runner):
    _answer_soon("Send", text="x")
    step_runner(placeholder="One line is enough", default="Start here")

    one = _asked(asking)[0]
    assert one["options"] == ["Send", "Cancel"]
    assert one["text"] == {"placeholder": "One line is enough",
                           "default": "Start here", "required": True}


def test_cancel_fails_the_step(asking, step_runner):
    _answer_soon("Cancel", text="half a thought")
    result = step_runner()

    assert not result.ok
    assert result.outputs["text"] == ""
    assert "cancelled" in result.message


def test_an_empty_box_is_no_answer_when_text_is_required(asking, step_runner):
    _answer_soon("Send", text="   ")
    result = step_runner()

    assert not result.ok
    assert "nothing was written" in result.message


def test_an_empty_box_is_allowed_when_text_is_optional(asking, step_runner):
    _answer_soon("Send", text="")
    result = step_runner(required=False)

    assert result.ok
    assert result.outputs["text"] == ""


def test_nobody_answering_fails_the_step(asking, step_runner):
    result = step_runner(_timeout=0.3)

    assert not result.ok
    assert "nobody answered" in result.message


def test_a_dismissed_window_fails_the_step(asking, step_runner):
    _answer_soon("", text="")
    assert not step_runner().ok


def test_with_no_application_it_fails_at_once(tmp_path, step_runner):
    ask_mod.reset()
    started = time.monotonic()
    result = step_runner(_timeout=30)

    assert not result.ok
    assert time.monotonic() - started < 5


def test_text_longer_than_the_limit_fails_the_step_at_once(asking, step_runner):
    """Refused at delivery it would leave the step waiting out its deadline for
    an answer the window, already closed, never sends again."""
    _answer_soon("Send", text="x" * (ask_mod.MAX_TEXT + 1))
    started = time.monotonic()
    result = step_runner(_timeout=30)

    assert not result.ok
    assert "longer than" in result.message
    assert time.monotonic() - started < 5


def test_a_plain_question_carries_no_text(asking):
    result = []
    thread = threading.Thread(target=lambda: result.append(ask_mod.ask(
        "Commit?", timeout_ms=2000)))
    thread.start()
    for _ in range(100):
        if asking.exists() and _asked(asking):
            break
        time.sleep(0.01)
    assert "text" not in _asked(asking)[0]
    assert ask_mod.deliver(1, answer="Approve", text="ignored")
    thread.join(3)
    assert "text" not in result[0]


def test_a_question_is_required():
    assert registry.get("user.input").problems({}) != []
    assert registry.get("user.input").problems({"question": "What?"}) == []


def test_its_metadata_survives_the_wire():
    json.dumps(registry.get("user.input").metadata.to_dict())
