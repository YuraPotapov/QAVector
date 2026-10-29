"""Where Launch Sessions is: environments, configurations, working copies, variables."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QSettings

from cms_gui import core, launch, launchcontroller as lc, launchprojectsfile as lpf, \
    launchpresetsfile as lps
from cms_gui.settings import Settings


def _inventory():
    return core.Inventory({
        "envs": [{"alias": "localhost:8069", "value": "localhost:8069",
                  "origin": "http://localhost:8069", "count": 2},
                 {"alias": "localhost:8070", "value": "localhost:8070",
                  "origin": "http://localhost:8070", "count": 1},
                 {"alias": "staging", "value": "https://staging.example.invalid/",
                  "origin": "https://staging.example.invalid", "count": 1}],
        "users": [{"env": "localhost:8069", "login": "admin"},
                  {"env": "localhost:8069", "login": "agent"},
                  {"env": "localhost:8070", "login": "admin"},
                  {"env": "https://staging.example.invalid/", "login": "agent"}]})


@pytest.fixture
def make(qapp, tmp_path):
    """A controller on files and settings of its own; call it to build one."""
    def build(configs=None, settings=None):
        if settings is None:
            settings = Settings()
            settings._qs = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
        if configs is not None:
            (tmp_path / "configs.json").write_text(json.dumps(configs), encoding="utf-8")
        registry = lpf.Registry(str(tmp_path / lpf.FILE_NAME))
        return lc.LaunchController(settings, registry,
                                   str(tmp_path / lps.FILE_NAME),
                                   str(tmp_path / "launch-drafts.json"),
                                   str(tmp_path / "configs.json"))
    return build


def _ready(controller):
    controller.set_inventory(_inventory())
    return controller


def _state(controller, **changes):
    state, _baseline = controller.opening()
    for key, value in changes.items():
        state[key] = value
    return state


def _signature(state):
    return json.dumps(launch.merged(state), sort_keys=True)


# ------------------------------------------------------------- what there is

def test_with_nothing_set_up_every_users_json_value_is_an_environment(make):
    controller = _ready(make())
    assert [env.id for env in controller.environments()] == [
        "env:localhost:8069", "env:localhost:8070", "env:https://staging.example.invalid/"]
    assert controller.environment_id == "env:localhost:8069"


def test_configurations_move_over_and_land_in_their_environment(make):
    controller = _ready(make({
        "Staging smoke": launch.merged({"environment": "staging"}),
        "Old local": launch.merged({"environment": "localhost"}),
        "Everyone": launch.merged({"environment": ""})}))
    envs = {env.id: env for env in controller.environments()}
    assert "missing:localhost" in envs and envs["missing:localhost"].name == "localhost"
    assert lc.ALL_ENVIRONMENTS in envs
    names = lambda env_id: [p.name for p in controller.presets_here(env_id)]
    assert names("env:https://staging.example.invalid/") == ["Staging smoke"]
    assert names("missing:localhost") == ["Old local"]
    assert names(lc.ALL_ENVIRONMENTS) == ["Everyone"]


def test_the_opened_configuration_comes_back_by_its_id(make, tmp_path):
    settings = Settings()
    settings._qs = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
    settings.launch_config_name = "Staging smoke"
    controller = _ready(make({"Staging smoke": launch.merged({"environment": "staging"})},
                             settings))
    assert controller.preset().name == "Staging smoke"
    assert controller.environment_id == "env:https://staging.example.invalid/"


# ------------------------------------------------------------- moving around

def test_each_environment_remembers_what_was_open_in_it(make):
    controller = _ready(make({"Staging smoke": launch.merged({"environment": "staging"})}))
    staging = "env:https://staging.example.invalid/"
    preset = controller.presets_here(staging)[0]
    controller.select_preset(preset.id)
    controller.select_environment("env:localhost:8069")
    assert controller.preset_id == lc.NEW
    controller.select_environment(staging)
    assert controller.preset_id == preset.id


def test_working_copies_are_kept_per_configuration_and_per_new_one(make):
    controller = _ready(make({"Staging smoke": launch.merged({"environment": "staging"})}))
    staging = "env:https://staging.example.invalid/"
    preset = controller.presets_here(staging)[0]
    state, _ = controller.select_preset(preset.id)
    state["sessions"]["jobs"] = 7
    controller.keep_draft(state, True, _signature(state))
    new_state, baseline = controller.select_environment("env:localhost:8069", state)
    assert baseline is None and new_state["environment_value"] == "localhost:8069"
    new_state["sessions"]["jobs"] = 3
    controller.keep_draft(new_state, False, _signature(new_state))
    back, baseline = controller.select_environment(staging)
    assert back["sessions"]["jobs"] == 7 and baseline["sessions"]["jobs"] == 1
    again, _ = controller.select_environment("env:localhost:8069")
    assert again["sessions"]["jobs"] == 3


def test_a_desktop_link_opens_the_saved_configuration_not_its_working_copy(make):
    controller = _ready(make({"Staging smoke": launch.merged({"environment": "staging"})}))
    preset = controller.presets.all()[0]
    state, _ = controller.select_preset(preset.id)
    state["sessions"]["jobs"] = 5
    controller.keep_draft(state, True, _signature(state))
    (opened, baseline), _ = controller.open_saved(preset.id)
    assert opened["sessions"]["jobs"] == 1 == baseline["sessions"]["jobs"]
    assert controller.open_saved("Staging smoke")[0][0]["sessions"]["jobs"] == 1
    assert controller.open_saved("No such thing") == (None, [])


def test_a_run_from_history_opens_as_new_in_the_environment_it_launched(make):
    controller = _ready(make())
    config = launch.merged({"environment": "anything", "environment_value": "localhost:8070",
                            "sessions": dict(launch.DEFAULTS["sessions"], jobs=4)})
    state = controller.open_unsaved(config)
    assert controller.environment_id == "env:localhost:8070"
    assert controller.preset_id == lc.NEW and state["sessions"]["jobs"] == 4
    gone = controller.open_unsaved(launch.merged({"environment": "old-stand",
                                                 "environment_value": "localhost:9999"}))
    assert controller.environment_id == "missing:old-stand"
    assert gone["environment"] == "old-stand"


# ------------------------------------------------------------- saving

def test_a_new_configuration_is_saved_into_the_environment_it_was_made_in(make):
    controller = _ready(make())
    controller.select_environment("env:localhost:8070")
    state = _state(controller, variables={"port": 8074})
    saved = controller.save(state, "Mine")
    assert saved.environment_id == "env:localhost:8070"
    assert saved.variables == {"port": 8074} and "variables" not in saved.config
    assert "environment" not in saved.config
    assert controller.preset_id == saved.id
    assert controller.draft(controller.draft_key(preset_id=lc.NEW,
                                                 env_id="env:localhost:8070")) is None


def test_a_save_made_elsewhere_since_it_was_opened_is_said_not_overwritten(make):
    controller = _ready(make())
    saved = controller.save(_state(controller), "Mine")
    mine = _state(controller)                       # opened here at revision 1
    controller.presets.put(controller.presets.get(saved.id))   # another window saves
    with pytest.raises(lps.PresetConflict):
        controller.save(mine)
    assert controller.save(mine, overwrite=True).revision == 3


def test_save_as_leaves_the_original_as_it_was_saved(make):
    controller = _ready(make())
    original = controller.save(_state(controller), "Original")
    state = _state(controller)
    state["sessions"]["jobs"] = 6
    controller.keep_draft(state, True, _signature(state))
    copy = controller.save_as(state, "Original")
    assert copy.name == "Original (2)" and copy.id != original.id
    assert controller.draft("preset:" + original.id) is None
    assert controller.presets.get(original.id).config["sessions"]["jobs"] == 1


def test_a_configuration_with_its_environment_gone_is_moved_not_copied(make):
    controller = _ready(make({"Old local": launch.merged({"environment": "localhost"})}))
    preset = controller.presets.all()[0]
    state, _ = controller.select_preset(preset.id)
    moved = controller.use_with_environment("env:localhost:8070", state, move=True)
    assert moved.id == preset.id and moved.environment_id == "env:localhost:8070"
    assert not moved.legacy
    assert "missing:localhost" not in {env.id for env in controller.environments()}


def test_using_one_with_another_environment_makes_a_copy_there(make):
    controller = _ready(make())
    original = controller.save(_state(controller), "Smoke")
    copy = controller.use_with_environment("env:https://staging.example.invalid/",
                                           _state(controller))
    assert copy.id != original.id and copy.name == "Smoke (staging)"
    assert controller.environment_id == "env:https://staging.example.invalid/"


def test_rename_keeps_the_id_and_delete_goes_back_to_new(make):
    controller = _ready(make())
    saved = controller.save(_state(controller), "Smoke")
    renamed = controller.rename("Smoke 2")
    assert renamed.id == saved.id and renamed.name == "Smoke 2"
    controller.delete()
    assert controller.preset_id == lc.NEW and controller.presets.get(saved.id) is None


# ------------------------------------------------------------- variables

def test_a_value_is_labelled_with_the_place_that_gave_it(make):
    controller = _ready(make())
    controller.set_environment_value("port", 8070)
    assert controller.environment().variables == {"port": "8070"}
    saved = controller.save(_state(controller, variables={"port": 8071}), "Port")
    scope = controller.scope(_state(controller))
    from cms_gui import launchvars
    result = launchvars.resolve("localhost:{{port}}", scope.definitions, scope.layers)
    assert result.text == "localhost:8071"
    assert result.values["port"].source == lc.SOURCE_CONFIGURATION
    changed = _state(controller, variables={"port": 8074})
    result = launchvars.resolve("{{port}}", *controller.scope(changed))
    value = result.values["port"]
    assert (value.text, value.source) == ("8074", lc.SOURCE_RUN)
    assert (value.inherited, value.inherited_source) == ("8071", lc.SOURCE_CONFIGURATION)
    assert saved.variables == {"port": 8071}


def test_changing_an_environment_writes_it_down_and_keeps_its_id(make):
    controller = _ready(make())
    controller.set_environment_address("http://localhost:{{port}}")
    env = controller.environment()
    assert env.id == "env:localhost:8069" and env.address == "http://localhost:{{port}}"
    assert not env.virtual
    assert controller.env_urls() == {"localhost:8069": "http://localhost:{{port}}"}


def test_addresses_kept_by_env_value_move_into_their_environments(make, tmp_path):
    settings = Settings()
    settings._qs = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
    settings.set_env_url("localhost:8070", "localhost:8074")
    controller = _ready(make(settings=settings))
    env = controller.environment("env:localhost:8070")
    assert env.address == "localhost:8074" and settings.env_urls() == {}


def test_a_desktop_link_run_writes_nothing(make):
    controller = make()
    controller.persistent = False
    _ready(controller)
    state = _state(controller)
    controller.keep_draft(state, True, _signature(state))
    assert controller.settings.launch_context() == {}
    assert controller.draft() is None


def test_a_configuration_saved_without_an_environment_is_all_environments(make):
    """Not "? (missing)": nothing was picked, so every account opens the address."""
    controller = make()
    controller.set_inventory(core.Inventory({"envs": []}))
    assert controller.environment() is None
    saved = controller.save(_state(controller), "No environment")
    assert saved.all_environments
    assert controller.preset_environment_id(saved) == lc.ALL_ENVIRONMENTS
    assert controller.environment().name == "All environments"


def test_one_saved_before_with_no_environment_reads_as_all_environments(make):
    controller = _ready(make())
    preset = controller.save(_state(controller), "Old")
    preset.environment_id, preset.legacy, preset.all_environments = "", None, False
    assert controller.preset_environment_id(preset) == lc.ALL_ENVIRONMENTS
