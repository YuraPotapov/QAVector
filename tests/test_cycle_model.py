"""Parsing a cycle, everything that can be wrong with one, and its shape."""

import pytest

from cycle.model import (CycleError, is_valid, layers, order, parse_cycle,
                         problems, to_document, to_graph)
from domain.cycle import CONTINUE, STOP


def build(steps, **extra):
    """A cycle mapping with sensible defaults, so a test says only what it means."""
    raw = {"id": "demo", "steps": steps}
    raw.update(extra)
    return raw


def step(step_id, **extra):
    entry = {"id": step_id, "plugin": "command.shell"}
    entry.update(extra)
    return entry


def diamond():
    """a -> {b, c} -> d, the smallest graph with something to say about it."""
    return build([step("a"),
                  step("b", needs=["a"]),
                  step("c", needs=["a"]),
                  step("d", needs=["b", "c"])])


# ---------------------------------------------------------------------- parsing
def test_a_cycle_keeps_what_the_file_said():
    raw = build([step("a", label="First", timeout=30)],
                name="Nightly", description="Every night", version=2,
                variables={"branch": "main"})
    cycle = parse_cycle(raw, "demo", source="/tmp/demo.yaml")

    assert cycle.id == "demo"
    assert cycle.name == "Nightly"
    assert cycle.description == "Every night"
    assert cycle.version == 2
    assert cycle.variables == {"branch": "main"}
    assert cycle.source == "/tmp/demo.yaml"
    assert [one.id for one in cycle.steps] == ["a"]
    assert cycle.steps[0].label == "First"
    assert cycle.steps[0].timeout == 30.0


def test_a_step_without_a_label_is_called_by_its_id():
    cycle = parse_cycle(build([step("checkout")]), "demo")
    assert cycle.steps[0].title == "checkout"


def test_a_cycle_without_a_name_is_called_by_its_id():
    assert parse_cycle(build([step("a")]), "demo").title == "demo"


def test_needs_may_be_written_as_one_name_or_as_a_list():
    cycle = parse_cycle(build([step("a"),
                               step("b", needs="a"),
                               step("c", needs=["a", "b"])]), "demo")
    assert cycle.step("b").needs == ("a",)
    assert cycle.step("c").needs == ("a", "b")


def test_a_step_remembers_where_it_sat_in_the_file():
    cycle = parse_cycle(build([step("a"), step("b"), step("c")]), "demo")
    assert [one.source_index for one in cycle.steps] == [0, 1, 2]


def test_retry_defaults_to_one_attempt_and_no_delay():
    cycle = parse_cycle(build([step("a")]), "demo")
    assert cycle.steps[0].retry_attempts == 1
    assert cycle.steps[0].retry_delay == 0.0


def test_retry_is_read_from_its_own_mapping():
    cycle = parse_cycle(
        build([step("a", retry={"attempts": 3, "delay": 1.5})]), "demo")
    assert cycle.steps[0].retry_attempts == 3
    assert cycle.steps[0].retry_delay == 1.5


def test_on_failure_defaults_to_stopping_the_run():
    cycle = parse_cycle(build([step("a")]), "demo")
    assert cycle.steps[0].on_failure == STOP
    assert parse_cycle(build([step("a", on_failure=CONTINUE)]),
                       "demo").steps[0].on_failure == CONTINUE


def test_an_absent_timeout_means_no_deadline():
    cycle = parse_cycle(build([step("a"), step("b", timeout="")]), "demo")
    assert cycle.step("a").timeout is None
    assert cycle.step("b").timeout is None


def test_step_returns_none_for_an_id_that_is_not_there():
    assert parse_cycle(build([step("a")]), "demo").step("nope") is None


# ------------------------------------------------------- shapes that cannot hold
def test_a_cycle_file_has_to_be_a_mapping():
    with pytest.raises(CycleError):
        parse_cycle(["not", "a", "mapping"], "demo")


def test_steps_have_to_be_a_list():
    with pytest.raises(CycleError):
        parse_cycle({"id": "demo", "steps": {"a": {}}}, "demo")


