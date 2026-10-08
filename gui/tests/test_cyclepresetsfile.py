"""Saved steps: what a preset keeps of a step, and the file it lives in.

No Qt. The two properties worth the most: a secret's value never reaches the
file, and nothing tying the step to its old cycle comes along with it.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import cyclepresetsfile as presets          # noqa: E402

DOCUMENT = {"id": "demo", "variables": {
    "jira_site": "https://jira.example.invalid",
    "jira_token": {"secret": True, "value": "never-this"},
    "unused": "left behind"}}

STEP = {"id": "todo", "plugin": "jira.issues", "label": "Read To Do",
        "needs": ["prepare"], "group": "Intake",
        "if": "${steps.prepare.outputs.ok}", "timeout": 120.0,
        "with": {"site": "${vars.jira_site}", "token": "${ vars.jira_token }",
                 "status": "To Do"}}


def test_a_preset_keeps_the_settings_and_not_the_place_in_the_old_cycle():
    made = presets.Preset.from_step("Jira: To Do", STEP, DOCUMENT)

    assert made.plugin == "jira.issues"
    assert made.step == {"label": "Read To Do", "timeout": 120.0,
                         "with": STEP["with"]}


def test_a_condition_on_nothing_else_is_kept():
    made = presets.Preset.from_step("x", dict(STEP, **{"if": "${vars.go}"}),
                                    {"variables": {"go": "1"}})
    assert made.step["if"] == "${vars.go}"
    assert made.variables == {"go": "1"}


def test_the_variables_it_reads_travel_with_it_and_a_secret_without_its_value():
    made = presets.Preset.from_step("Jira: To Do", STEP, DOCUMENT)

    assert made.variables == {"jira_site": "https://jira.example.invalid",
                              "jira_token": {"secret": True}}


def test_the_new_step_s_id_comes_from_the_preset_s_name():
    made = presets.Preset("Jira: In Progress", "jira.issues", {"label": "x"})
    assert made.step_for() == {"id": "jira_in_progress", "label": "x"}


def test_saved_and_read_back_whole(tmp_path):
    path = str(tmp_path / presets.FILE_NAME)
    presets.save(path, [presets.Preset.from_step("Jira: To Do", STEP, DOCUMENT)])

    again = presets.load(path)

    assert [one.name for one in again] == ["Jira: To Do"]
    assert again[0].variables["jira_token"] == {"secret": True}
    assert "never-this" not in open(path).read()


def test_no_file_is_no_saved_steps(tmp_path):
    assert presets.load(str(tmp_path / "missing.json")) == []


def test_a_file_that_cannot_be_read_is_an_error_not_an_empty_list(tmp_path):
    path = tmp_path / presets.FILE_NAME
    path.write_text("{not json")
    with pytest.raises(presets.CyclePresetsFileError):
        presets.load(str(path))


def test_what_a_newer_build_wrote_survives_an_older_one(tmp_path):
    path = tmp_path / presets.FILE_NAME
    path.write_text(json.dumps({"version": 1, "presets": [
        {"name": "x", "plugin": "command.shell", "step": {}, "colour": "red"}]}))

    presets.save(str(path), presets.load(str(path)))

    assert json.loads(path.read_text())["presets"][0]["colour"] == "red"


def test_two_saved_steps_of_one_name_are_refused(tmp_path):
    two = [presets.Preset("Same", "command.shell"),
           presets.Preset("same", "command.shell")]
    assert presets.validate(two)
    with pytest.raises(presets.CyclePresetsFileError):
        presets.save(str(tmp_path / presets.FILE_NAME), two)


def test_the_file_sits_beside_the_projects_file(tmp_path):
    projects = str(tmp_path / "elsewhere" / "cycleprojects.json")
    assert presets.path_beside(projects) == str(
        tmp_path / "elsewhere" / presets.FILE_NAME)


def test_grouped_by_kind_and_in_saved_order_within_one():
    made = [presets.Preset("To Do", "jira.issues"),
            presets.Preset("Build", "command.shell"),
            presets.Preset("In Progress", "jira.issues")]
    groups = presets.grouped(made)

    assert [plugin for plugin, _rows in groups] == ["command.shell",
                                                    "jira.issues"]
    assert [one.name for one in groups[1][1]] == ["To Do", "In Progress"]


def test_it_remembers_the_cycle_its_secrets_are_kept_for():
    made = presets.Preset.from_step("Jira: To Do", STEP, DOCUMENT)
    assert made.cycle == "demo"
    assert made.secrets() == ["jira_token"]


def test_the_cycle_survives_the_file(tmp_path):
    path = str(tmp_path / presets.FILE_NAME)
    presets.save(path, [presets.Preset.from_step("x", STEP, DOCUMENT)])
    assert presets.load(path)[0].cycle == "demo"
