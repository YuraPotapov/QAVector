"""``user.choice``: a person picks from a list an earlier step produced.

Most of what can go wrong here is data that is not there: an empty list, an
item with no key, a column nobody has, a run started on a task the queue does
not hold. Each of them gets a test, and each must end in a stated result - a
skip, a success with nothing chosen, or a failure saying why - and never in a
window with nothing to choose from or a run that carries an empty key on to a
step that would read it as "take the first one".
"""

import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import ask as ask_mod                                   # noqa: E402
from cycle import model, registry                                  # noqa: E402
from cycle.context import CancelToken, RunContext                  # noqa: E402
from cycle.workspace import create                                 # noqa: E402
from domain.cycle import SKIPPED, CycleRun, CycleStep              # noqa: E402
from engine import events                                          # noqa: E402

ISSUES = [
    {"key": "QA-1", "summary": "Fix the login", "status": "To Do",
     "assignee": "Ann", "description": "The form forgets the user."},
    {"key": "QA-2", "summary": "Add a filter", "status": "REOPEN",
     "assignee": "", "description": "Filter by status."},
    {"key": "QA-3", "summary": "Export to CSV", "status": "To Do",
     "description": ""},
]


@pytest.fixture
def asking(tmp_path):
    out = tmp_path / "events.jsonl"
    events.configure(str(out))
    ask_mod.reset()
    ask_mod.configure(True)
    yield out
    ask_mod.reset()
    events.configure(None)


class _Watching(object):
    """An observer that keeps what the step logged, and ignores the rest."""

    def __init__(self):
        self.lines = []

    def step_log(self, step_id, stream, rows):
        self.lines.extend(rows)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def watching():
    return _Watching()


@pytest.fixture
def choose(tmp_path, watching):
    """Run the step with these settings, in a cycle where it reads ``queue``."""
    def run(**settings):
        timeout = settings.pop("_timeout", 1)
        settings = dict({"question": "Which task?", "items": ISSUES}, **settings)
        cycle = model.parse_cycle({
            "id": "c", "name": "c", "steps": [
                {"id": "queue", "plugin": "command.shell", "with": {"command": "true"}},
                {"id": "pick", "plugin": "user.choice", "needs": ["queue"],
                 "with": {"question": "Which task?",
                          "items": "${steps.queue.outputs.issues}"}}]}, "c")
        workspace = create("20261013-000000-a", str(tmp_path / "runs"))
        context = RunContext(CycleRun(id="r", cycle_id="c", workspace=workspace),
                             cycle, workspace, cancel=CancelToken(), observer=watching)
        step = CycleStep(id="pick", plugin="user.choice", timeout=timeout,
                         settings=settings)
        return registry.get("user.choice").execute(context, step)
    return run


def _answer_soon(answer, chosen=None, who=""):
    def later():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if ask_mod.deliver(1, answer=answer, chosen=chosen, who=who):
                return
            time.sleep(0.01)
    threading.Thread(target=later, daemon=True).start()


def _asked(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("kind") == "cycle.ask"]


# ------------------------------------------------------------- choosing
def test_what_was_ticked_is_handed_on_in_the_list_s_order(asking, choose):
    _answer_soon("Choose", chosen=[2, 0], who="tester")
    result = choose()

    assert result.ok
    assert result.outputs["values"] == ["QA-1", "QA-3"]
    assert [one["key"] for one in result.outputs["items"]] == ["QA-1", "QA-3"]
    assert result.outputs["value"] == "QA-1"
    assert result.outputs["item"]["summary"] == "Fix the login"
    assert result.outputs["joined"] == "QA-1, QA-3"
    assert result.outputs["count"] == 2
    assert result.outputs["who"] == "tester"


def test_the_question_carries_the_table_and_not_the_items(asking, choose):
    _answer_soon("Choose", chosen=[0])
    choose(columns=["key", "summary", "assignee"], item_detail="{status}\n\n{description}")

    one = _asked(asking)[0]
    assert one["options"] == ["Choose", "Cancel"]
    table = one["choice"]
    assert table["mode"] == "multiple"
    assert table["columns"] == ["key", "summary", "assignee"]
    assert table["value_column"] == 0
    assert table["rows"][0] == {"cells": ["QA-1", "Fix the login", "Ann"],
                                "detail": "To Do\n\nThe form forgets the user.",
                                "checked": False}
    # Nothing the table does not show travels with it.
    assert "description" not in json.dumps(table["rows"][0]["cells"])


def test_single_mode_takes_exactly_one(asking, choose):
    _answer_soon("Choose", chosen=[1])
    result = choose(mode="single")

    assert result.ok
    assert result.outputs["values"] == ["QA-2"]


def test_single_mode_refuses_two(asking, choose):
    _answer_soon("Choose", chosen=[0, 1])
    result = choose(mode="single")

    assert not result.ok
    assert "takes one" in result.message


def test_checked_all_ticks_every_row(asking, choose):
    _answer_soon("Choose", chosen=[0])
    choose(checked="all")

    assert [row["checked"] for row in _asked(asking)[0]["choice"]["rows"]] == \
        [True, True, True]


