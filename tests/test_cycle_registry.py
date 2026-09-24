"""The plugin table: what is in it, what describes one, and the register seam."""

import json

import pytest

from cycle import registry
from cycle.registry import (CyclePlugin, ManagedPlugin, PluginMetadata,
                            PluginResult, field, output)


class _Fake(CyclePlugin):
    def __init__(self, plugin_id="test.fake", **extra):
        self.metadata = PluginMetadata(id=plugin_id, name="Fake", **extra)
        self.ran = []

    def execute(self, context, step):
        self.ran.append(step.id)
        return registry.succeeded(where="here")


@pytest.fixture
def clean():
    """Undo anything a test registered, so the table is the built-ins again."""
    yield
    for plugin_id in list(registry._registered):
        registry.unregister(plugin_id)


# ------------------------------------------------------------------- the table
def test_the_builtins_are_there():
    assert "command.shell" in {plugin.metadata.id
                               for plugin in registry.all_plugins()}


def test_asking_for_a_plugin_that_is_not_there_gives_none_rather_than_raising():
    """Callers are checking; an exception would make every lookup a try block."""
    assert registry.get("does.not.exist") is None


def test_every_builtin_says_who_it_is():
    for plugin in registry.all_plugins():
        assert plugin.metadata.id
        assert plugin.metadata.name
        assert plugin.metadata.version
        assert plugin.metadata.category in registry.CATEGORIES


def test_every_builtin_field_is_a_kind_a_front_end_can_draw():
    for plugin in registry.all_plugins():
        for spec in plugin.metadata.inputs:
            assert spec.kind in registry.KINDS, (plugin.metadata.id, spec.key)


def test_every_builtin_permission_is_one_of_the_declared_ones():
    for plugin in registry.all_plugins():
        for permission in plugin.metadata.permissions:
            assert permission in registry.PERMISSIONS, plugin.metadata.id


def test_no_builtin_declares_the_same_field_twice():
    for plugin in registry.all_plugins():
        keys = [spec.key for spec in plugin.metadata.inputs]
        assert len(keys) == len(set(keys)), plugin.metadata.id


def test_the_table_comes_back_in_a_stable_order():
    assert ([plugin.metadata.id for plugin in registry.all_plugins()]
            == sorted(plugin.metadata.id for plugin in registry.all_plugins()))


# ---------------------------------------------------------------------- describe
def test_what_describe_publishes_survives_being_turned_into_json():
    """It travels to a front-end that cannot import any of this."""
    json.dumps(registry.describe())


def test_a_described_plugin_carries_the_fields_a_form_is_built_from():
    described = {entry["id"]: entry for entry in registry.describe()}
    shell = described["command.shell"]
    keys = {spec["key"] for spec in shell["inputs"]}
    assert "command" in keys
    assert {"key", "label", "kind", "hint", "required", "default", "options"} <= set(
        shell["inputs"][0])


def test_a_described_plugin_says_what_it_produces():
    """So a step reading ${steps.x.outputs.y} can be written before x has run."""
    described = {entry["id"]: entry for entry in registry.describe()}
    assert "exit_code" in {one["key"]
                           for one in described["command.shell"]["outputs"]}


def test_metadata_cannot_be_changed_after_it_is_written():
    """--describe's answer has to be what the run will actually do."""
    with pytest.raises(Exception):
        registry.get("command.shell").metadata.id = "something.else"


# ---------------------------------------------------------------------- register
def test_registering_adds_a_plugin_to_the_table(clean):
    registry.register(_Fake())
    assert registry.get("test.fake") is not None
    assert "test.fake" in {plugin.metadata.id
                           for plugin in registry.all_plugins()}


def test_registering_an_id_that_is_taken_is_refused(clean):
    registry.register(_Fake())
    with pytest.raises(ValueError):
        registry.register(_Fake())


def test_replacing_is_possible_but_has_to_be_asked_for(clean):
    first = _Fake()
    registry.register(first)
    second = _Fake()
    registry.register(second, replace=True)
    assert registry.get("test.fake") is second


def test_a_builtin_can_be_replaced_when_that_is_what_was_asked(clean):
    """The seam has to work for overriding, or it only half exists."""
    registry.register(_Fake("command.shell"), replace=True)
    assert isinstance(registry.get("command.shell"), _Fake)


def test_unregistering_puts_a_replaced_builtin_back(clean):
    registry.register(_Fake("command.shell"), replace=True)
    registry.unregister("command.shell")
    assert not isinstance(registry.get("command.shell"), _Fake)


def test_something_without_metadata_cannot_be_registered(clean):
    with pytest.raises(ValueError):
        registry.register(object())


def test_something_whose_metadata_has_no_id_cannot_be_registered(clean):
    class Nameless(CyclePlugin):
        metadata = PluginMetadata(id="", name="Nameless")
    with pytest.raises(ValueError):
        registry.register(Nameless())


def test_a_registered_plugin_appears_in_describe(clean):
    registry.register(_Fake())
    assert "test.fake" in {entry["id"] for entry in registry.describe()}


