"""The `if:` grammar: three forms, no eval, and complaints before the run."""

import pytest

from cycle import conditions, variables
from domain.cycle import CycleRun, StepRun, FAILED, SKIPPED, SUCCESS, TIMEOUT


def scope():
    run = CycleRun(id="r", cycle_id="demo", variables={"branch": "main",
                                                       "deploy": "no"})
    steps = {
        "pg": StepRun("pg", status=SUCCESS,
                      outputs={"port": 5432, "changed": True, "count": 0,
                               "note": ""}),
        "tests": StepRun("tests", status=FAILED),
    }
    return variables.scope(run, None, steps, env={})


# ------------------------------------------------------------------ comparison
def test_a_status_comparison_that_holds():
    assert conditions.evaluate("${steps.tests.status} == 'failed'", scope())


def test_a_status_comparison_that_does_not():
    assert not conditions.evaluate("${steps.tests.status} == 'success'", scope())


def test_the_inverse_comparison():
    assert conditions.evaluate("${steps.tests.status} != 'success'", scope())
    assert not conditions.evaluate("${steps.tests.status} != 'failed'", scope())


def test_double_quotes_work_as_well_as_single():
    assert conditions.evaluate('${steps.tests.status} == "failed"', scope())


def test_a_literal_may_sit_on_either_side():
    assert conditions.evaluate("'failed' == ${steps.tests.status}", scope())


def test_two_references_can_be_compared():
    assert conditions.evaluate("${steps.pg.status} != ${steps.tests.status}",
                               scope())


def test_a_number_compares_against_its_written_form():
    """A value that travelled through YAML or a pipe arrives as text."""
    assert conditions.evaluate("${steps.pg.outputs.port} == '5432'", scope())


def test_whitespace_around_the_operator_does_not_matter():
    assert conditions.evaluate("${steps.tests.status}=='failed'", scope())


def test_an_empty_literal_is_a_usable_comparison():
    assert conditions.evaluate("${steps.pg.outputs.note} == ''", scope())


# ------------------------------------------------------------------ truthiness
def test_a_bare_reference_to_something_true():
    assert conditions.evaluate("${steps.pg.outputs.changed}", scope())


def test_a_bare_reference_to_zero_is_false():
    assert not conditions.evaluate("${steps.pg.outputs.count}", scope())


def test_a_bare_reference_to_an_empty_string_is_false():
    assert not conditions.evaluate("${steps.pg.outputs.note}", scope())


def test_the_words_people_write_for_no_read_as_false():
    """"no" reading as true is the kind of surprise that costs an afternoon."""
    assert not conditions.evaluate("${vars.deploy}", scope())


def test_a_non_empty_string_is_true():
    assert conditions.evaluate("${vars.branch}", scope())


def test_no_condition_at_all_means_the_step_always_runs():
    assert conditions.evaluate("", scope())
    assert conditions.evaluate(None, scope())
    assert conditions.evaluate("   ", scope())


# ----------------------------------------------------------------- not there
def test_a_reference_to_something_missing_stops_the_run():
    """Not a quiet false: a renamed step should say so, not skip the branch."""
    with pytest.raises(conditions.ConditionError):
        conditions.evaluate("${steps.ghost.status} == 'failed'", scope())


def test_a_missing_reference_on_its_own_also_raises():
    with pytest.raises(conditions.ConditionError):
        conditions.evaluate("${steps.ghost.status}", scope())


@pytest.mark.parametrize("expression", [
    "${steps.gate.outputs.passed}",
    "${steps.gate.outputs.passed} == 'false'",
    "${steps.gate.outputs.passed} != 'true'",
    "'false' == ${steps.gate.outputs.passed}",
])
def test_a_skipped_gate_has_no_verdict_not_a_false_verdict(expression):
    root = variables.scope(step_runs={"gate": StepRun("gate", status=SKIPPED)})

    with pytest.raises(conditions.ConditionUnavailable, match="gate was skipped"):
        conditions.evaluate(expression, root)


@pytest.mark.parametrize("status", [SUCCESS, FAILED, TIMEOUT])
def test_a_missing_output_from_a_step_that_ran_is_still_an_error(status):
    root = variables.scope(step_runs={"gate": StepRun("gate", status=status)})

    with pytest.raises(conditions.ConditionError) as error:
        conditions.evaluate("${steps.gate.outputs.passed} == 'false'", root)
    assert not isinstance(error.value, conditions.ConditionUnavailable)