def test_a_step_has_to_be_a_mapping():
    with pytest.raises(CycleError):
        parse_cycle(build(["just a string"]), "demo")


def test_a_step_without_an_id_cannot_be_represented():
    with pytest.raises(CycleError):
        parse_cycle(build([{"plugin": "command.shell"}]), "demo")


def test_with_has_to_be_a_mapping():
    with pytest.raises(CycleError):
        parse_cycle(build([step("a", **{"with": ["a", "b"]})]), "demo")


def test_variables_have_to_be_a_mapping():
    with pytest.raises(CycleError):
        parse_cycle(build([step("a")], variables=["branch"]), "demo")


def test_the_id_in_the_file_has_to_match_the_file_it_is_in():
    """Two answers to "what is this called" is a bug waiting for a rename."""
    with pytest.raises(CycleError):
        parse_cycle(build([step("a")]), "something_else")


def test_a_file_with_no_id_of_its_own_takes_the_one_it_was_asked_for():
    cycle = parse_cycle({"steps": [step("a")]}, "from_the_filename")
    assert cycle.id == "from_the_filename"


def test_a_cycle_with_no_steps_key_parses_and_is_then_complained_about():
    cycle = parse_cycle({"id": "demo"}, "demo")
    assert cycle.steps == []
    assert any("at least one step" in message for message in problems(cycle))


# ------------------------------------------------------------------- validation
def test_a_good_cycle_has_nothing_wrong_with_it():
    raw = diamond()
    assert problems(parse_cycle(raw, "demo"), raw=raw) == []
    assert is_valid(parse_cycle(raw, "demo"), raw=raw)


def test_two_steps_with_one_id_are_reported():
    raw = build([step("a"), step("a")])
    assert any("share this id" in message
               for message in problems(parse_cycle(raw, "demo"), raw=raw))


def test_a_step_that_needs_something_that_is_not_there_is_reported():
    raw = build([step("a", needs=["ghost"])])
    found = problems(parse_cycle(raw, "demo"), raw=raw)
    assert any("needs 'ghost'" in message for message in found)


def test_a_step_that_needs_itself_is_reported_in_its_own_words():
    """Not as a loop: "a -> a wait for each other" reads as a puzzle."""
    raw = build([step("a", needs=["a"])])
    found = problems(parse_cycle(raw, "demo"), raw=raw)
    assert any("needs itself" in message for message in found)
    assert not any("wait for each other" in message for message in found)


def test_a_ring_names_the_steps_going_round_it():
    raw = build([step("a", needs=["b"]), step("b", needs=["a"])])
    found = problems(parse_cycle(raw, "demo"), raw=raw)
    loops = [message for message in found if "wait for each other" in message]
    assert loops, found
    assert "a -> b -> a" in loops[0]


def test_a_longer_ring_is_found_too():
    raw = build([step("a", needs=["c"]), step("b", needs=["a"]),
                 step("c", needs=["b"])])
    found = problems(parse_cycle(raw, "demo"), raw=raw)
    assert any("wait for each other" in message for message in found)


def test_a_loop_is_reported_with_an_arrow_spelt_out_not_drawn():
    """A literal arrow glyph renders as a box in DejaVu and as ASCII on Windows."""
    raw = build([step("a", needs=["b"]), step("b", needs=["a"])])
    for message in problems(parse_cycle(raw, "demo"), raw=raw):
        assert all(ord(char) < 0x2100 for char in message)


def test_a_step_with_no_plugin_is_reported():
    raw = build([{"id": "a"}])
    assert any("no plugin" in message
               for message in problems(parse_cycle(raw, "demo"), raw=raw))


def test_an_unusable_step_id_is_reported():
    raw = build([step("has a space"), step("has.a.dot")])
    found = problems(parse_cycle(raw, "demo"), raw=raw)
    assert sum("not a usable step id" in message for message in found) == 2


