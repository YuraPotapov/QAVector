"""Saved configurations with ids: revisions, names, links, and the move from configs.json."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import core, launch, launchprojectsfile as lpf, launchpresetsfile as lps


@pytest.fixture
def presets(tmp_path):
    return lps.Presets(str(tmp_path / lps.FILE_NAME))


def _preset(name="Smoke", env="env:localhost:8069", project=lpf.WORKSPACE):
    return lps.Preset("", name, project, env, 0, launch.merged({}), {"port": 8074})


def test_a_new_configuration_gets_an_id_and_its_first_revision(presets):
    saved = presets.put(_preset())
    assert saved.id.startswith("c-") and saved.revision == 1
    assert presets.get(saved.id).variables == {"port": 8074}


def test_each_save_moves_the_revision_on(presets):
    saved = presets.put(_preset())
    saved.name = "Smoke renamed"
    again = presets.put(saved, base_revision=1)
    assert again.revision == 2 and presets.get(saved.id).name == "Smoke renamed"


def test_a_save_made_elsewhere_since_it_was_opened_is_not_written_over(presets):
    mine = presets.put(_preset())
    theirs = presets.get(mine.id)
    theirs.config["sessions"]["jobs"] = 9
    presets.put(theirs, base_revision=1)             # the other window saves first
    mine.config["sessions"]["jobs"] = 2
    with pytest.raises(lps.PresetConflict) as raised:
        presets.put(mine, base_revision=1)
    assert raised.value.current.config["sessions"]["jobs"] == 9
    assert presets.get(mine.id).config["sessions"]["jobs"] == 9


def test_names_are_unique_within_a_project_only(presets):
    presets.put(_preset("Smoke"))
    presets.put(_preset("Smoke", env="e-9", project="p-other"))
    assert presets.unique_name(lpf.WORKSPACE, "Smoke") == "Smoke (2)"
    assert presets.unique_name("p-third", "Smoke") == "Smoke"


def test_a_link_finds_a_configuration_by_id_then_by_a_name_only_one_has(presets):
    first = presets.put(_preset("Smoke"))
    assert presets.find(first.id) == (first, [])
    assert presets.find("Smoke")[0].id == first.id
    presets.put(_preset("Smoke", env="e-9", project="p-other"))
    preset, candidates = presets.find("Smoke")
    assert preset is None and len(candidates) == 2
    assert presets.find("Nothing") == (None, [])


def test_an_unreadable_file_reads_as_empty_and_is_kept_aside(tmp_path):
    path = tmp_path / lps.FILE_NAME
    path.write_text("{broken", encoding="utf-8")
    presets = lps.Presets(str(path))
    assert presets.all() == [] and presets.unreadable
    presets.put(_preset())
    kept = [name for name in os.listdir(tmp_path) if ".unreadable-" in name]
    assert kept and (tmp_path / kept[0]).read_text(encoding="utf-8") == "{broken"


# ------------------------------------------------ the move from configs.json

def _configs(tmp_path):
    path = tmp_path / lps.CONFIGS_FILE
    path.write_text(json.dumps({
        "Admin smoke": launch.merged({"environment": "staging"}),
        "Old local": launch.merged({"environment": "localhost"}),
        "Gone port": launch.merged({"environment": "localhost:8071"}),
        "Everyone": launch.merged({"environment": ""}),
        "Kept value": launch.merged({"environment": "local",
                                     "environment_value": "localhost:8070"}),
    }), encoding="utf-8")
    return str(path)


def _inventory():
    return core.Inventory({"envs": [
        {"alias": "localhost:8069", "value": "localhost:8069",
         "origin": "http://localhost:8069", "count": 1},
        {"alias": "localhost:8070", "value": "localhost:8070",
         "origin": "http://localhost:8070", "count": 1},
        {"alias": "staging", "value": "https://staging.example.invalid/",
         "origin": "https://staging.example.invalid", "count": 1}]})


def test_every_configuration_moves_with_its_name_and_settings(presets, tmp_path):
    configs = _configs(tmp_path)
    before = open(configs, encoding="utf-8").read()
    names = lps.migrate_configs(presets, configs, lpf.WORKSPACE)
    assert sorted(names) == ["Admin smoke", "Everyone", "Gone port", "Kept value",
                             "Old local"]
    moved = {preset.name: preset for preset in presets.all()}
    assert "environment" not in moved["Admin smoke"].config
    assert moved["Admin smoke"].legacy == {"alias": "staging", "value": ""}
    assert moved["Everyone"].all_environments and not moved["Everyone"].legacy
    assert open(configs, encoding="utf-8").read() == before, "configs.json was touched"


def test_moving_twice_does_nothing_the_second_time(presets, tmp_path):
    configs = _configs(tmp_path)
    lps.migrate_configs(presets, configs, lpf.WORKSPACE)
    assert lps.migrate_configs(presets, configs, lpf.WORKSPACE) == {}
    assert len(presets.all()) == 5


def test_environments_are_bound_by_value_then_alias_and_never_guessed(presets, tmp_path):
    lps.migrate_configs(presets, _configs(tmp_path), lpf.WORKSPACE)
    environments = lpf.catalog([], _inventory())
    assert lps.bind_environments(presets, environments, _inventory()) == 2
    moved = {preset.name: preset for preset in presets.all()}
    assert moved["Admin smoke"].environment_id == "env:https://staging.example.invalid/"
    assert moved["Kept value"].environment_id == "env:localhost:8070"
    # "localhost" became localhost:8069 / :8070 - not guessed; :8071 is gone.
    assert moved["Old local"].legacy and not moved["Old local"].environment_id
    assert moved["Gone port"].legacy and not moved["Gone port"].environment_id
    assert lps.bind_environments(presets, environments, _inventory()) == 0


def test_an_old_desktop_link_still_finds_a_renamed_configuration(presets, tmp_path):
    names = lps.migrate_configs(presets, _configs(tmp_path), lpf.WORKSPACE)
    preset = presets.get(names["Admin smoke"])
    preset.name = "Admin smoke (staging)"
    presets.put(preset)
    assert presets.find("Admin smoke")[0].id == preset.id
