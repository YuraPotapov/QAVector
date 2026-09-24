"""Where somebody put the nodes, and whether it is still there tomorrow.

The whole point of this file is that it survives a restart, so the tests are
about the round trip and about the ways it can come back wrong. It is cosmetic
state, which sets the standard for everything here: a malformed file must cost
somebody the memory of where they dragged a box and nothing else - never the
cycle, never the page, never an exception anyone has to read.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import cyclelayoutfile as clf


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "cyclelayout.json")


# ------------------------------------------------------------- the round trip
def test_what_was_put_down_comes_back(path):
    clf.remember("development", {"todo": (10.0, 20.0), "plan": (10.0, 140.0)},
                 path)

    assert clf.places_for("development", path) == {"todo": (10.0, 20.0),
                                                   "plan": (10.0, 140.0)}


def test_arranging_one_cycle_leaves_the_others_alone(path):
    clf.remember("one", {"a": (1.0, 2.0)}, path)
    clf.remember("two", {"b": (3.0, 4.0)}, path)

    assert clf.places_for("one", path) == {"a": (1.0, 2.0)}
    assert clf.places_for("two", path) == {"b": (3.0, 4.0)}


def test_remembering_again_replaces_rather_than_merges(path):
    """A node deleted from the cycle must not keep its place for ever."""
    clf.remember("one", {"a": (1.0, 2.0), "b": (3.0, 4.0)}, path)
    clf.remember("one", {"a": (9.0, 9.0)}, path)

    assert clf.places_for("one", path) == {"a": (9.0, 9.0)}


def test_remembering_nothing_forgets_the_cycle(path):
    """Which is what Arrange does: an empty entry is a row that says nothing
    and has to be skipped by every reader."""
    clf.remember("one", {"a": (1.0, 2.0)}, path)
    clf.remember("one", {}, path)

    assert clf.places_for("one", path) == {}
    assert "one" not in load_raw(path).get("cycles", {})


def test_forget_drops_one_cycle_and_says_whether_there_was_one(path):
    clf.remember("one", {"a": (1.0, 2.0)}, path)

    assert clf.forget("one", path) is True
    assert clf.forget("one", path) is False
    assert clf.places_for("one", path) == {}


def test_a_cycle_nobody_arranged_has_no_places(path):
    clf.remember("one", {"a": (1.0, 2.0)}, path)
    assert clf.places_for("never opened", path) == {}


def test_no_file_at_all_is_an_empty_answer(path):
    assert clf.load(path) == {}
    assert clf.places_for("one", path) == {}


# ------------------------------------------------- the ways it comes back wrong
def test_a_malformed_file_reads_empty_rather_than_raising(path):
    """Refusing to open a cycle because the file remembering where its boxes
    sat is broken would be the least important thing stopping the work."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json at all")

    assert clf.load(path) == {}


@pytest.mark.parametrize("place", [
    None, "somewhere", [], [1], [1, 2, 3], ["a", "b"], [1, None],
    {"x": 1, "y": 2},
])
def test_a_place_that_is_not_a_pair_of_numbers_is_dropped(path, place):
    _write_raw(path, {"one": {"good": [1, 2], "bad": place}})

    assert clf.places_for("one", path) == {"good": (1.0, 2.0)}


def test_a_coordinate_off_the_edge_of_the_world_is_dropped(path):
    """Honouring it would put the node somewhere the view cannot scroll to,
    which reads as the node having vanished."""
    _write_raw(path, {"one": {"good": [1, 2],
                              "far": [clf.LIMIT * 10, 0],
                              "nan": [float("nan"), 0]}})

    assert clf.places_for("one", path) == {"good": (1.0, 2.0)}


def test_a_cycle_whose_places_are_not_a_mapping_is_skipped(path):
    _write_raw(path, {"one": ["a", "b"], "two": {"a": [1, 2]}})

    assert list(clf.load(path)) == ["two"]
    assert clf.places_for("two", path) == {"a": (1.0, 2.0)}