def test_a_misspelt_step_key_is_reported_rather_than_ignored():
    """A silently dropped `timout:` is a step that quietly never times out."""
    raw = build([step("a", timout=5)])
    found = problems(parse_cycle(raw, "demo"), raw=raw)
    assert any("unknown key 'timout'" in message for message in found)


def test_a_misspelt_cycle_key_is_reported():
    raw = build([step("a")], stpes=[])
    assert any("unknown key 'stpes'" in message
               for message in problems(parse_cycle(raw, "demo"), raw=raw))


def test_an_unknown_key_is_only_findable_from_the_raw_mapping():
    """Parsing has already dropped it, so without `raw` there is nothing to see."""
    raw = build([step("a", timout=5)])
    assert problems(parse_cycle(raw, "demo")) == []


def test_retry_backoff_is_accepted_and_says_it_does_nothing():
    """Better than behaving as though the file had not asked for it."""
    raw = build([step("a", retry={"attempts": 2, "backoff": "exponential"})])
    found = problems(parse_cycle(raw, "demo"), raw=raw)
    assert any("backoff is not implemented" in message for message in found)


def test_a_bad_on_failure_is_reported_with_what_it_could_be():
    raw = build([step("a", on_failure="explode")])
    found = problems(parse_cycle(raw, "demo"), raw=raw)
    assert any("on_failure is 'explode'" in message for message in found)


def test_a_timeout_of_zero_is_reported_because_it_reads_as_a_mistake():
    raw = build([step("a", timeout=0)])
    assert any("timeout is 0" in message
               for message in problems(parse_cycle(raw, "demo"), raw=raw))


def test_fewer_than_one_attempt_is_reported():
    raw = build([step("a", retry={"attempts": 0})])
    assert any("retry.attempts is 0" in message
               for message in problems(parse_cycle(raw, "demo"), raw=raw))


def test_a_negative_retry_delay_is_reported():
    raw = build([step("a", retry={"delay": -1})])
    assert any("retry.delay cannot be negative" in message
               for message in problems(parse_cycle(raw, "demo"), raw=raw))


def test_a_malformed_condition_is_reported_at_load_time():
    """Not at three in the morning, in the middle of the run that reached it."""
    raw = build([step("a", **{"if": "not an expression"})])
    found = problems(parse_cycle(raw, "demo"), raw=raw)
    assert any("neither a ${...} reference nor a quoted string" in message
               for message in found)


def test_a_good_condition_passes():
    raw = build([step("a", needs=["b"], **{"if": "${steps.b.status} == 'failed'"}),
                 step("b")])
    assert problems(parse_cycle(raw, "demo"), raw=raw) == []


def test_a_condition_reading_a_step_it_does_not_wait_for_is_reported():
    """Otherwise it is evaluated while that step is still pending, and the
    step is silently skipped every time for a reason that looks like the
    condition being false."""
    raw = build([step("a", **{"if": "${steps.b.status} == 'failed'"}), step("b")])
    found = problems(parse_cycle(raw, "demo"), raw=raw)
    assert any("does not need b" in message for message in found)
    assert any("Add b to needs" in message for message in found)


def test_a_condition_reading_something_other_than_a_step_is_not_a_race():
    raw = build([step("a", **{"if": "${vars.deploy} == 'yes'"})])
    assert problems(parse_cycle(raw, "demo"), raw=raw) == []


def test_a_condition_reading_a_step_that_is_waited_for_transitively_is_still_reported():
    """`needs` is what the scheduler reads, so it is what has to name it."""
    raw = build([step("a"),
                 step("b", needs=["a"]),
                 step("c", needs=["b"], **{"if": "${steps.a.status} == 'success'"})])
    found = problems(parse_cycle(raw, "demo"), raw=raw)
    assert any("does not need a" in message for message in found)


def test_every_complaint_comes_back_at_once():
    """Someone fixing a file wants the list, not the first line and another run."""
    raw = build([step("a", needs=["ghost"], on_failure="explode", timeout=-1)])
    assert len(problems(parse_cycle(raw, "demo"), raw=raw)) >= 3


