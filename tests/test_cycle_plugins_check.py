"""Several things that must all hold before the run goes on.

Two properties carry this file. The first is that a gate which refuses **names
the check that refused** - without that it is an `if:` with extra steps, and
somebody reads "it did not pass" and has to work out which of four conditions
they broke. The second is that equality here is the *same* equality `if:` uses:
two rules about whether 3 and "3" are the same answer would drift, and the one
being read would turn out to be the other one.

Values arrive already resolved - the executor substitutes `${...}` before a
plugin sees its settings - so these tests pass the resolved text, which is
exactly what a run passes.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import bus, registry                                    # noqa: E402
from cycle.conditions import evaluate                              # noqa: E402
from cycle.context import CancelToken, RunContext                  # noqa: E402
from cycle.workspace import create                                 # noqa: E402
from domain.cycle import CycleRun, CycleStep                       # noqa: E402


@pytest.fixture
def gate(tmp_path):
    recorder = bus.Recorder()

    def go(checks):
        workspace = create("20260921-000000-g", str(tmp_path / "runs"))
        context = RunContext(CycleRun(id="r", cycle_id="c", workspace=workspace),
                             None, workspace, cancel=CancelToken(),
                             observer=recorder)
        step = CycleStep(id="g", plugin="check.gate", settings={"checks": checks})
        return registry.get("check.gate").execute(context, step)

    # What the gate said to whoever is watching, which is a separate answer
    # from what it returned: the row a reader sees is the stage, not the result.
    go.stages = lambda: [fields["stage"] for call, fields in recorder.calls
                         if call == "step_stage"]
    return go


# ------------------------------------------------------------- the operators
@pytest.mark.parametrize("expression, holds", [
    ("1 > 0", True), ("0 > 1", False), ("1 > 1", False),
    ("1 >= 1", True), ("0 >= 1", False),
    ("0 < 1", True), ("1 < 0", False), ("1 < 1", False),
    ("1 <= 1", True), ("2 <= 1", False),
    ("a == a", True), ("a == b", False),
    ("a != b", True), ("a != a", False),
])
def test_each_operator_counts_what_it_promises(gate, expression, holds):
    assert gate({"one": expression}).ok is holds, expression


def test_the_boundary_is_included_where_it_says_it_is(gate):
    """`<=` at the limit is the case a budget lands on, and getting it wrong
    costs somebody their last attempt or gives them one too many."""
    assert gate({"budget": "3 <= 3"}).ok
    assert not gate({"budget": "4 <= 3"}).ok


def test_a_negative_and_a_decimal_are_numbers(gate):
    assert gate({"one": "-1 < 0"}).ok
    assert gate({"one": "1.5 > 1.4"}).ok


# ----------------------------------------------------------- what it reports
def test_a_refusal_names_the_check_that_refused(gate):
    """Otherwise this is an `if:` with extra steps."""
    result = gate({"there is work": "1 > 0",
                   "within budget": "5 <= 3",
                   "tree is the one": "abc == abc"})

    assert not result.ok
    assert result.outputs["failed_check"] == "within budget"
    assert "within budget" in result.message


def test_the_first_failure_is_the_answer_not_the_last(gate):
    """A gate is written cheapest first, and the earliest failure is the one
    that explains the rest."""
    result = gate({"nothing to do": "0 > 0", "and it is bad": "1 > 2"})

    assert result.outputs["failed_check"] == "nothing to do"


def test_the_refusal_shows_the_values_not_only_the_expression(gate):
    """Which is usually the whole question: 'the tree changed' means nothing
    without the two trees."""
    result = gate({"tree is the one": "aaa111 == bbb222"})

    assert "aaa111" in result.message and "bbb222" in result.message


def test_every_check_is_accounted_for_however_it_came_out(gate):
    result = gate({"first": "1 > 0", "second": "0 > 1"})

    assert len(result.outputs["reasons"]) == 2
    assert any("first" in one for one in result.outputs["reasons"])
    assert any("second" in one for one in result.outputs["reasons"])


def test_passing_says_so_plainly(gate):
    result = gate({"a": "1 > 0", "b": "x == x"})

    assert result.ok
    assert result.outputs["passed"] is True
    assert result.outputs["failed_check"] == ""


# --------------------------------------------------- the row somebody reads
def test_the_row_says_which_way_the_gate_went(gate):
    """The title used to be the failed check's bare name, which reads as the
    name of a phase: "within its budget" looks like something that went well."""
    gate({"there is work": "1 > 0", "within its budget": "5 <= 3"})
    refused, = gate.stages()

    assert refused["kind"] == "gate"
    assert refused["status"] == "failed"
    assert refused["title"] == "Gate refused: within its budget"


def test_a_gate_that_held_says_so_in_its_row_too(gate):
    gate({"a": "1 > 0", "b": "x == x"})
    passed, = gate.stages()

    assert passed["status"] == "done"
    assert passed["title"] == "Gate passed: all 2 checks held"


def test_every_check_carries_which_way_it_went(gate):
    """The point of the row: the joined text could only say what was compared,
    so which of four lines refused had to be worked out by eye."""
    gate({"there is work": "1 > 0", "within its budget": "5 <= 3",
          "tree is the one": "abc == abc"})
    checks = gate.stages()[0]["body"]["checks"]

    assert [one["name"] for one in checks] == ["there is work",
                                               "within its budget",
                                               "tree is the one"]
    assert [one["held"] for one in checks] == [True, False, True]
    assert checks[1]["expression"] == "5 <= 3"
    assert checks[1]["account"] == "5 <= 3"


def test_the_row_still_carries_the_text_it_always_did(gate):
    """So a reader that does not know this kind - an older GUI, a log - shows
    what it showed before rather than nothing."""
    result = gate({"first": "1 > 0", "second": "0 > 1"})

    assert gate.stages()[0]["detail"] == "\n".join(result.outputs["reasons"])


def test_a_number_is_shown_as_one_and_an_empty_answer_is_named(gate):
    """`'5' <= '3'` reads as two pieces of text being compared, when what
    happened was arithmetic; and two quotes are not something anybody can
    measure."""
    result = gate({"within its budget": "5 <= 3"})
    assert "5 <= 3" in result.message

    result = gate({"not already committed": " != committed"})
    assert "(empty) != 'committed'" in result.outputs["reasons"][0]


def test_the_outputs_are_there_either_way(gate):
    """A downstream `if:` reads `passed` on the branch where the gate refused,
    so a refusal that dropped its outputs would fail that step instead."""
    for checks in ({"a": "1 > 0"}, {"a": "0 > 1"}):
        assert set(gate(checks).outputs) == {"passed", "failed_check", "reasons"}


# -------------------------------------------------------------- the refusals
def test_a_line_with_no_comparison_is_refused_before_the_run():
    """Not a check that fails - a check nobody finished writing. Failing the
    gate would read as though the thing being guarded had gone wrong."""
    problems = registry.get("check.gate").problems(
        {"checks": {"half written": "${steps.a.outputs.tree}"}})

    assert problems and "half written" in problems[0]
    assert "makes no comparison" in problems[0]


def test_an_operator_without_spaces_is_not_an_operator():
    """The spaces are what keep the split where it was meant: a resolved
    summary or commit message may well contain a `>` of its own."""
    assert registry.get("check.gate").problems({"checks": {"one": "1>0"}}) != []


def test_a_value_containing_the_operator_is_protected_by_quoting(gate):
    """Values arrive already resolved, and a summary or a diff may well contain
    a `>`. Quoting is how you say it is part of the value."""
    assert gate({"one": "'a > b' == 'a > b'"}).ok
    assert not gate({"one": "'a > b' == 'a < b'"}).ok


def test_the_leftmost_operator_is_the_one_that_splits(gate):
    """A line with two of them is ambiguous however it is read, so it is read
    the way somebody scans it."""
    from cycle.plugins.check import _operator_in

    assert _operator_in("a > b == c") == (">", "a", "b == c")
    assert _operator_in("1 <= 2") == ("<=", "1", "2")


def test_an_operator_inside_quotes_is_part_of_the_value(gate):
    from cycle.plugins.check import _operator_in

    assert _operator_in("'x > y' != z") == ("!=", "'x > y'", "z")


def test_comparing_things_that_are_not_numbers_says_so(gate):
    """Deliberately not just False: 'not a number' and 'smaller' are different
    answers, and the second sends somebody looking for the wrong problem."""
    result = gate({"budget": "unknown < 3"})

    assert not result.ok
    assert "not numbers" in result.message


def test_checks_are_required():
    assert registry.get("check.gate").problems({}) != []


def test_checks_must_be_a_mapping():
    assert registry.get("check.gate").problems({"checks": ["1 > 0"]}) != []


def test_an_unknown_setting_is_refused():
    assert registry.get("check.gate").problems(
        {"checks": {"a": "1 > 0"}, "nope": 1}) != []


# ------------------------------------------------------------ one truth only
@pytest.mark.parametrize("left, right", [
    (3, "3"), ("3", 3), (True, "true"), (False, "false"),
    ("a", "a"), ("a", "b"), (1, 2),
])
def test_equality_is_the_same_equality_a_condition_uses(gate, left, right):
    """Two rules about whether 3 and "3" are the same answer would drift, and
    the one being read would turn out to be the other one."""
    by_condition = evaluate("${vars.left} == ${vars.right}",
                            {"vars": {"left": left, "right": right}})
    by_gate = gate({"same": "%s == %s" % (left, right)}).ok

    assert by_gate is by_condition, "%r vs %r" % (left, right)


def test_quotes_are_optional_and_do_not_change_what_is_compared(gate):
    """Most sides arrive bare because `${...}` was already resolved, but a
    value with spaces needs them, and quoting out of habit must not change
    the answer."""
    assert gate({"one": "'a b' == a b"}).ok
    assert gate({"one": '"x" == x'}).ok
    assert not gate({"one": "'x' == 'y'"}).ok


def test_a_boolean_written_both_ways_is_one_thing(gate):
    """Python writes True; YAML writes true. A real boolean output arrives in
    Python's spelling and the literal beside it was typed by a person - so
    comparing them as text makes `verified == true` quietly false on exactly
    the runs where it should be true."""
    assert gate({"verified": "True == true"}).ok
    assert gate({"verified": "False == false"}).ok
    assert not gate({"verified": "True == false"}).ok
    assert gate({"verified": "True != false"}).ok


def test_only_two_boolean_words_are_one_thing(gate):
    """`1 == true` staying false is right: somebody who wrote that meant a
    number, and quietly agreeing would be a different kind of wrong."""
    assert not gate({"one": "1 == true"}).ok
    assert not gate({"one": "yes == true"}).ok


# --------------------------------------------------------------- the shape
def test_it_is_the_pair_to_the_gate_that_asks_a_person():
    """One asks a person, one asks the facts. Both refuse the same way."""
    assert registry.get("check.gate") is not registry.get("approval.gate")
    assert registry.get("check.gate").metadata.permissions == ()


def test_its_metadata_survives_the_wire():
    json.dumps(registry.get("check.gate").metadata.to_dict())
