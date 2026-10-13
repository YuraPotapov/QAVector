"""Reading and writing scenario files.

The property that matters throughout: anything this module writes must come back
out of the compiler as the steps that went in. A scenario that does not compile
is not a scenario, and finding that out at the end of a launch - minutes later,
in front of whoever was watching - is the failure these tests exist to prevent.
"""

import os

import pytest

from engine import compiler, flowfile, loader

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "flows")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A writable tree in front of the fixture tree, as an installed build has."""
    user = tmp_path / "user-flows"
    (user / "scenarios").mkdir(parents=True)
    monkeypatch.setattr(loader.runtime_paths, "flows_search_path",
                        lambda: [str(user), FIXTURES])
    return user


def _roundtrip(steps, meta=None):
    """Render steps, parse them back, and return the parsed Steps."""
    text = flowfile.render(dict(meta or {}, id="rt"), steps)
    import yaml
    return [compiler.parse_step(raw) for raw in yaml.safe_load(text)["steps"]], text


# ------------------------------------------------------------------- rendering

def test_a_selector_step_renders_as_shorthand():
    parsed, text = _roundtrip([{"action": "click", "target": "menu_settings"}])
    assert "  - click: \"menu_settings\"" in text
    assert parsed[0].action == "click" and parsed[0].target == "menu_settings"


def test_a_target_and_value_step_renders_as_an_inline_mapping():
    parsed, text = _roundtrip([{"action": "fill", "target": "login_input",
                                "value": "admin"}])
    assert '  - fill: {target: "login_input", value: "admin"}' in text
    assert parsed[0].target == "login_input" and parsed[0].value == "admin"


def test_a_service_step_renders_as_shorthand():
    parsed, text = _roundtrip([{"action": "service_restart", "target": "Storefront/Web"}])
    assert '  - service_restart: "Storefront/Web"' in text
    assert parsed[0].target == "Storefront/Web"


def test_a_service_wait_keeps_its_value_through_the_round_trip():
    # It takes a {target, value} mapping like `fill` does, so it must be written
    # as one - rendered as a bare target its regex would be silently dropped.
    parsed, text = _roundtrip([{"action": "wait_for_out", "target": "Storefront/Web",
                                "value": ".+:8069"}])
    assert '  - wait_for_out: {target: "Storefront/Web", value: ".+:8069"}' in text
    assert parsed[0].value == ".+:8069"


def test_a_value_only_step_puts_the_argument_in_value():
    parsed, _ = _roundtrip([{"action": "press", "value": "Enter"}])
    assert parsed[0].value == "Enter" and parsed[0].target is None


def test_a_timeout_forces_the_verbose_form():
    # Shorthand has nowhere to put a timeout except on {target, value} actions.
    parsed, text = _roundtrip([{"action": "assert_not_visible",
                                "target": "menu_settings", "timeout": 2500}])
    assert "  - type: assert_not_visible" in text
    assert parsed[0].timeout == 2500


def test_a_timeout_stays_inline_where_shorthand_supports_it():
    parsed, text = _roundtrip([{"action": "assert_text_contains", "target": "breadcrumb",
                                "value": "UITEST", "timeout": 2000}])
    assert "timeout: 2000}" in text
    assert parsed[0].timeout == 2000


def test_state_and_retry_survive_the_round_trip():
    parsed, _ = _roundtrip([{"action": "wait_for", "target": "dtop_confirm",
                             "state": "detached", "timeout": 20000,
                             "retry": {"attempts": 15, "delay": 1}}])
    assert parsed[0].state == "detached"
    assert parsed[0].retry == {"attempts": 15, "delay": 1}


def test_use_steps_render_as_shorthand():
    parsed, text = _roundtrip([{"action": "use", "target": "auth.login"}])
    assert '  - use: "auth.login"' in text
    assert parsed[0].action == "use" and parsed[0].target == "auth.login"


def test_values_are_always_quoted():
    """An unquoted 100 parses as an int, and `100 in text` raises TypeError."""
    parsed, text = _roundtrip([{"action": "fill", "target": "amount", "value": "100"},
                               {"action": "assert_title", "value": "no"}])
    assert 'value: "100"' in text
    assert parsed[0].value == "100"
    assert parsed[1].value == "no"       # not False


def test_a_quote_in_a_value_does_not_break_the_document():
    parsed, _ = _roundtrip([{"action": "assert_title", "value": 'say "hi"'}])
    assert parsed[0].value == 'say "hi"'


def test_a_scenario_with_no_steps_is_still_a_valid_document():
    import yaml
    text = flowfile.render({"id": "empty"}, [])
    assert yaml.safe_load(text)["steps"] == []      # not None


def test_the_rendered_document_reads_like_the_tree_it_joins():
    _, text = _roundtrip([{"action": "use", "target": "auth.login"},
                          {"action": "assert_visible", "target": "dashboard"}],
                         meta={"name": "Recorded run", "tags": ["smoke"]})
    assert text.splitlines()[:4] == ['id: rt', 'name: "Recorded run"',
                                     'tags: [smoke]', 'steps:']


# ------------------------------------------------------------------------- ids

def test_a_dotted_id_is_sanitised():
    # `my.thing` maps to flows/my/thing.yaml, so a scenario saved under that name
    # would be discovered and then never found again.
    assert flowfile.safe_id("my.thing") == "my_thing"
    assert flowfile.safe_id("Access / Admin") == "Access_Admin"
    assert flowfile.safe_id("   ") == "scenario"


def test_saving_sanitises_the_id_it_was_given(tree):
    result = flowfile.save("my.thing", steps=[{"action": "assert_visible",
                                               "target": "dashboard"}])
    assert result["ok"] and result["id"] == "my_thing"
    assert os.path.exists(os.path.join(str(tree), "scenarios", "my_thing.yaml"))


# ---------------------------------------------------------------------- saving

def test_a_saved_scenario_is_discoverable_and_compiles(tree):
    flowfile.save("recorded", meta={"name": "Recorded", "tags": ["smoke"]},
                  steps=[{"action": "use", "target": "auth.login"},
                         {"action": "assert_visible", "target": "dashboard"}])
    assert "recorded" in loader.discover_scenarios()
    # A context is needed because auth.login templates {{env.origin}}; the run
    # supplies the real one, so checking uses the same stand-in the writer does.
    steps = compiler.compile_scenario("recorded", selectors=loader.load_selectors(),
                                      ctx=flowfile.CHECK_CONTEXT)
    assert [s.action for s in steps][-1] == "assert_visible"


def test_nothing_is_written_when_it_does_not_compile(tree):
    result = flowfile.save("broken", steps=[{"action": "nonsense", "target": "x"}])
    assert not result["ok"]
    assert "unknown action" in result["problems"][0]
    assert not os.path.exists(os.path.join(str(tree), "scenarios", "broken.yaml"))


def test_a_missing_use_target_is_caught_before_the_file_exists(tree):
    result = flowfile.save("dangling", steps=[{"action": "use", "target": "no.such"}])
    assert not result["ok"]
    assert not os.path.exists(os.path.join(str(tree), "scenarios", "dangling.yaml"))


def test_overwriting_keeps_a_backup(tree):
    path = os.path.join(str(tree), "scenarios", "keep.yaml")
    flowfile.save("keep", meta={"name": "First"},
                  steps=[{"action": "assert_visible", "target": "dashboard"}])
    flowfile.save("keep", meta={"name": "Second"},
                  steps=[{"action": "assert_visible", "target": "dashboard"}])
    assert "Second" in open(path, encoding="utf-8").read()
    assert "First" in open(path + ".bak", encoding="utf-8").read()


def test_saving_can_use_raw_yaml_text(tree):
    result = flowfile.save("typed", yaml_text=(
        "id: typed\nname: Typed by hand\ntags: []\nsteps:\n"
        "  - assert_visible: dashboard\n"))
    assert result["ok"]
    assert loader.load_flow("typed").name == "Typed by hand"


def test_raw_yaml_is_validated_too(tree):
    result = flowfile.save("typed", yaml_text="id: typed\nsteps:\n  - wat: x\n")
    assert not result["ok"]


# -------------------------------------------------------- reading and deleting

def test_describe_carries_both_the_text_and_the_steps(tree):
    flowfile.save("shown", meta={"name": "Shown", "tags": ["smoke"]},
                  steps=[{"action": "click", "target": "menu_settings"}])
    payload = flowfile.describe_flow("shown")
    assert payload["writable"] and payload["source"] == "user"
    assert payload["meta"]["name"] == "Shown"
    assert payload["steps"][0] == {"action": "click", "target": "menu_settings",
                                   "value": None, "state": None, "timeout": None,
                                   "retry": None, "doc": None}
    assert "click:" in payload["yaml"]
    assert payload["problems"] == []


def test_a_bundled_scenario_reads_as_not_writable(tree):
    payload = flowfile.describe_flow("demo_smoke")
    assert not payload["writable"] and payload["source"] == "bundled"


def test_a_broken_file_still_opens_with_its_problems(tree):
    # The one file someone most needs to open is the one that will not compile.
    path = os.path.join(str(tree), "scenarios", "bad.yaml")
    open(path, "w", encoding="utf-8").write("id: bad\nsteps:\n  - wat: x\n")
    payload = flowfile.describe_flow("bad")
    assert payload["problems"] and "unknown action" in payload["problems"][0]
    assert payload["yaml"].startswith("id: bad")


def test_unresolved_references_are_reported_without_being_errors(tree):
    flowfile.save("refs", steps=[{"action": "use", "target": "auth.login"},
                                 {"action": "click", "target": ".raw_css"},
                                 {"action": "click", "target": "not_a_known_name"}])
    payload = flowfile.describe_flow("refs")
    assert payload["problems"] == []                       # it still compiles
    assert payload["unresolved"]["selectors"] == ["not_a_known_name"]
    assert payload["unresolved"]["use"] == []


def test_deleting_a_user_scenario_removes_it(tree):
    flowfile.save("gone", steps=[{"action": "assert_visible", "target": "dashboard"}])
    assert flowfile.delete("gone")["ok"]
    assert "gone" not in loader.discover_scenarios()


def test_a_bundled_scenario_cannot_be_deleted(tree):
    # It would come back on the next upgrade, and under /opt it is not even
    # writable - so say why instead of failing with a permission error.
    result = flowfile.delete("demo_smoke")
    assert not result["ok"]
    assert "duplicate it instead" in result["problems"][0]
    assert os.path.exists(loader.flow_path("demo_smoke"))


# -------------------------------------------------------------------- importing

def test_importing_validates_and_names_from_the_file(tree, tmp_path):
    source = tmp_path / "shared.scenario.yaml"
    source.write_text("id: whatever\nname: Shared\ntags: []\nsteps:\n"
                      "  - assert_visible: dashboard\n", encoding="utf-8")
    result = flowfile.import_file(str(source))
    assert result["ok"] and result["id"] == "shared_scenario"
    assert loader.load_flow("shared_scenario").name == "Shared"


def test_importing_something_that_does_not_compile_is_refused(tree, tmp_path):
    source = tmp_path / "bad.yaml"
    source.write_text("id: bad\nsteps:\n  - wat: x\n", encoding="utf-8")
    assert not flowfile.import_file(str(source))["ok"]
    assert not os.path.exists(os.path.join(str(tree), "scenarios", "bad.yaml"))


# ----------------------------------------------------------------------- doc:

def test_a_doc_rides_along_with_a_shorthand_step():
    parsed, text = _roundtrip([{"action": "click", "target": "new_button",
                                "doc": "Press New to open an empty form"}])
    assert '  - click: "new_button"\n    doc: "Press New to open an empty form"' in text
    assert parsed[0].doc == "Press New to open an empty form"


def test_a_doc_rides_along_with_a_verbose_step():
    parsed, _text = _roundtrip([{"action": "wait_for", "target": "form_view",
                                 "state": "visible", "doc": "The form opens"}])
    assert parsed[0].state == "visible" and parsed[0].doc == "The form opens"


def test_a_blank_doc_is_left_out():
    _parsed, text = _roundtrip([{"action": "click", "target": "a", "doc": "  "}])
    assert "doc:" not in text


def test_the_editor_sees_a_step_s_doc():
    assert flowfile.step_to_json({"click": "a", "doc": "Press A"})["doc"] == "Press A"