def test_validation_never_raises_on_a_broken_cycle():
    raw = build([step("a", needs=["b"]), step("b", needs=["a"]),
                 step("a", **{"if": "junk"})])
    problems(parse_cycle(raw, "demo"), raw=raw)      # must not raise


class _Registry:
    """A stand-in for cycle.registry, so this file needs no plugins to exist."""

    def __init__(self, known, complaints=()):
        self._known = {}
        for plugin_id in known:
            self._known[plugin_id] = _Plugin(plugin_id, complaints)

    def get(self, plugin_id):
        return self._known.get(plugin_id)

    def all_plugins(self):
        return list(self._known.values())


class _Plugin:
    def __init__(self, plugin_id, complaints):
        self.metadata = type("Meta", (), {"id": plugin_id})()
        self._complaints = list(complaints)

    def problems(self, settings):
        return list(self._complaints)


def test_an_unknown_plugin_is_reported_with_the_ones_that_exist():
    raw = build([step("a", plugin="does.not.exist")])
    registry = _Registry(["command.shell", "report.json"])
    found = problems(parse_cycle(raw, "demo"), registry=registry, raw=raw)
    assert any("unknown plugin 'does.not.exist'" in message for message in found)
    assert any("command.shell" in message for message in found)


def test_a_plugin_gets_to_complain_about_its_own_settings():
    raw = build([step("a")])
    registry = _Registry(["command.shell"], complaints=["command is required."])
    found = problems(parse_cycle(raw, "demo"), registry=registry, raw=raw)
    assert "a: command is required." in found


def test_without_a_registry_plugin_ids_go_unchecked():
    """What --describe wants: it reads every cycle and should not load plugins."""
    raw = build([step("a", plugin="anything.at.all")])
    assert problems(parse_cycle(raw, "demo"), raw=raw) == []


# ------------------------------------------------------------------------ shape
def test_a_chain_puts_one_step_in_each_layer():
    cycle = parse_cycle(build([step("a"), step("b", needs=["a"]),
                               step("c", needs=["b"])]), "demo")
    assert layers(cycle) == {"a": 0, "b": 1, "c": 2}


def test_a_diamond_shares_a_layer_between_its_two_branches():
    assert layers(parse_cycle(diamond(), "demo")) == {"a": 0, "b": 1, "c": 1,
                                                      "d": 2}


def test_a_step_sits_after_the_deepest_thing_it_waits_for():
    """Longest path, not shortest: d waits for b, so it cannot sit beside it."""
    cycle = parse_cycle(build([step("a"),
                               step("b", needs=["a"]),
                               step("d", needs=["a", "b"])]), "demo")
    assert layers(cycle)["d"] == 2


def test_steps_with_no_dependencies_all_start_at_the_front():
    cycle = parse_cycle(build([step("a"), step("b"), step("c")]), "demo")
    assert layers(cycle) == {"a": 0, "b": 0, "c": 0}


def test_order_groups_the_steps_by_layer_in_file_order():
    cycle = parse_cycle(diamond(), "demo")
    assert [[one.id for one in layer] for layer in order(cycle)] == [
        ["a"], ["b", "c"], ["d"]]


def test_a_ring_still_produces_layers_so_a_broken_cycle_can_be_drawn():
    """Seeing it is how somebody fixes it; problems() is what refuses to run it."""
    cycle = parse_cycle(build([step("a", needs=["b"]), step("b", needs=["a"])]),
                        "demo")
    assert set(layers(cycle)) == {"a", "b"}


