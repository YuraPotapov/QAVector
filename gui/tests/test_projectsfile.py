"""Projects: a list of folders, Workspace first, and the paths each one holds."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import launchprojectsfile as lpf, projectsfile as pf


@pytest.fixture
def where(tmp_path):
    return str(tmp_path / pf.FILE_NAME), str(tmp_path / "projects")


def test_without_a_list_there_is_only_workspace(where):
    path, _root = where
    assert pf.load(path) == [pf.workspace()]


def test_a_new_project_gets_an_empty_folder_of_its_own(where):
    path, root = where
    project = pf.add("CLAIM", path, root)
    assert project.folder == os.path.join(root, "claim")
    for part in pf.SKELETON:
        assert os.path.isdir(os.path.join(project.folder, part))
    assert not os.path.exists(pf.paths(project.folder).users), "empty means no users"
    assert [p.name for p in pf.load(path)] == ["Workspace", "CLAIM"]
    assert json.load(open(path))["version"] == pf.VERSION


def test_names_are_unique_and_folders_never_shared(where):
    path, root = where
    pf.add("Helpdesk", path, root)
    with pytest.raises(pf.ProjectsFileError):
        pf.add("helpdesk", path, root)
    os.makedirs(os.path.join(root, "help-desk"))
    other = pf.add("Help desk", path, root)
    assert other.folder == os.path.join(root, "help-desk-2")


def test_rename_and_remove_leave_the_folder_alone(where):
    path, root = where
    project = pf.add("CLAIM", path, root)
    pf.rename(project.id, "Claim", path)
    assert pf.find(pf.load(path), project.id).name == "Claim"
    pf.remove(project.id, path)
    assert pf.find(pf.load(path), project.id) is None
    assert os.path.isdir(project.folder)
    with pytest.raises(pf.ProjectsFileError):
        pf.remove(pf.WORKSPACE, path)


def test_every_path_of_a_project_is_inside_its_folder():
    paths = pf.paths("/data/claim")
    for value in paths:
        assert value.startswith("/data/claim")


def test_launch_projects_are_adopted_once(where):
    path, root = where
    launch = [lpf.ProjectRow(lpf.WORKSPACE, lpf.WORKSPACE_NAME,
                             [lpf.EnvironmentRow("e-1", "Local", "localhost:8069")]),
              lpf.ProjectRow("p-1", "CLAIM"),
              lpf.ProjectRow("p-2", "Helpdesk",
                             [lpf.EnvironmentRow("e-2", "Help", "helpdesk")])]
    added = pf.adopt_launch_projects(launch, path, root)
    # The empty one is a project now; the one with environments gave them to
    # Workspace, where their users and configurations are.
    assert [p.name for p in added] == ["CLAIM"]
    assert [p.id for p in launch] == [lpf.WORKSPACE]
    assert [e.name for e in launch[0].environments] == ["Local", "Help"]
    assert pf.adopt_launch_projects([lpf.ProjectRow("p-9", "Later")], path, root) == []


def test_with_nothing_to_adopt_the_list_still_starts(where):
    path, root = where
    assert pf.adopt_launch_projects([], path, root) == []
    assert os.path.exists(path)
