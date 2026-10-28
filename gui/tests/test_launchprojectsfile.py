"""launch-projects.json: projects, environments, variables - and the ones nobody set."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import core, launchprojectsfile as lpf


def _inventory():
    return core.Inventory({"envs": [
        {"alias": "localhost:8069", "value": "localhost:8069",
         "origin": "http://localhost:8069", "count": 3},
        {"alias": "localhost:8070", "value": "localhost:8070",
         "origin": "http://localhost:8070", "count": 2},
        {"alias": "staging", "value": "https://staging.example.invalid/",
         "origin": "https://staging.example.invalid", "count": 5}]})


def _helpdesk():
    return lpf.ProjectRow("p-1", "Helpdesk",
                          {"port": {"type": "port", "default": 8069}},
                          [lpf.EnvironmentRow("e-1", "Local", "localhost:8069",
                                              "http://localhost:{{port}}",
                                              {"port": 8070})])


def test_a_missing_file_is_no_projects(tmp_path):
    assert lpf.load(str(tmp_path / "nope.json")) == []


def test_it_round_trips_and_keeps_what_it_does_not_know(tmp_path):
    path = str(tmp_path / lpf.FILE_NAME)
    project = _helpdesk()
    project.extra["colour"] = "teal"
    project.environments[0].extra["note"] = "the one on my laptop"
    lpf.save(path, [project])
    again = lpf.load(path)[0]
    assert again.to_entry() == project.to_entry()
    assert json.load(open(path))["version"] == lpf.VERSION


def test_a_file_from_a_newer_build_is_not_read_as_this_one(tmp_path):
    path = tmp_path / lpf.FILE_NAME
    path.write_text(json.dumps({"version": lpf.VERSION + 1, "projects": []}))
    with pytest.raises(lpf.LaunchProjectsError):
        lpf.load(str(path))


@pytest.mark.parametrize("change, said", [
    (lambda p: setattr(p, "name", ""), "no name"),
    (lambda p: setattr(p.environments[0], "account_key", ""), "account key"),
    (lambda p: setattr(p.environments[0], "account_key", "localhost:{{port}}"),
     "placeholder"),
    (lambda p: p.environments[0].variables.update(port="abc"), "port"),
    (lambda p: p.environments[0].variables.update(colour="red"), "does not define"),
    (lambda p: p.variables.update(env={"type": "string"}), "variable"),
    (lambda p: p.environments.append(lpf.EnvironmentRow("e-2", "local", "localhost:8070")),
     "share this name"),
])
def test_what_the_rest_relies_on_is_checked(change, said):
    project = _helpdesk()
    change(project)
    assert any(said in problem for problem in lpf.validate([project])), \
        lpf.validate([project])


def test_a_half_written_address_can_be_saved(tmp_path):
    project = _helpdesk()
    project.environments[0].address = "http://localhost:{{port"
    assert lpf.validate([project]) == []


def test_every_unclaimed_value_is_offered_as_a_virtual_environment():
    envs = lpf.catalog([_helpdesk()], _inventory())
    assert [(e.id, e.project_id, e.virtual) for e in envs] == [
        ("e-1", "p-1", False),
        ("env:localhost:8070", lpf.WORKSPACE, True),
        ("env:https://staging.example.invalid/", lpf.WORKSPACE, True)]
    local = envs[0]
    assert (local.origin, local.accounts) == ("http://localhost:8069", 3)
    assert envs[2].name == "staging"


def test_with_nothing_set_up_workspace_is_the_one_project():
    envs = lpf.catalog([], _inventory())
    assert lpf.project_names([], envs) == [(lpf.WORKSPACE, lpf.WORKSPACE_NAME)]
    names = lpf.project_names([_helpdesk()], lpf.catalog([_helpdesk()], _inventory()))
    assert names == [("p-1", "Helpdesk"), (lpf.WORKSPACE, lpf.WORKSPACE_NAME)]


def test_a_virtual_id_still_finds_its_value_once_it_has_an_environment():
    envs = lpf.catalog([_helpdesk()], _inventory())
    assert lpf.find(envs, "env:localhost:8069").id == "e-1"
    assert lpf.find(envs, "env:localhost:8071") is None
    # Two environments holding the same accounts: no guessing.
    shared = _helpdesk()
    shared.environments.append(lpf.EnvironmentRow("e-2", "Other port", "localhost:8069"))
    assert lpf.find(lpf.catalog([shared], _inventory()), "env:localhost:8069") is None


def test_changing_a_virtual_environment_writes_it_down_under_its_own_id(tmp_path):
    registry = lpf.Registry(str(tmp_path / lpf.FILE_NAME))
    virtual = registry.environments(_inventory())[0]
    assert virtual.virtual and virtual.id == "env:localhost:8069"

    def change(projects):
        lpf.environment_row(projects, virtual).address = "localhost:8074"
    registry.update(change)
    envs = registry.environments(_inventory())
    assert envs[0].id == "env:localhost:8069" and not envs[0].virtual
    assert envs[0].address == "localhost:8074" and envs[0].project_id == lpf.WORKSPACE


def test_a_change_is_made_to_the_file_as_it_is_now(tmp_path):
    """Two windows: the second one's change must not undo the first one's."""
    path = str(tmp_path / lpf.FILE_NAME)
    first, second = lpf.Registry(path), lpf.Registry(path)
    first.update(lambda projects: projects.append(_helpdesk()))
    second.update(lambda projects: lpf.define(projects, "p-1", "host", "string",
                                              "localhost"))
    assert set(lpf.load(path)[0].variables) == {"port", "host"}


def test_an_unreadable_file_is_never_written_over(tmp_path):
    path = tmp_path / lpf.FILE_NAME
    path.write_text("{not json", encoding="utf-8")
    registry = lpf.Registry(str(path))
    assert registry.error
    with pytest.raises(lpf.LaunchProjectsError):
        registry.update(lambda projects: projects.append(_helpdesk()))
    assert path.read_text(encoding="utf-8") == "{not json"


def test_define_checks_the_name_and_the_type():
    projects = []
    definition = lpf.define(projects, lpf.WORKSPACE, "port", "port", 8069)
    assert (definition.type, definition.default) == ("port", 8069)
    assert projects[0].id == lpf.WORKSPACE
    with pytest.raises(lpf.LaunchProjectsError):
        lpf.define(projects, lpf.WORKSPACE, "user", "string")
    with pytest.raises(lpf.LaunchProjectsError):
        lpf.define(projects, lpf.WORKSPACE, "colour", "rgb")


def test_the_command_page_gets_an_address_only_when_one_environment_holds_the_value():
    registry = lpf.Registry("")
    registry.projects = [_helpdesk()]
    assert registry.address_for_value("localhost:8069", _inventory()) == \
        "http://localhost:{{port}}"
    registry.projects[0].environments.append(
        lpf.EnvironmentRow("e-2", "Other port", "localhost:8069", "localhost:9000"))
    assert registry.address_for_value("localhost:8069", _inventory()) == ""