# -------------------------------------------------------------- generic problems
def test_a_required_field_that_is_missing_is_reported():
    plugin = _Fake(inputs=(field("who", "Who", required=True),))
    assert plugin.problems({}) == ["Who is required."]


def test_a_required_field_that_is_blank_is_reported():
    plugin = _Fake(inputs=(field("who", "Who", required=True),))
    assert plugin.problems({"who": "   "}) == ["Who is required."]


def test_a_required_field_with_a_default_is_not_required_of_the_author():
    plugin = _Fake(inputs=(field("n", "N", "number", required=True, default=1),))
    assert plugin.problems({}) == []


def test_a_number_that_is_not_a_number_is_reported():
    plugin = _Fake(inputs=(field("n", "N", "number"),))
    assert plugin.problems({"n": "soon"})
    assert plugin.problems({"n": "3"}) == []
    assert plugin.problems({"n": 3.5}) == []


def test_a_choice_outside_its_options_is_reported_with_the_options():
    plugin = _Fake(inputs=(field("how", "How", "choice", options=("a", "b")),))
    found = plugin.problems({"how": "c"})
    assert found and "a, b" in found[0]


def test_an_env_that_is_not_a_mapping_is_reported():
    plugin = _Fake(inputs=(field("env", "Environment", "env"),))
    assert plugin.problems({"env": ["A=1"]})
    assert plugin.problems({"env": {"A": "1"}}) == []


def test_a_setting_the_plugin_does_not_have_is_reported():
    """A quietly ignored `comand:` is a step that silently does nothing."""
    plugin = _Fake(inputs=(field("who", "Who"),))
    found = plugin.problems({"who": "x", "whom": "y"})
    assert found and "'whom'" in found[0]


def test_problems_never_raises_on_anything_a_file_could_contain():
    plugin = _Fake(inputs=(field("n", "N", "number"), field("c", "C", "check")))
    for settings in ({}, None, {"n": None}, {"n": []}, {"c": "yes"},
                     {"n": {"nested": True}}):
        plugin.problems(settings)


def test_a_setting_falls_back_to_the_default_the_plugin_declared():
    """So a default lives in one place rather than two that drift apart."""
    plugin = _Fake(inputs=(field("n", "N", "number", default=7),))
    assert plugin.setting({}, "n") == 7
    assert plugin.setting({"n": 3}, "n") == 3
    assert plugin.setting({"n": ""}, "n") == 7


def test_a_setting_with_no_default_and_no_value_falls_back_to_what_was_asked():
    plugin = _Fake(inputs=(field("n", "N"),))
    assert plugin.setting({}, "n", "fallback") == "fallback"
    assert plugin.setting({}, "not-a-field-at-all", "fallback") == "fallback"


# ------------------------------------------------------------------- the result
def test_a_result_says_whether_it_worked():
    assert PluginResult("success").ok
    assert not PluginResult("failed").ok


def test_the_short_ways_of_writing_a_result():
    good = registry.succeeded("all done", port=5432)
    assert good.ok and good.outputs == {"port": 5432} and good.message == "all done"
    assert not registry.failed("no").ok
    assert registry.skipped("nothing to do").status == "skipped"


def test_a_plugin_that_does_not_implement_execute_says_so():
    class Empty(CyclePlugin):
        metadata = PluginMetadata(id="test.empty", name="Empty")
    with pytest.raises(NotImplementedError):
        Empty().execute(None, None)


def test_prepare_and_cleanup_are_optional():
    """Most plugins have nothing to do in either; they should not have to say so."""
    plugin = _Fake()
    assert plugin.prepare(None, None) is None
    assert plugin.cleanup(None, None) is None


# ------------------------------------------------------------------- long-running
def test_a_managed_plugin_hands_what_it_started_to_the_run_to_hold():
    import tempfile
    from cycle.context import RunContext
    from domain.cycle import CycleRun, CycleStep

    class Service(ManagedPlugin):
        metadata = PluginMetadata(id="test.service", name="Service",
                                  background=True)

        def start(self, context, step):
            return {"pid": 123}

        def stop(self, context, step, handle):
            handle["stopped"] = True

        def status(self, context, step, handle):
            return {"pid": handle["pid"]}

    workspace = tempfile.mkdtemp()
    context = RunContext(CycleRun(id="r", cycle_id="c", workspace=workspace),
                         None, workspace)
    step = CycleStep(id="pg", plugin="test.service")

    result = Service().execute(context, step)
    assert result.ok
    assert result.outputs == {"pid": 123}
    assert [entry[0] for entry in context.held()] == ["pg"]


def test_a_managed_plugin_that_holds_nothing_registers_nothing():
    import tempfile
    from cycle.context import RunContext
    from domain.cycle import CycleRun, CycleStep

    class Nothing(ManagedPlugin):
        metadata = PluginMetadata(id="test.nothing", name="Nothing")

        def start(self, context, step):
            return None

    workspace = tempfile.mkdtemp()
    context = RunContext(CycleRun(id="r", cycle_id="c", workspace=workspace),
                         None, workspace)
    assert Nothing().execute(context, CycleStep(id="a", plugin="x")).ok
    assert context.held() == []
