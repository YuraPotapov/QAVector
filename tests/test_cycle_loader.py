"""Finding cycle files across the trees, and reading them."""

import os

import pytest

from cycle import loader
from cycle.model import CycleError

yaml = pytest.importorskip("yaml")


DEMO = """\
id: demo
name: Demo cycle
steps:
  - id: first
    plugin: command.shell
    with:
      command: echo hello
  - id: second
    plugin: command.shell
    needs: [first]
"""


def write(directory, name, text):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


# ----------------------------------------------------------------- search path
def test_one_directory_is_taken_literally():
    """That is what --cycles-dir means."""
    assert loader.search_path("/somewhere") == ["/somewhere"]


def test_a_list_is_taken_literally_too():
    """How a caller stages a candidate tree in front of the real ones."""
    assert loader.search_path(["/a", "/b"]) == ["/a", "/b"]


def test_no_directory_means_the_layered_default():
    path = loader.search_path(None)
    assert path and all(part.endswith("cycles") for part in path)


def test_an_empty_list_falls_back_rather_than_searching_nothing():
    assert loader.search_path([]) == [loader.DEFAULT_CYCLES_DIR]


# ------------------------------------------------------------------- resolution
def test_a_cycle_id_resolves_to_a_file_beside_it(tmp_path):
    path = write(str(tmp_path), "demo.yaml", DEMO)
    assert loader.cycle_path("demo", str(tmp_path)) == path


def test_the_yml_spelling_is_read_too(tmp_path):
    path = write(str(tmp_path), "demo.yml", DEMO)
    assert loader.cycle_path("demo", str(tmp_path)) == path


def test_yaml_wins_when_a_tree_somehow_has_both(tmp_path):
    write(str(tmp_path), "demo.yml", DEMO)
    expected = write(str(tmp_path), "demo.yaml", DEMO)
    assert loader.cycle_path("demo", str(tmp_path)) == expected


def test_a_missing_cycle_resolves_to_where_it_would_go(tmp_path):
    """So "no cycle file for X" points somewhere useful."""
    assert loader.cycle_path("ghost", str(tmp_path)) == os.path.join(
        str(tmp_path), "ghost.yaml")


def test_a_new_cycle_is_written_with_the_yaml_spelling(tmp_path):
    assert loader.canonical_path("fresh", str(tmp_path)).endswith("fresh.yaml")


def test_the_nearest_tree_wins(tmp_path):
    """A cycle the user edited shadows a bundled one with the same id."""
    user, bundled = str(tmp_path / "user"), str(tmp_path / "bundled")
    mine = write(user, "demo.yaml", DEMO)
    write(bundled, "demo.yaml", DEMO.replace("Demo cycle", "Shipped"))
    assert loader.cycle_path("demo", [user, bundled]) == mine


def test_a_cycle_only_in_the_far_tree_is_still_found(tmp_path):
    user, bundled = str(tmp_path / "user"), str(tmp_path / "bundled")
    os.makedirs(user)
    theirs = write(bundled, "shipped.yaml", DEMO.replace("id: demo",
                                                         "id: shipped"))
    assert loader.cycle_path("shipped", [user, bundled]) == theirs


# ---------------------------------------------------------------------- reading
def test_reading_a_cycle_gives_the_parsed_one_and_what_was_written(tmp_path):
    write(str(tmp_path), "demo.yaml", DEMO)
    cycle, raw = loader.read_cycle("demo", str(tmp_path))
    assert cycle.id == "demo"
    assert cycle.name == "Demo cycle"
    assert [step.id for step in cycle.steps] == ["first", "second"]
    assert raw["steps"][0]["with"] == {"command": "echo hello"}


def test_a_loaded_cycle_remembers_the_file_it_came_from(tmp_path):
    path = write(str(tmp_path), "demo.yaml", DEMO)
    assert loader.load_cycle("demo", str(tmp_path)).source == path


def test_asking_for_a_cycle_that_is_not_there_says_where_it_looked(tmp_path):
    with pytest.raises(loader.CycleNotFound) as caught:
        loader.load_cycle("ghost", str(tmp_path))
    assert "ghost" in str(caught.value)


