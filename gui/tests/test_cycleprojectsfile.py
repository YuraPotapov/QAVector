"""The Cycles section's own projects file: read, checked, written.

Its own file rather than a corner of services.json, because a project can have
half a dozen cycles and no services at all. What is worth testing is the same
thing servicesfile's tests cover: that a missing file is not an error, that a
broken one is, that nothing invalid is ever written, and that a file from a
newer build comes back out whole.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import cycleprojectsfile as cpf


def write(path, document):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return str(path)


# -------------------------------------------------------------------- reading
def test_a_file_that_is_not_there_is_simply_no_projects_yet(tmp_path):
    """The ordinary state before anybody has made one."""
    assert cpf.load(str(tmp_path / "nothing.json")) == []
    assert cpf.load("") == []


def test_the_projects_come_back_in_the_order_they_were_written(tmp_path):
    path = write(tmp_path / "p.json", {"version": 1, "projects": [
        {"name": "Portal"}, {"name": "Release"}]})
    assert cpf.names(cpf.load(path)) == ["Portal", "Release"]


def test_everything_a_row_says_is_kept(tmp_path):
    path = write(tmp_path / "p.json", {"projects": [
        {"name": "Portal", "description": "The customer one",
         "colour": "#5980a6", "added": "2026-09-17"}]})
    project = cpf.load(path)[0]

    assert project.name == "Portal"
    assert project.description == "The customer one"
    assert project.colour == "#5980a6"
    assert project.added == "2026-09-17"


def test_a_file_from_a_newer_build_keeps_what_this_one_does_not_know(tmp_path):
    """Otherwise opening it in an older build quietly strips fields."""
    path = write(tmp_path / "p.json", {"projects": [
        {"name": "Portal", "something_new": {"deep": 1}}]})
    project = cpf.load(path)[0]
    assert project.extra == {"something_new": {"deep": 1}}

    out = str(tmp_path / "out.json")
    cpf.save(out, [project])
    assert json.load(open(out))["projects"][0]["something_new"] == {"deep": 1}


def test_a_file_with_no_projects_key_reads_as_empty(tmp_path):
    assert cpf.load(write(tmp_path / "p.json", {"version": 1})) == []


def test_a_file_that_cannot_be_understood_is_an_error(tmp_path):
    """Carrying on would mean writing over it with an empty list."""
    path = tmp_path / "p.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(cpf.CycleProjectsFileError):
        cpf.load(str(path))


def test_a_file_that_is_not_an_object_is_an_error(tmp_path):
    with pytest.raises(cpf.CycleProjectsFileError):
        cpf.load(write(tmp_path / "p.json", ["a", "list"]))


def test_projects_that_are_not_a_list_is_an_error(tmp_path):
    with pytest.raises(cpf.CycleProjectsFileError):
        cpf.load(write(tmp_path / "p.json", {"projects": {"a": 1}}))


def test_a_row_that_is_not_an_object_is_an_error(tmp_path):
    with pytest.raises(cpf.CycleProjectsFileError):
        cpf.load(write(tmp_path / "p.json", {"projects": ["Portal"]}))


# ----------------------------------------------------------------- validation
def test_a_good_list_has_nothing_wrong_with_it():
    assert cpf.validate([cpf.ProjectRow(name="Portal"),
                         cpf.ProjectRow(name="Release")]) == []


def test_a_project_with_no_name_is_reported():
    assert cpf.validate([cpf.ProjectRow(name="  ")])


def test_two_projects_with_one_name_are_reported():
    """A cycle naming it could mean either."""
    found = cpf.validate([cpf.ProjectRow(name="Portal"),
                          cpf.ProjectRow(name="Portal")])
    assert found and "share this name" in found[0]


def test_names_are_compared_the_way_people_read_them():
    assert cpf.validate([cpf.ProjectRow(name="Portal"),
                         cpf.ProjectRow(name="portal")])


# -------------------------------------------------------------------- writing
def test_saving_and_reading_back_gives_the_same_projects(tmp_path):
    path = str(tmp_path / "p.json")
    cpf.save(path, [cpf.ProjectRow(name="Portal", description="x")])

    back = cpf.load(path)
    assert cpf.names(back) == ["Portal"]
    assert back[0].description == "x"


def test_nothing_invalid_is_ever_written(tmp_path):
    """A file that cannot be read back is worse than the edit not being saved."""
    path = str(tmp_path / "p.json")
    with pytest.raises(cpf.CycleProjectsFileError):
        cpf.save(path, [cpf.ProjectRow(name="")])
    assert not os.path.exists(path)


def test_saving_with_nowhere_to_save_it_says_so():
    with pytest.raises(cpf.CycleProjectsFileError):
        cpf.save("", [cpf.ProjectRow(name="Portal")])


def test_the_previous_file_is_kept_as_a_backup(tmp_path):
    path = str(tmp_path / "p.json")
    cpf.save(path, [cpf.ProjectRow(name="First")])
    cpf.save(path, [cpf.ProjectRow(name="Second")])

    assert cpf.names(cpf.load(path)) == ["Second"]
    assert cpf.names(cpf.load(path + ".bak")) == ["First"]


def test_no_temporary_file_is_left_behind(tmp_path):
    path = str(tmp_path / "p.json")
    cpf.save(path, [cpf.ProjectRow(name="Portal")])
    assert [name for name in os.listdir(str(tmp_path))
            if name.startswith(".")] == []


def test_the_file_says_which_version_wrote_it(tmp_path):
    path = str(tmp_path / "p.json")
    cpf.save(path, [cpf.ProjectRow(name="Portal")])
    assert json.load(open(path))["version"] == cpf.VERSION


def test_a_directory_that_does_not_exist_yet_is_made(tmp_path):
    path = str(tmp_path / "deep" / "down" / "p.json")
    cpf.save(path, [cpf.ProjectRow(name="Portal")])
    assert os.path.exists(path)


def test_an_empty_field_is_not_written_at_all(tmp_path):
    """The file stays readable: a row of blanks says nothing."""
    path = str(tmp_path / "p.json")
    cpf.save(path, [cpf.ProjectRow(name="Portal")])
    assert set(json.load(open(path))["projects"][0]) == {"name"}


# --------------------------------------------------------------- housekeeping
def test_the_fingerprint_notices_the_file_changing(tmp_path):
    path = str(tmp_path / "p.json")
    assert cpf.fingerprint(path) is None

    cpf.save(path, [cpf.ProjectRow(name="Portal")])
    before = cpf.fingerprint(path)
    cpf.save(path, [cpf.ProjectRow(name="Portal"),
                    cpf.ProjectRow(name="Release")])
    assert cpf.fingerprint(path) != before


def test_the_default_sits_beside_the_other_files_the_gui_owns():
    from cms_gui import servicesfile

    assert os.path.dirname(cpf.default_path()) == os.path.dirname(
        servicesfile.default_path())
    assert cpf.default_path().endswith(cpf.FILE_NAME)


def test_what_settings_says_wins_over_the_default():
    assert cpf.resolve_path("/somewhere/mine.json") == "/somewhere/mine.json"
    assert cpf.resolve_path("") == cpf.default_path()
    assert cpf.resolve_path("  ") == cpf.default_path()


def test_a_project_can_be_found_by_name_however_it_was_typed():
    rows = [cpf.ProjectRow(name="Customer Portal")]
    assert cpf.find(rows, "customer portal") is not None
    assert cpf.find(rows, " Customer Portal ") is not None
    assert cpf.find(rows, "nope") is None


def test_a_row_can_be_copied_without_sharing_its_extras():
    one = cpf.ProjectRow(name="Portal", extra={"deep": [1]})
    other = one.copy()
    other.extra["deep"] = [2]
    assert one.extra["deep"] == [1]