def test_checked_values_tick_those_rows_whatever_their_case(asking, choose):
    _answer_soon("Choose", chosen=[0])
    choose(checked="qa-2, QA-3")

    assert [row["checked"] for row in _asked(asking)[0]["choice"]["rows"]] == \
        [False, True, True]


def test_single_mode_ticks_only_the_first_checked(asking, choose):
    _answer_soon("Choose", chosen=[1])
    choose(mode="single", checked=["QA-2", "QA-3"])

    assert [row["checked"] for row in _asked(asking)[0]["choice"]["rows"]] == \
        [False, True, False]


def test_a_value_with_a_space_is_one_value(asking, choose):
    _answer_soon("Choose", chosen=[0])
    statuses = [{"key": "In Progress"}, {"key": "To Do"}]
    choose(items=statuses, checked="In Progress")

    assert _asked(asking)[0]["choice"]["rows"][0]["checked"] is True


def test_checked_all_with_single_mode_is_refused_before_it_runs():
    found = registry.get("user.choice").problems(
        {"question": "q", "items": "${steps.a.outputs.b}", "mode": "single",
         "checked": "all"})
    assert any("checked: all" in one for one in found)


# ------------------------------------------- chosen without asking (a pin)
def test_choose_takes_the_item_without_opening_a_window(asking, choose):
    result = choose(choose="qa-2", _timeout=30)

    assert result.ok
    assert result.outputs["values"] == ["QA-2"]
    assert _asked(asking) == []


def test_choose_works_with_no_application_attached(tmp_path, choose):
    ask_mod.reset()
    result = choose(choose="QA-3")

    assert result.ok
    assert result.outputs["value"] == "QA-3"


def test_choose_naming_an_item_the_list_lacks_fails(asking, choose):
    result = choose(choose="QA-9")

    assert not result.ok
    assert "QA-9 is not among the 3 item(s)" in result.message
    assert _asked(asking) == []


def test_choose_naming_two_in_single_mode_fails(asking, choose):
    result = choose(mode="single", choose="QA-1, QA-2")

    assert not result.ok
    assert "takes one" in result.message


# ------------------------------------------------- nothing to choose from
def test_an_empty_list_skips_by_default_without_asking(asking, choose):
    result = choose(items=[])

    assert result.status == SKIPPED
    assert "Nothing to choose from: the list from queue is empty." == result.message
    assert _asked(asking) == []


def test_an_empty_list_can_continue_with_nothing_chosen(asking, choose):
    result = choose(items=[], when_empty="continue")

    assert result.ok
    assert result.outputs == {"items": [], "values": [], "value": "", "item": {},
                              "joined": "", "count": 0, "who": ""}
    assert _asked(asking) == []


def test_an_empty_list_can_fail(asking, choose):
    result = choose(items=[], when_empty="fail")

    assert not result.ok
    assert "Nothing to choose from" in result.message


def test_an_empty_list_with_a_pin_fails_whatever_when_empty_says(asking, choose):
    """A run started on one task that the queue does not hold is an error, not
    an empty queue: skipping would end it quietly as though there were no work."""
    for when_empty in ("skip", "continue", "fail"):
        result = choose(items=[], choose="QA-9", when_empty=when_empty)
        assert not result.ok
        assert result.message == "QA-9 was asked for, but the list from queue is empty."


# --------------------------------------------------- data that is not right
def test_items_that_are_text_fail_and_say_how_to_write_them(asking, choose):
    result = choose(items="[{'key': 'QA-1'}]")

    assert not result.ok
    assert "reference on its own" in result.message


def test_items_that_are_a_mapping_fail(asking, choose):
    assert "not a mapping" in choose(items={"key": "QA-1"}).message


def test_items_that_are_nothing_fail(asking, choose):
    assert "Items to choose from is required." == choose(items=None).message


def test_an_item_with_no_key_fails_naming_it_and_what_it_has(asking, choose):
    result = choose(items=[{"key": "QA-1"}, {"summary": "No key here"}])

    assert not result.ok
    assert result.message == "item 2 has no key (it has: summary)."


def test_an_item_with_an_empty_key_fails(asking, choose):
    """An empty key handed on would become `issue: ''`, which a Jira step
    reads as "take the first one in the queue"."""
    result = choose(items=[{"key": "  "}])

    assert not result.ok
    assert "item 1 has no key" in result.message


def test_two_items_with_one_key_fail(asking, choose):
    result = choose(items=[{"key": "QA-1"}, {"key": "qa-1"}])

    assert not result.ok
    assert "two items share the key qa-1 (items 1 and 2)" in result.message


def test_mappings_and_plain_values_together_fail(asking, choose):
    result = choose(items=[{"key": "QA-1"}, "QA-2"])

    assert not result.ok
    assert result.message == ("items must all be mappings or all plain values; "
                              "item 2 is not like item 1.")


def test_a_column_no_item_has_fails_naming_what_they_have(asking, choose):
    result = choose(columns=["key", "sumary"])

    assert not result.ok
    assert result.message.startswith("column sumary: no item has it (they have: ")
    assert "summary" in result.message