def test_a_skipped_step_status_can_still_be_tested():
    root = variables.scope(step_runs={"gate": StepRun("gate", status=SKIPPED)})
    assert conditions.evaluate("${steps.gate.status} == 'skipped'", root)


def test_a_typo_in_a_skipped_steps_available_outputs_is_still_an_error():
    root = variables.scope(step_runs={
        "gate": StepRun("gate", status=SKIPPED, outputs={"passed": False})})

    assert conditions.evaluate("${steps.gate.outputs.passed} == 'false'", root)
    with pytest.raises(conditions.ConditionError) as error:
        conditions.evaluate("${steps.gate.outputs.typo} == 'false'", root)
    assert not isinstance(error.value, conditions.ConditionUnavailable)


# ------------------------------------------------------------------- complaints
def test_a_well_formed_condition_has_nothing_to_complain_about():
    assert conditions.problems("${steps.a.status} == 'failed'") == []
    assert conditions.problems("${steps.a.outputs.ok}") == []
    assert conditions.problems("${a.b} != \"x\"") == []


def test_no_condition_has_nothing_to_complain_about():
    assert conditions.problems("") == []
    assert conditions.problems(None) == []


def test_a_bare_word_is_complained_about():
    """It would be a name in Python and is nothing at all here."""
    assert conditions.problems("failed")


def test_an_unquoted_literal_is_complained_about():
    assert conditions.problems("${steps.a.status} == failed")


def test_an_empty_side_is_complained_about():
    assert conditions.problems("${steps.a.status} ==")


def test_the_complaint_says_what_a_condition_looks_like():
    message = conditions.problems("nonsense")[0]
    assert "${...}" in message and "!=" in message


def test_complaints_are_checked_before_a_run_not_during_one():
    """problems() knows only the shape; what a reference points at is evaluate's."""
    assert conditions.problems("${steps.ghost.status} == 'failed'") == []


def test_problems_never_raises_on_anything_written_in_the_field():
    for text in ("", "   ", "==", "'a' == 'b'", "${a} == ${b} == ${c}",
                 "((((", "${", "}"):
        conditions.problems(text)


def test_a_condition_may_be_written_between_two_literals_and_still_evaluates():
    """Pointless, but it parses, so it must not be a crash."""
    assert conditions.evaluate("'a' == 'a'", scope())
    assert not conditions.evaluate("'a' == 'b'", scope())


# ------------------------------------------------- a grammar this does not have
# `and` used to pass validation in silence and then evaluate false, so the step
# it guarded was skipped and nothing said why. The rule is the same as it was -
# half an expression evaluator is worse than a small complete one - but the
# refusal has to be audible.

def test_and_is_refused_rather_than_quietly_read_as_one_long_literal():
    found = conditions.problems("${steps.a.status} == 'success' "
                                "and ${steps.b.status} == 'failed'")
    assert found and "and" in found[0]


def test_or_is_refused_the_same_way():
    assert conditions.problems("${steps.a.status} == 'a' "
                               "or ${steps.b.status} == 'b'")


def test_the_symbols_are_refused_too():
    assert conditions.problems("${a} == 'x' && ${b} == 'y'")
    assert conditions.problems("${a} == 'x' || ${b} == 'y'")


def test_the_refusal_says_which_word_and_what_to_do_instead():
    """"Neither a reference nor a quoted string" is true and unhelpful when
    what you wrote was `and`."""
    message = conditions.problems("${a} == 'x' and ${b} == 'y'")[0]
    assert "'and'" in message
    assert "two steps" in message


def test_a_literal_ends_at_its_own_closing_quote():
    """Which is the bug underneath: a greedy match ran from the first quote to
    the last and swallowed the operator between them."""
    assert conditions.problems("${a} == 'x' and ${b} == 'y'")


def test_prose_that_merely_contains_a_word_gets_the_general_message():
    """`not` is a word ordinary phrasing uses; a malformed condition should
    not be lectured about an operator nobody used."""
    message = conditions.problems("not an expression")[0]
    assert "neither a ${...} reference nor a quoted string" in message


def test_a_literal_carrying_the_other_quote_type_still_works():
    inner = chr(34) + "it's fine" + chr(34)          # "it's fine"
    assert conditions.problems("${steps.a.status} == " + inner) == []
