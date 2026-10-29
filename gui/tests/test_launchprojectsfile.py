"""launch-projects.json: environments as a name and variables, kept in projects - and
the ones nobody set."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import core, launchprojectsfile as lpf, launchvars


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
                          [lpf.EnvironmentRow("e-1", "Local", "localhost:8069",
                                              "http://localhost:{{port}}",
                                              {"port": "8070"})])


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


def test_url_is_a_variable_written_first():
    row = lpf.EnvironmentRow("e-1", "Local", "localhost:8069", "",
                             {"port": 8069, "debug": True})
    assert row.variables == {"port": "8069", "debug": "true"}
    row.address = "http://localhost:{{port}}"
    assert list(row.to_entry()["variables"]) == ["url", "port", "debug"]
    row.address = ""
    assert "url" not in row.variables and row.address == ""


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
    (lambda p: p.environments[0].variables.update({"env": "x"}), "variable's name"),
    (lambda p: p.environments[0].variables.update({"2nd": "x"}), "variable's name"),
    (lambda p: p.environments.append(lpf.EnvironmentRow("e-2", "local", "localhost:8070")),
     "share this name"),
])
def test_what_the_rest_relies_on_is_checked(change, said):
    project = _helpdesk()
    change(project)
    assert any(said in problem for problem in lpf.validate([project])), \
        lpf.validate([project])


def test_a_half_written_address_and_any_text_value_can_be_saved():
    project = _helpdesk()
    project.environments[0].address = "http://localhost:{{port"
    project.environments[0].variables["port"] = "not yet"
    assert lpf.validate([project]) == []


# ------------------------------------------------------------- version 1

def _version_1(tmp_path):
    path = tmp_path / lpf.FILE_NAME
    path.write_text(json.dumps({"version": 1, "projects": [{
        "id": "workspace", "name": "Workspace", "colour": "teal",
        "variables": {"HD_port": {"type": "port", "default": 8178},
                      "stage_url": {"type": "string",
                                    "default": "https://stage.example.invalid/"},
                      "debug": {"type": "boolean", "default": False}},
        "environments": [
            {"id": "env:localhost:8180", "name": "Helpdesk", "account_key": "helpdesk",
             "address": "http://localhost:{{HD_port}}/web?debug={{debug}}"},
            {"id": "e-2", "name": "Other port", "account_key": "localhost:8069",
             "address": "http://localhost:{{HD_port}}", "variables": {"HD_port": 8190},
             "note": "kept"},
            {"id": "e-3", "name": "Plain", "account_key": "localhost:8070"}]}]}))
    return path


def test_version_1_takes_the_values_its_address_used(tmp_path):
    path = _version_1(tmp_path)
    project = lpf.load(str(path))[0]
    helpdesk, other, plain = project.environments
    assert helpdesk.variables == {
        "url": "http://localhost:{{HD_port}}/web?debug={{debug}}",
        "HD_port": "8178", "debug": "false"}
    assert other.variables == {"url": "http://localhost:{{HD_port}}", "HD_port": "8190"}
    assert other.extra == {"note": "kept"}
    assert plain.variables == {}
    # A variable nobody's address used is let go; the project is only a folder.
    assert project.to_entry() == {"colour": "teal", "id": "workspace",
                                  "name": "Workspace",
                                  "environments": [row.to_entry() for row in
                                                   project.environments]}


def test_version_1_opens_where_it_did(tmp_path):
    path = _version_1(tmp_path)
    envs = lpf.catalog(lpf.load(str(path)), _inventory())
    other = lpf.find(envs, "e-2")
    scope = lpf.scope(other.variables)
    result = launchvars.resolve(other.address, scope.definitions, scope.layers)
    assert result.text == "http://localhost:8190" and not result.problems


def test_the_first_save_keeps_the_version_1_file_beside_it(tmp_path):
    path = _version_1(tmp_path)
    before = path.read_text()
    registry = lpf.Registry(str(path))
    registry.update(lambda projects: setattr(projects[0], "name", "Everything"))
    assert json.loads(path.read_text())["version"] == lpf.VERSION
    kept = tmp_path / (lpf.FILE_NAME + ".v1")
    assert kept.read_text() == before
    registry.update(lambda projects: setattr(projects[0], "name", "Again"))
    assert kept.read_text() == before


# ------------------------------------------------------------- what is offered

def test_every_unclaimed_value_is_offered_as_a_virtual_environment():
    envs = lpf.catalog([_helpdesk()], _inventory())
    assert [(e.id, e.project_id, e.virtual) for e in envs] == [
        ("e-1", "p-1", False),
        ("env:localhost:8070", lpf.WORKSPACE, True),
        ("env:https://staging.example.invalid/", lpf.WORKSPACE, True)]
    local = envs[0]
    assert (local.origin, local.accounts) == ("http://localhost:8069", 3)
    assert local.address == "http://localhost:{{port}}"
    assert local.variables == {"port": "8070"}
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
    second.update(lambda projects: projects[0].environments[0].variables.update(
        host="localhost"))
    assert lpf.load(path)[0].environments[0].variables == {
        "url": "http://localhost:{{port}}", "port": "8070", "host": "localhost"}


def test_an_unreadable_file_is_never_written_over(tmp_path):
    path = tmp_path / lpf.FILE_NAME
    path.write_text("{not json", encoding="utf-8")
    registry = lpf.Registry(str(path))
    assert registry.error
    with pytest.raises(lpf.LaunchProjectsError):
        registry.update(lambda projects: projects.append(_helpdesk()))
    assert path.read_text(encoding="utf-8") == "{not json"


# ------------------------------------------------------------- keys and scope

def test_a_new_key_is_made_from_the_name_once_and_never_clashes():
    assert lpf.new_key("Helpdesk Local") == "helpdesk-local"
    assert lpf.new_key("\u65e5\u672c") == "env"     # nothing ASCII in it
    assert lpf.new_key("Helpdesk", ["helpdesk"]) == "helpdesk-2"
    assert lpf.new_key("Helpdesk", ["HELPDESK", "helpdesk-2"]) == "helpdesk-3"
    # A key the launcher could not tell from one already there is skipped.
    assert lpf.new_key("localhost 8069", ["localhost:8069"]) == "localhost-8069"


def test_scope_makes_every_given_name_a_text_variable_but_url():
    scope = lpf.scope({"url": "http://x", "port": "8069"},
                      [launchvars.Layer("this run", {"branch": "42", "gone": None})])
    assert sorted(scope.definitions) == ["branch", "port"]
    assert {d.type for d in scope.definitions.values()} == {"string"}
    result = launchvars.resolve("http://localhost:{{port}}/{{branch}}",
                                scope.definitions, scope.layers)
    assert result.text == "http://localhost:8069/42"
    assert result.values["port"].source == lpf.ENVIRONMENT


def test_the_command_page_gets_an_address_only_when_one_environment_holds_the_value():
    registry = lpf.Registry("")
    registry.projects = [_helpdesk()]
    assert registry.address_for_value("localhost:8069", _inventory()) == \
        "http://localhost:{{port}}"
    registry.projects[0].environments.append(
        lpf.EnvironmentRow("e-2", "Other port", "localhost:8069", "localhost:9000"))
    assert registry.address_for_value("localhost:8069", _inventory()) == ""


# ------------------------------------------- an environment that left its value

def _inventory_with(*values):
    return type("Inv", (), {"envs": [{"value": v, "alias": v,
                                      "origin": "http://%s" % v, "count": 3}
                                     for v in values]})()


def test_a_virtual_environment_given_another_key_gets_an_id_of_its_own(tmp_path):
    path = str(tmp_path / "launch-projects.json")
    registry = lpf.Registry(path)
    virtual = lpf.find(registry.environments(_inventory_with("localhost:8180")),
                       "env:localhost:8180")

    def edit(projects):
        row = lpf.environment_row(projects, virtual)
        row.name, row.account_key = "Helpdesk", "helpdesk"
    registry.update(edit)
    envs = registry.environments(_inventory_with("localhost:8180"))
    ids = [env.id for env in envs]
    assert len(ids) == len(set(ids))
    helpdesk = [env for env in envs if env.name == "Helpdesk"][0]
    assert not helpdesk.id.startswith(lpf.VIRTUAL_PREFIX)
    # The accounts still filed under the old value are an environment again,
    # under the id the configurations made on them carry.
    back = lpf.find(envs, "env:localhost:8180")
    assert back.account_key == "localhost:8180" and back.accounts == 3


def test_a_file_written_with_the_clash_reads_mended_the_same_every_time(tmp_path):
    path = tmp_path / "launch-projects.json"
    path.write_text(json.dumps({"version": 1, "projects": [{
        "id": "workspace", "name": "Workspace", "variables": {},
        "environments": [{"id": "env:localhost:8180", "name": "Helpdesk",
                          "account_key": "helpdesk", "address": "", "variables": {}}]}]}))
    first = lpf.load(str(path))[0].environments[0].id
    second = lpf.load(str(path))[0].environments[0].id
    assert first == second and first.startswith("e-")


def test_a_virtual_environment_kept_on_its_value_keeps_its_id(tmp_path):
    registry = lpf.Registry(str(tmp_path / "launch-projects.json"))
    virtual = lpf.find(registry.environments(_inventory_with("localhost:8069")),
                       "env:localhost:8069")
    registry.update(lambda projects: setattr(
        lpf.environment_row(projects, virtual), "name", "Local"))
    assert lpf.find(registry.environments(_inventory_with("localhost:8069")),
                    "env:localhost:8069").name == "Local"