def test_a_cycle_not_found_is_a_cycle_error(tmp_path):
    """So one except clause covers "missing" and "malformed" where that is right."""
    assert issubclass(loader.CycleNotFound, CycleError)


def test_a_file_that_is_not_a_mapping_raises(tmp_path):
    write(str(tmp_path), "broken.yaml", "- just\n- a list\n")
    with pytest.raises(CycleError):
        loader.load_cycle("broken", str(tmp_path))


def test_an_empty_file_reads_as_an_empty_cycle(tmp_path):
    write(str(tmp_path), "blank.yaml", "")
    assert loader.load_cycle("blank", str(tmp_path)).steps == []


# -------------------------------------------------------------------- inventory
def test_every_cycle_in_a_tree_is_listed(tmp_path):
    write(str(tmp_path), "one.yaml", DEMO.replace("id: demo", "id: one"))
    write(str(tmp_path), "two.yaml", DEMO.replace("id: demo", "id: two"))
    assert sorted(loader.cycle_files(str(tmp_path))) == ["one", "two"]


def test_files_that_are_not_cycles_are_ignored(tmp_path):
    write(str(tmp_path), "one.yaml", DEMO.replace("id: demo", "id: one"))
    write(str(tmp_path), "notes.txt", "hello")
    write(str(tmp_path), "one.yaml.bak", DEMO)
    assert sorted(loader.cycle_files(str(tmp_path))) == ["one"]


def test_a_tree_that_does_not_exist_contributes_nothing(tmp_path):
    assert loader.cycle_files(str(tmp_path / "nowhere")) == {}


def test_one_id_names_one_file_across_two_trees(tmp_path):
    user, bundled = str(tmp_path / "user"), str(tmp_path / "bundled")
    mine = write(user, "demo.yaml", DEMO)
    write(bundled, "demo.yaml", DEMO)
    write(bundled, "extra.yaml", DEMO.replace("id: demo", "id: extra"))
    found = loader.cycle_files([user, bundled])
    assert sorted(found) == ["demo", "extra"]
    assert found["demo"] == mine


def test_discover_returns_what_it_could_read(tmp_path):
    write(str(tmp_path), "good.yaml", DEMO.replace("id: demo", "id: good"))
    found = loader.discover(str(tmp_path))
    assert [entry[0] for entry in found] == ["good"]
    assert found[0][1].id == "good"


def test_one_broken_cycle_does_not_hide_the_others(tmp_path):
    """The inventory is read on every start; it must survive a bad file."""
    write(str(tmp_path), "good.yaml", DEMO.replace("id: demo", "id: good"))
    write(str(tmp_path), "broken.yaml", "steps: not-a-list\n")
    write(str(tmp_path), "unparseable.yaml", "steps: [\n  unclosed\n")
    assert [entry[0] for entry in loader.discover(str(tmp_path))] == ["good"]


def test_discover_comes_back_in_a_stable_order(tmp_path):
    for name in ("zebra", "apple", "mango"):
        write(str(tmp_path), name + ".yaml", DEMO.replace("id: demo", "id: " + name))
    assert [entry[0] for entry in loader.discover(str(tmp_path))] == [
        "apple", "mango", "zebra"]


# --------------------------------------------------------------------- bundled
def test_nothing_is_bundled_when_there_is_only_one_tree(tmp_path):
    """A source checkout: everything is the developer's to edit."""
    path = write(str(tmp_path), "demo.yaml", DEMO)
    assert loader.is_bundled(path, str(tmp_path)) is False


def test_a_cycle_in_the_far_tree_is_bundled(tmp_path):
    user, bundled = str(tmp_path / "user"), str(tmp_path / "bundled")
    os.makedirs(user)
    theirs = write(bundled, "shipped.yaml", DEMO)
    assert loader.is_bundled(theirs, [user, bundled]) is True


def test_a_cycle_in_the_writable_tree_is_not_bundled(tmp_path):
    user, bundled = str(tmp_path / "user"), str(tmp_path / "bundled")
    mine = write(user, "demo.yaml", DEMO)
    os.makedirs(bundled, exist_ok=True)
    assert loader.is_bundled(mine, [user, bundled]) is False