# ------------------------------------------------------------------ wire payload
def test_the_graph_carries_a_node_for_every_step_and_an_edge_for_every_need():
    graph = to_graph(parse_cycle(diamond(), "demo"))
    assert [node["id"] for node in graph["nodes"]] == ["a", "b", "c", "d"]
    assert [(edge["from"], edge["to"]) for edge in graph["edges"]] == [
        ("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]


def test_every_node_carries_its_place_in_the_grid():
    graph = to_graph(parse_cycle(diamond(), "demo"))
    places = {node["id"]: (node["layer"], node["row"]) for node in graph["nodes"]}
    assert places == {"a": (0, 0), "b": (1, 0), "c": (1, 1), "d": (2, 0)}


def test_rows_within_a_layer_follow_the_order_of_the_file():
    raw = build([step("root"),
                 step("second", needs=["root"]),
                 step("first", needs=["root"])])
    graph = to_graph(parse_cycle(raw, "demo"))
    rows = {node["id"]: node["row"] for node in graph["nodes"]}
    assert rows["second"] == 0 and rows["first"] == 1


def test_the_graph_carries_what_a_node_has_to_show():
    raw = build([step("a", label="Backend tests", timeout=30, disabled=True,
                      on_failure="continue", retry={"attempts": 3},
                      **{"if": "${steps.b.status} == 'failed'"}),
                 step("b")])
    node = to_graph(parse_cycle(raw, "demo"))["nodes"][0]
    assert node["label"] == "Backend tests"
    assert node["plugin"] == "command.shell"
    assert node["timeout"] == 30.0
    assert node["retry"] == 3
    assert node["disabled"] is True
    assert node["on_failure"] == "continue"
    assert node["condition"] == "${steps.b.status} == 'failed'"


def test_the_graph_is_the_same_every_time_it_is_built():
    """The canvas diffs by key across pushes; a moving payload would defeat that."""
    cycle = parse_cycle(diamond(), "demo")
    assert to_graph(cycle) == to_graph(cycle)


def test_the_graph_survives_being_turned_into_json():
    """It travels on the event stream and out of --cycle-show."""
    import json
    json.dumps(to_graph(parse_cycle(diamond(), "demo")))


def test_a_cycle_with_one_step_has_a_graph_with_no_edges():
    graph = to_graph(parse_cycle(build([step("only")]), "demo"))
    assert len(graph["nodes"]) == 1
    assert graph["edges"] == []


# --------------------------------------------------------------- the project
def test_a_cycle_can_say_which_project_it_belongs_to():
    raw = build([step("a")], project="Customer Portal")
    assert parse_cycle(raw, "demo").project == "Customer Portal"
    assert problems(parse_cycle(raw, "demo"), raw=raw) == []


def test_a_cycle_with_no_project_says_so_by_saying_nothing():
    assert parse_cycle(build([step("a")]), "demo").project == ""


def test_the_project_travels_in_the_graph_the_canvas_draws():
    raw = build([step("a")], project="Portal")
    assert to_graph(parse_cycle(raw, "demo"))["project"] == "Portal"


def test_a_project_nothing_recognises_is_not_an_error():
    """The front-end shows it as unassigned; a file is not wrong for naming a
    project that only exists on somebody else's machine."""
    raw = build([step("a")], project="Whatever They Called It")
    assert problems(parse_cycle(raw, "demo"), raw=raw) == []


# -------------------------------------------------------------- the document
# What to draw and what to change are two shapes of the same file. A front-end
# that cannot parse YAML - which is every front-end here, by design - edits the
# document and hands it back.

def test_the_document_round_trips_through_the_parser():
    raw = build([step("a", **{"with": {"command": "make"}}),
                 step("b", needs=["a"], timeout=60,
                      retry={"attempts": 2, "delay": 1.5},
                      on_failure="continue", label="Second",
                      **{"if": "${steps.a.status} == 'success'"})],
                name="Nightly", description="Each night", project="Portal",
                variables={"branch": "main"})
    cycle = parse_cycle(raw, "demo")

    from cycle.model import to_document
    assert parse_cycle(to_document(cycle), "demo") == cycle


def test_the_document_carries_only_what_was_actually_written():
    """Round-tripping a file should not quietly fill it with defaults its
    author chose not to say."""
    from cycle.model import to_document

    document = to_document(parse_cycle(build([step("a")]), "demo"))
    assert set(document) == {"id", "steps"}
    assert set(document["steps"][0]) == {"id", "plugin"}


def test_the_document_keeps_a_step_s_own_settings():
    from cycle.model import to_document

    raw = build([step("a", **{"with": {"command": "make", "dir": "src"}})])
    document = to_document(parse_cycle(raw, "demo"))
    assert document["steps"][0]["with"] == {"command": "make", "dir": "src"}


def test_the_document_keeps_the_steps_in_the_order_of_the_file():
    from cycle.model import to_document

    raw = build([step("z"), step("a"), step("m")])
    document = to_document(parse_cycle(raw, "demo"))
    assert [one["id"] for one in document["steps"]] == ["z", "a", "m"]


def test_a_disabled_step_says_so_in_the_document():
    from cycle.model import to_document

    raw = build([step("a", disabled=True)])
    assert to_document(parse_cycle(raw, "demo"))["steps"][0]["disabled"] is True


def test_the_document_survives_being_turned_into_json():
    """It travels to a front-end that cannot import any of this."""
    import json
    from cycle.model import to_document

    json.dumps(to_document(parse_cycle(diamond(), "demo")))


# --------------------------------------------------- a variable that is a path
# Most of a cycle's variables are paths - the checkout it works in, where the
# rules are written down. Saying so buys one thing and changes nothing else:
# the value is ordinary text and is read as such.
def test_a_path_keeps_its_value_where_every_other_value_lives():
    cycle = parse_cycle({"id": "c", "steps": [], "variables": {
        "repo": {"kind": "path", "value": "/src/app"}}}, "c")

    assert cycle.variables["repo"] == "/src/app"
    assert cycle.paths == ("repo",)
    assert cycle.secrets == ()


def test_a_path_reads_the_same_as_any_other_variable():
    """`${vars.repo}` gets a string whatever it was declared as - nothing at
    run time treats one differently, and that is the point."""
    from cycle import variables

    cycle = parse_cycle({"id": "c", "steps": [], "variables": {
        "repo": {"kind": "path", "value": "/src/app"},
        "plain": "/src/other"}}, "c")
    scope = variables.scope(cycle=cycle)

    assert scope["vars"]["repo"] == scope["vars"]["plain"].replace("other",
                                                                   "app")


def test_a_path_with_no_value_is_empty_rather_than_absent():
    cycle = parse_cycle({"id": "c", "steps": [], "variables": {
        "repo": {"kind": "path"}}}, "c")

    assert cycle.variables["repo"] == ""
    assert cycle.paths == ("repo",)


def test_a_path_is_written_back_as_it_was_declared():
    """A round trip through the editor must not quietly demote it - it would
    lose its chooser on the next open, which reads as the feature breaking."""
    cycle = parse_cycle({"id": "c", "steps": [], "variables": {
        "repo": {"kind": "path", "value": "/src/app"}}}, "c")

    assert to_document(cycle)["variables"] == {
        "repo": {"kind": "path", "value": "/src/app"}}


def test_the_graph_says_which_variables_are_paths():
    """The canvas and the form are given it the same way they are given the
    secrets, so neither has to work it out."""
    cycle = parse_cycle({"id": "c", "steps": [], "variables": {
        "repo": {"kind": "path", "value": "/src/app"},
        "token": {"secret": True}}}, "c")
    graph = to_graph(cycle)

    assert graph["paths"] == ["repo"]
    assert graph["secrets"] == ["token"]


@pytest.mark.parametrize("declared, says", [
    ({"kind": "nonsense"}, "neither secret"),
    ({}, "neither secret"),
    ({"secret": True, "kind": "path"}, "both secret and path"),
    ({"kind": "path", "default": "/src"}, "takes a value, not a default"),
])
def test_a_variable_that_declares_nonsense_is_refused(declared, says):
    with pytest.raises(CycleError) as raised:
        parse_cycle({"id": "c", "steps": [], "variables": {"x": declared}}, "c")
    assert says in str(raised.value)


def test_a_secret_still_cannot_carry_its_value():
    """The rule the whole split exists for, unchanged by any of this."""
    with pytest.raises(CycleError):
        parse_cycle({"id": "c", "steps": [], "variables": {
            "token": {"secret": True, "value": "sekret"}}}, "c")
