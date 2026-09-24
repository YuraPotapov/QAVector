"""Resolving ${...} against a run's scope: the seam that lets steps compose."""

import pytest

from cycle import variables
from domain.cycle import Artifact, CycleRun, StepRun, FAILED, SUCCESS


def scope(**extra):
    run = CycleRun(id="20260916-120000-demo", cycle_id="demo",
                   workspace="/runs/demo", variables={"branch": "main"})
    steps = {
        "pg": StepRun("pg", status=SUCCESS,
                      outputs={"port": 5432, "host": "localhost", "ready": True},
                      artifacts=[Artifact("log", "steps/pg/stdout.log", "pg",
                                          name="out")]),
        "tests": StepRun("tests", status=FAILED, message="3 failed",
                         attempts=2, outputs={}),
    }
    steps.update(extra.pop("steps", {}))
    return variables.scope(run, None, steps, env=extra.pop("env", {"HOME": "/home/x"}))


# ------------------------------------------------------------------- the scope
def test_the_scope_has_the_five_branches_a_reference_can_start_at():
    assert set(scope()) == {"vars", "env", "run", "steps"}


def test_a_run_s_variables_win_over_the_cycle_s_defaults():
    """The cycle says what a variable usually is; the run says what it is now."""
    from cycle.model import parse_cycle
    cycle = parse_cycle({"id": "demo", "variables": {"branch": "main", "jobs": 2},
                         "steps": [{"id": "a", "plugin": "command.shell"}]}, "demo")
    run = CycleRun(id="r", cycle_id="demo", variables={"branch": "release"})
    root = variables.scope(run, cycle, {})
    assert root["vars"] == {"branch": "release", "jobs": 2}


def test_a_step_that_has_not_run_still_appears_with_its_pending_status():
    root = variables.scope(None, None, {"a": StepRun("a")})
    assert root["steps"]["a"]["status"] == "pending"


def test_an_artifact_is_reachable_by_the_name_it_was_given():
    assert scope()["steps"]["pg"]["artifacts"]["out"] == "steps/pg/stdout.log"


def test_an_artifact_with_no_name_falls_back_to_its_filename():
    steps = {"a": StepRun("a", artifacts=[Artifact("log", "steps/a/run.log", "a")])}
    root = variables.scope(None, None, steps)
    assert root["steps"]["a"]["artifacts"]["run.log"] == "steps/a/run.log"


def test_the_scope_reads_the_real_environment_when_none_is_given():
    import os
    os.environ["CYCLE_TEST_MARKER"] = "here"
    try:
        assert variables.scope()["env"]["CYCLE_TEST_MARKER"] == "here"
    finally:
        del os.environ["CYCLE_TEST_MARKER"]


# --------------------------------------------------------------------- resolving
def test_a_whole_reference_keeps_the_value_s_own_type():
    """Without this every port and count arrives as text and is converted back."""
    resolved = variables.resolve("${steps.pg.outputs.port}", scope())
    assert resolved == 5432
    assert isinstance(resolved, int)


def test_a_whole_reference_to_a_boolean_stays_a_boolean():
    assert variables.resolve("${steps.pg.outputs.ready}", scope()) is True


def test_an_embedded_reference_stringifies_because_it_can_do_nothing_else():
    assert variables.resolve("port ${steps.pg.outputs.port}", scope()) == "port 5432"


def test_several_references_in_one_string_are_all_replaced():
    assert variables.resolve("${steps.pg.outputs.host}:${steps.pg.outputs.port}",
                             scope()) == "localhost:5432"


def test_whitespace_inside_the_braces_is_allowed():
    assert variables.resolve("${ vars.branch }", scope()) == "main"


def test_a_string_with_no_reference_comes_back_as_it_was():
    assert variables.resolve("just text", scope()) == "just text"


def test_things_that_are_not_strings_pass_through_untouched():
    root = scope()
    assert variables.resolve(7, root) == 7
    assert variables.resolve(None, root) is None
    assert variables.resolve(True, root) is True


def test_a_mapping_resolves_all_the_way_down():
    resolved = variables.resolve(
        {"cmd": "psql -p ${steps.pg.outputs.port}",
         "env": {"BRANCH": "${vars.branch}"},
         "keep": 7},
        scope())
    assert resolved == {"cmd": "psql -p 5432", "env": {"BRANCH": "main"},
                        "keep": 7}


def test_a_list_resolves_item_by_item():
    assert variables.resolve(["${vars.branch}", 7, "x"], scope()) == ["main", 7, "x"]


def test_a_tuple_stays_a_tuple():
    assert variables.resolve(("${vars.branch}",), scope()) == ("main",)


def test_the_run_s_own_facts_are_reachable():
    root = scope()
    assert variables.resolve("${run.workspace}", root) == "/runs/demo"
    assert variables.resolve("${run.cycle}", root) == "demo"


def test_a_step_s_status_is_reachable():
    assert variables.resolve("${steps.tests.status}", scope()) == "failed"


def test_a_step_s_message_and_attempt_count_are_reachable():
    root = scope()
    assert variables.resolve("${steps.tests.message}", root) == "3 failed"
    assert variables.resolve("${steps.tests.attempts}", root) == 2


# ------------------------------------------------------------------- not there
def test_an_unknown_path_is_an_error_not_an_empty_string():
    """A typo should fail where it was written, not leave a hole in a command."""
    with pytest.raises(variables.ResolveError):
        variables.resolve("${steps.pg.outputs.nope}", scope())


def test_an_unknown_step_is_an_error():
    with pytest.raises(variables.ResolveError):
        variables.resolve("${steps.ghost.status}", scope())


def test_an_unknown_branch_of_the_scope_is_an_error():
    with pytest.raises(variables.ResolveError):
        variables.resolve("${nonsense.x}", scope())


def test_the_error_names_what_was_there_instead():
    """Otherwise somebody compares their file against the scope by eye."""
    with pytest.raises(variables.ResolveError) as caught:
        variables.resolve("${steps.pg.outputs.nope}", scope())
    message = str(caught.value)
    assert "steps.pg.outputs" in message
    assert "port" in message and "host" in message


def test_walking_into_something_that_is_not_a_mapping_says_so():
    with pytest.raises(variables.ResolveError) as caught:
        variables.resolve("${steps.tests.status.deeper}", scope())
    assert "not a mapping" in str(caught.value)


def test_an_error_inside_a_mapping_still_raises():
    with pytest.raises(variables.ResolveError):
        variables.resolve({"a": {"b": ["${vars.missing}"]}}, scope())


def test_an_index_is_not_supported_yet_and_fails_honestly():
    """Better than silently resolving to the wrong thing."""
    with pytest.raises(variables.ResolveError):
        variables.resolve("${steps.pg.outputs.ports[0]}", scope())


# ----------------------------------------------------------------- diagnostics
def test_references_finds_every_path_a_value_mentions():
    found = variables.references({"cmd": "${a.b} and ${c.d}",
                                  "list": ["${e.f}", 7]})
    assert sorted(found) == ["a.b", "c.d", "e.f"]


def test_references_of_something_with_none_is_empty():
    assert variables.references({"a": 1, "b": "plain"}) == []