def test_too_many_items_fail(asking, choose):
    many = [{"key": "QA-%d" % one} for one in range(1001)]
    result = choose(items=many)

    assert not result.ok
    assert "1001 items is more than" in result.message


# -------------------------------------- what the window shows for gaps
def test_a_field_some_items_lack_is_an_empty_cell(asking, choose):
    _answer_soon("Choose", chosen=[0])
    choose(columns=["key", "assignee"])

    rows = _asked(asking)[0]["choice"]["rows"]
    assert [row["cells"] for row in rows] == [["QA-1", "Ann"], ["QA-2", ""], ["QA-3", ""]]


def test_cells_are_one_line_and_cut_to_length(asking, choose):
    _answer_soon("Choose", chosen=[0])
    long = "word " * 100
    choose(items=[{"key": "QA-1", "summary": "two\nlines", "tags": ["a", "b"],
                   "meta": {"x": 1}, "done": True, "long": long}],
           columns=["key", "summary", "tags", "meta", "done", "long"])

    cells = _asked(asking)[0]["choice"]["rows"][0]["cells"]
    assert cells[:5] == ["QA-1", "two lines", "a, b", '{"x":1}', "yes"]
    assert len(cells[5]) == 200 and cells[5].endswith("…")


def test_a_detail_with_nothing_in_it_is_empty_not_an_error(asking, choose):
    _answer_soon("Choose", chosen=[0])
    choose(item_detail="{description}{missing}")

    rows = _asked(asking)[0]["choice"]["rows"]
    assert rows[2]["detail"] == ""
    assert rows[0]["detail"] == "The form forgets the user."


def test_a_long_detail_is_cut_and_says_so(asking, choose):
    _answer_soon("Choose", chosen=[0])
    choose(items=[{"key": "QA-1", "description": "x" * 5000}],
           item_detail="{description}")

    detail = _asked(asking)[0]["choice"]["rows"][0]["detail"]
    assert detail.endswith("\n(cut at 3000 characters)")
    assert len(detail) < 3100


def test_plain_values_are_one_column_named_value(asking, choose):
    _answer_soon("Choose", chosen=[1])
    result = choose(items=["main", "release"], columns=["ignored"])

    table = _asked(asking)[0]["choice"]
    assert table["columns"] == ["value"]
    assert table["value_column"] == 0
    assert result.outputs["values"] == ["release"]


def test_a_checked_value_the_list_lacks_is_passed_over_and_logged(asking, choose,
                                                                   watching):
    _answer_soon("Choose", chosen=[0])
    result = choose(checked="QA-1, QA-9")

    assert result.ok
    assert [row["checked"] for row in _asked(asking)[0]["choice"]["rows"]] == \
        [True, False, False]
    assert "checked QA-9 is not in the list" in watching.lines


def test_a_template_that_names_more_than_a_field_is_refused():
    plugin = registry.get("user.choice")
    for template in ("{a.b}", "{a[0]}", "{}", "{0}", "{unclosed"):
        assert plugin.problems({"question": "q", "items": "x",
                                "item_detail": template}), template


# ------------------------------------------------ no answer is no answer
def test_cancel_fails_the_step(asking, choose):
    _answer_soon("Cancel", chosen=[0])
    result = choose()

    assert not result.ok
    assert result.message == "cancelled"
    assert result.outputs["values"] == []


def test_a_closed_window_fails_the_step(asking, choose):
    _answer_soon("")
    assert not choose().ok


def test_nobody_answering_fails_the_step(asking, choose):
    result = choose(_timeout=0.3)

    assert not result.ok
    assert "nobody answered" in result.message


def test_with_no_application_it_fails_at_once(tmp_path, choose):
    ask_mod.reset()
    started = time.monotonic()
    result = choose(_timeout=30)

    assert not result.ok
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("chosen, said", [
    (None, "no rows it could name"),
    ("0", "no rows it could name"),
    ([True], "no rows it could name"),
    ([], "nothing was chosen"),
    ([0, 0], "same row twice"),
    ([3], "row 4, and the list has 3"),
])
def test_a_bad_answer_fails_at_once_with_its_reason(asking, choose, chosen, said):
    _answer_soon("Choose", chosen=chosen)
    started = time.monotonic()
    result = choose(_timeout=30)

    assert not result.ok
    assert said in result.message
    assert time.monotonic() - started < 5


def test_a_plain_question_takes_no_rows(asking):
    result = []
    thread = threading.Thread(target=lambda: result.append(ask_mod.ask(
        "Commit?", timeout_ms=2000)))
    thread.start()
    for _ in range(100):
        if _asked(asking):
            break
        time.sleep(0.01)
    assert "choice" not in _asked(asking)[0]
    assert ask_mod.deliver(1, answer="Approve", chosen=[0])
    thread.join(3)
    assert "chosen" not in result[0]


def test_its_metadata_survives_the_wire():
    json.dumps(registry.get("user.choice").metadata.to_dict())


def test_what_it_chose_is_reused_by_resume_and_partial_runs():
    assert registry.get("user.choice").metadata.reusable is True