def test_a_document_that_is_not_an_object_reads_empty(path):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(["not", "a", "document"], handle)

    assert clf.load(path) == {}


def test_an_unnamed_cycle_is_not_written(path):
    clf.remember("", {"a": (1.0, 2.0)}, path)
    clf.remember("   ", {"a": (1.0, 2.0)}, path)

    assert clf.load(path) == {}


# ------------------------------------------------- where they were looking
VIEW = {"zoom": 0.45, "center": [120.0, 40.0]}


def test_a_view_comes_back_the_way_it_went_down(path):
    clf.remember_view("one", VIEW, path)

    assert clf.view_for("one", path) == VIEW


def test_a_view_and_an_arrangement_do_not_overwrite_each_other(path):
    """They are written by two different gestures, a drag and a wheel, and
    either one clearing the other would be a fix that undid itself."""
    clf.remember("one", {"a": (1.0, 2.0)}, path)
    clf.remember_view("one", VIEW, path)

    assert clf.places_for("one", path) == {"a": (1.0, 2.0)}
    assert clf.view_for("one", path) == VIEW


def test_forgetting_the_view_keeps_the_arrangement(path):
    """Fit forgets where somebody was looking from; it does not put the boxes
    back, which is what Arrange is for."""
    clf.remember("one", {"a": (1.0, 2.0)}, path)
    clf.remember_view("one", VIEW, path)
    clf.remember_view("one", None, path)

    assert clf.view_for("one", path) is None
    assert clf.places_for("one", path) == {"a": (1.0, 2.0)}


def test_a_cycle_with_neither_is_dropped_from_the_file(path):
    clf.remember("one", {"a": (1.0, 2.0)}, path)
    clf.remember_view("one", VIEW, path)
    clf.remember("one", {}, path)
    clf.remember_view("one", None, path)

    assert "one" not in load_raw(path).get("cycles", {})


@pytest.mark.parametrize("view", [
    {"zoom": 0.45},                       # no centre
    {"zoom": 0, "center": [0, 0]},        # a view with nothing in it
    {"zoom": float("nan"), "center": [0, 0]},
    {"zoom": 1.0, "center": [clf.LIMIT * 10, 0]},
    {"zoom": "big", "center": [0, 0]},
    "not a view",
])
def test_a_view_that_could_not_be_looked_at_is_dropped(path, view):
    clf.remember_view("one", view, path)

    assert clf.view_for("one", path) is None


def test_a_file_from_the_build_before_this_one_still_reads(path):
    """Version 1 wrote the places as the entry itself. Throwing somebody's
    arrangement away to add a zoom to it would be a poor trade."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "cycles": {"one": {"a": [1, 2]}}}, handle)

    assert clf.places_for("one", path) == {"a": (1.0, 2.0)}
    assert clf.view_for("one", path) is None

    clf.remember_view("one", VIEW, path)
    assert clf.places_for("one", path) == {"a": (1.0, 2.0)}


# ----------------------------------------------------------------- the file
def test_the_file_carries_its_version(path):
    clf.remember("one", {"a": (1.0, 2.0)}, path)
    assert load_raw(path)["version"] == clf.VERSION


def test_it_is_written_where_the_gui_keeps_its_other_files():
    from cms_gui import servicesfile

    assert (os.path.dirname(clf.default_path())
            == os.path.dirname(servicesfile.default_path()))
    assert clf.default_path().endswith(clf.FILE_NAME)


def test_a_directory_that_cannot_be_written_says_so_rather_than_crashing(tmp_path):
    blocked = tmp_path / "a-file-not-a-directory"
    blocked.write_text("in the way")

    with pytest.raises(clf.CycleLayoutFileError):
        clf.remember("one", {"a": (1.0, 2.0)}, str(blocked / "layout.json"))


# ---------------------------------------------------------------- helpers
def load_raw(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write_raw(path, cycles):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": clf.VERSION, "cycles": cycles}, handle)
