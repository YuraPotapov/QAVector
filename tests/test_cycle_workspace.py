"""Run directories: what they are called, what is in them, and where paths point."""

import os
import time

from cycle import workspace


# ------------------------------------------------------------------- naming
def test_a_run_id_starts_with_when_and_ends_with_what(tmp_path):
    run_id = workspace.new_run_id("nightly", str(tmp_path),
                                  when=time.mktime((2026, 9, 16, 19, 34, 12,
                                                    0, 0, -1)))
    assert run_id == "20260916-193412-nightly"


def test_run_ids_sort_into_the_order_they_happened(tmp_path):
    """Which is what makes a directory listing a history, with nothing parsed."""
    earlier = workspace.new_run_id("a", str(tmp_path),
                                   when=time.mktime((2026, 9, 16, 8, 0, 0, 0, 0, -1)))
    later = workspace.new_run_id("a", str(tmp_path),
                                 when=time.mktime((2026, 9, 16, 20, 0, 0, 0, 0, -1)))
    assert earlier < later


def test_two_runs_in_the_same_second_get_different_directories(tmp_path):
    """engine/artifacts.new_run_dir does not, and they interleave their files."""
    when = time.mktime((2026, 9, 16, 19, 34, 12, 0, 0, -1))
    first = workspace.new_run_id("nightly", str(tmp_path), when=when)
    workspace.create(first, str(tmp_path))
    second = workspace.new_run_id("nightly", str(tmp_path), when=when)

    assert first != second
    assert second.endswith("-2")


def test_a_third_run_in_the_same_second_is_also_distinct(tmp_path):
    when = time.mktime((2026, 9, 16, 19, 34, 12, 0, 0, -1))
    made = []
    for _ in range(3):
        run_id = workspace.new_run_id("nightly", str(tmp_path), when=when)
        workspace.create(run_id, str(tmp_path))
        made.append(run_id)
    assert len(set(made)) == 3


def test_a_cycle_id_that_would_not_survive_a_filename_is_cleaned_up(tmp_path):
    run_id = workspace.new_run_id("weird/id: with spaces", str(tmp_path))
    assert "/" not in run_id and ":" not in run_id and " " not in run_id


def test_a_cycle_id_with_nothing_usable_in_it_still_gives_a_name(tmp_path):
    assert workspace.new_run_id("///", str(tmp_path)).endswith("-cycle")


def test_safe_id_keeps_what_is_already_safe():
    assert workspace.safe_id("nightly_build-2") == "nightly_build-2"


# ---------------------------------------------------------------- the directory
def test_creating_a_run_makes_all_of_its_rooms(tmp_path):
    """Present from the start, so a half-finished run looks like a run."""
    path = workspace.create("20260916-120000-demo", str(tmp_path))
    assert os.path.isdir(path)
    for name in workspace.SUBDIRS:
        assert os.path.isdir(os.path.join(path, name)), name


def test_creating_the_same_run_twice_is_harmless(tmp_path):
    first = workspace.create("demo-run", str(tmp_path))
    assert workspace.create("demo-run", str(tmp_path)) == first


def test_where_a_run_is_can_be_asked_before_it_exists(tmp_path):
    assert workspace.path_of("not-yet", str(tmp_path)) == os.path.join(
        str(tmp_path), "not-yet")


def test_a_step_gets_a_directory_under_the_run(tmp_path):
    path = workspace.create("demo-run", str(tmp_path))
    step = workspace.step_dir(path, "checkout")
    assert os.path.isdir(step)
    assert step == os.path.join(path, "steps", "checkout")


def test_a_step_directory_can_be_reached_into(tmp_path):
    path = workspace.create("demo-run", str(tmp_path))
    assert os.path.isdir(workspace.step_dir(path, "tests", "reports"))


def test_a_step_id_is_made_safe_before_it_becomes_a_directory(tmp_path):
    path = workspace.create("demo-run", str(tmp_path))
    assert ".." not in workspace.step_dir(path, "../escape")


def test_artifacts_reports_and_logs_each_have_their_place(tmp_path):
    path = workspace.create("demo-run", str(tmp_path))
    assert workspace.artifact_path(path, "chart.png").startswith(
        os.path.join(path, "artifacts"))
    assert workspace.report_path(path, "run.html").startswith(
        os.path.join(path, "reports"))
    assert workspace.log_path(path).endswith(os.path.join("logs", "cycle.jsonl"))


def test_a_path_with_directories_in_it_cannot_escape_its_room(tmp_path):
    path = workspace.create("demo-run", str(tmp_path))
    assert workspace.artifact_path(path, "../../etc/passwd") == os.path.join(
        path, "artifacts", "passwd")


# -------------------------------------------------------------------- recording
def test_a_path_inside_the_workspace_is_recorded_relative_to_it(tmp_path):
    """So a run directory still reads correctly after it is zipped and moved."""
    path = workspace.create("demo-run", str(tmp_path))
    inside = os.path.join(path, "steps", "a", "stdout.log")
    assert workspace.relative(path, inside) == os.path.join("steps", "a",
                                                            "stdout.log")


def test_a_path_outside_the_workspace_is_left_as_it_was(tmp_path):
    """A string of `..` would mean less than the path it replaced."""
    path = workspace.create("demo-run", str(tmp_path))
    assert workspace.relative(path, "/etc/hosts") == "/etc/hosts"


# --------------------------------------------------------------------- listing
def test_runs_are_listed_newest_first(tmp_path):
    for name in ("20260916-080000-a", "20260916-200000-b", "20260915-120000-c"):
        workspace.create(name, str(tmp_path))
    assert workspace.existing_runs(str(tmp_path)) == [
        "20260916-200000-b", "20260916-080000-a", "20260915-120000-c"]


def test_listing_a_place_with_no_runs_in_it_gives_nothing(tmp_path):
    assert workspace.existing_runs(str(tmp_path / "nowhere")) == []


def test_a_stray_file_beside_the_run_directories_is_not_a_run(tmp_path):
    workspace.create("20260916-080000-a", str(tmp_path))
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    assert workspace.existing_runs(str(tmp_path)) == ["20260916-080000-a"]


def test_the_default_root_is_the_users_own_cycle_runs_directory():
    import runtime_paths
    assert workspace.runs_root() == runtime_paths.cycle_runs_dir()
