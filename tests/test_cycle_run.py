"""The run record on disk: what is written, when, and what reads back."""

import json
import os

from cycle import run as run_mod, workspace
from domain.cycle import (Artifact, CycleRun, StepRun, FAILED, SKIPPED, SUCCESS)


def a_run(root, run_id="20260916-120000-demo", status=SUCCESS):
    path = workspace.create(run_id, root)
    run = CycleRun(id=run_id, cycle_id="demo", cycle_name="Demo",
                   status=status, started_at=1789000000.0,
                   ended_at=1789000042.0, duration_ms=42000.0,
                   workspace=path, variables={"branch": "main"})
    run.steps = {
        "build": StepRun("build", plugin="command.shell", status=SUCCESS,
                         attempts=1, duration_ms=1200.0, message="exited 0",
                         outputs={"exit_code": 0},
                         artifacts=[Artifact("log", "steps/build/stdout.log",
                                             "build", name="stdout", bytes=9)]),
        "tests": StepRun("tests", plugin="command.shell", status=FAILED,
                         attempts=2, message="exited 1"),
        "deploy": StepRun("deploy", plugin="command.shell", status=SKIPPED,
                          message="tests failed"),
    }
    return run


# ------------------------------------------------------------------- writing
def test_saving_a_run_writes_it_into_its_own_directory(tmp_path):
    run = a_run(str(tmp_path))
    path = run_mod.save(run)
    assert path == os.path.join(run.workspace, "metadata.json")
    assert os.path.exists(path)


def test_the_record_says_which_format_it_is(tmp_path):
    """So a reader can tell a format it does not know from a file it cannot parse."""
    run = a_run(str(tmp_path))
    run_mod.save(run)
    with open(run_mod.metadata_path(run.workspace), encoding="utf-8") as handle:
        assert json.load(handle)["schema"] == run_mod.SCHEMA


def test_the_record_carries_the_run_and_every_step(tmp_path):
    run = a_run(str(tmp_path))
    document = run_mod.to_document(run)

    assert document["id"] == run.id
    assert document["cycle_id"] == "demo"
    assert document["status"] == SUCCESS
    assert document["variables"] == {"branch": "main"}
    assert set(document["steps"]) == {"build", "tests", "deploy"}
    assert document["steps"]["build"]["outputs"] == {"exit_code": 0}


def test_the_record_counts_how_the_steps_went_so_a_reader_need_not(tmp_path):
    document = run_mod.to_document(a_run(str(tmp_path)))
    assert document["tally"][SUCCESS] == 1
    assert document["tally"][FAILED] == 1
    assert document["exit_code"] == 0


def test_the_record_holds_no_absolute_path_of_any_kind(tmp_path):
    """Two reasons, and both bite. A record that names the machine it was
    written on stops describing itself the moment the directory is zipped and
    opened elsewhere - and in a source checkout, where the data root is the
    checkout, it would put somebody's home directory into a file in the working
    tree, ready to be committed."""
    run = a_run(str(tmp_path))
    run_mod.save(run)
    with open(run_mod.metadata_path(run.workspace), encoding="utf-8") as handle:
        text = handle.read()

    assert str(tmp_path) not in text
    assert "workspace" not in json.loads(text)


def test_a_run_reads_back_knowing_where_it_was_read_from(tmp_path):
    """The directory is the workspace; nothing has to be told twice."""
    run = a_run(str(tmp_path))
    run_mod.save(run)
    assert run_mod.load(run.workspace).workspace == os.path.abspath(run.workspace)


def test_a_run_directory_still_reads_after_it_is_moved(tmp_path):
    """Which is the whole point of a record with no absolute path in it."""
    import shutil

    run = a_run(str(tmp_path))
    run_mod.save(run)
    moved = str(tmp_path / "somewhere-else")
    shutil.move(run.workspace, moved)

    back = run_mod.load(moved)
    assert back is not None
    assert back.id == run.id
    assert back.workspace == os.path.abspath(moved)


def test_the_index_holds_no_absolute_path_either(tmp_path):
    root = str(tmp_path)
    run_mod.remember(a_run(root), root)
    with open(run_mod.index_path(root), encoding="utf-8") as handle:
        text = handle.read()

    assert root not in text
    assert "workspace" not in run_mod.index(root)[0]


def test_saving_survives_a_directory_that_is_not_there(tmp_path):
    """A record is never worth failing a run that has already happened."""
    run = CycleRun(id="r", cycle_id="demo",
                   workspace=str(tmp_path / "does" / "not" / "exist"))
    run_mod.save(run)                    # must not raise


def test_saving_a_run_with_nowhere_to_save_it_does_nothing(tmp_path):
    assert run_mod.save(CycleRun(id="r", cycle_id="demo", workspace="")) == ""


def test_the_record_is_written_whole_or_not_at_all(tmp_path):
    """A reader must never catch half a file; os.replace is the mechanism."""
    run = a_run(str(tmp_path))
    run_mod.save(run)
    run.status = FAILED
    run_mod.save(run)

    with open(run_mod.metadata_path(run.workspace), encoding="utf-8") as handle:
        assert json.load(handle)["status"] == FAILED
    leftovers = [name for name in os.listdir(run.workspace)
                 if name.startswith(".")]
    assert leftovers == []


# ------------------------------------------------------------------- reading
def test_a_saved_run_reads_back_as_what_it_was(tmp_path):
    run = a_run(str(tmp_path))
    run_mod.save(run)
    back = run_mod.load(run.workspace)

    assert back.id == run.id
    assert back.status == run.status
    assert back.cycle_name == "Demo"
    assert back.variables == {"branch": "main"}
    assert set(back.steps) == {"build", "tests", "deploy"}


def test_a_step_reads_back_with_its_outputs_and_artifacts(tmp_path):
    run = a_run(str(tmp_path))
    run_mod.save(run)
    step = run_mod.load(run.workspace).steps["build"]

    assert step.status == SUCCESS
    assert step.outputs == {"exit_code": 0}
    assert isinstance(step.artifacts[0], Artifact)
    assert step.artifacts[0].path == "steps/build/stdout.log"
    assert step.artifacts[0].name == "stdout"


def test_reading_where_there_is_no_record_gives_nothing(tmp_path):
    assert run_mod.load(str(tmp_path)) is None


def test_reading_a_record_that_is_not_json_gives_nothing(tmp_path):
    path = workspace.create("broken", str(tmp_path))
    with open(run_mod.metadata_path(path), "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert run_mod.load(path) is None


def test_a_record_from_an_older_version_reads_back_with_gaps(tmp_path):
    """The useful answer to a missing field is the run without it."""
    back = run_mod.from_document({"id": "r", "cycle_id": "demo",
                                  "steps": {"a": {"step_id": "a"}}})
    assert back.id == "r"
    assert back.steps["a"].status == "pending"


def test_a_record_from_a_newer_version_drops_what_it_does_not_know(tmp_path):
    """A half-understood record is worse than a plainly partial one."""
    back = run_mod.from_document({"id": "r", "cycle_id": "demo",
                                  "something_new": {"deep": 1},
                                  "steps": {"a": {"step_id": "a",
                                                  "also_new": True}}})
    assert back.id == "r"
    assert not hasattr(back, "something_new")


def test_reading_something_that_is_not_a_record_at_all_gives_nothing():
    assert run_mod.from_document(["not", "a", "mapping"]) is None


# ------------------------------------------------------------------- the graph
def test_the_graph_is_kept_beside_the_record(tmp_path):
    """So an old run is drawn as it was, not as the cycle file is now."""
    run = a_run(str(tmp_path))
    graph = {"id": "demo", "nodes": [{"id": "build"}], "edges": []}
    run_mod.Persister(root=str(tmp_path)).run_start(run, graph)

    assert run_mod.graph_of(run.workspace) == graph


def test_asking_for_a_graph_that_was_never_kept_gives_nothing(tmp_path):
    assert run_mod.graph_of(str(tmp_path)) is None


# ------------------------------------------------------------------- the index
def test_remembering_a_run_puts_it_at_the_top(tmp_path):
    root = str(tmp_path)
    run_mod.remember(a_run(root, "20260916-080000-a"), root)
    run_mod.remember(a_run(root, "20260916-200000-b"), root)

    assert [row["id"] for row in run_mod.index(root)] == [
        "20260916-200000-b", "20260916-080000-a"]


def test_a_row_says_enough_to_list_a_run_without_opening_it(tmp_path):
    root = str(tmp_path)
    run_mod.remember(a_run(root), root)
    row = run_mod.index(root)[0]

    assert row["cycle"] == "demo"
    assert row["name"] == "Demo"
    assert row["status"] == SUCCESS
    assert row["steps"] == 3
    assert row["passed"] == 1 and row["failed"] == 1 and row["skipped"] == 1
    assert row["duration_ms"] == 42000.0


def test_remembering_a_run_twice_replaces_its_row(tmp_path):
    root = str(tmp_path)
    run = a_run(root)
    run_mod.remember(run, root)
    run.status = FAILED
    run_mod.remember(run, root)

    rows = run_mod.index(root)
    assert len(rows) == 1
    assert rows[0]["status"] == FAILED


def test_the_index_is_capped(tmp_path):
    root = str(tmp_path)
    rows = [{"id": "run-%d" % index} for index in range(run_mod.MAX_INDEXED + 20)]
    run_mod._atomic(run_mod.index_path(root), {"schema": 1, "runs": rows})
    run_mod.remember(a_run(root), root)

    assert len(run_mod.index(root)) == run_mod.MAX_INDEXED


def test_reading_an_index_that_is_not_there_gives_nothing(tmp_path):
    assert run_mod.index(str(tmp_path)) == []


def test_reading_an_index_that_is_broken_gives_nothing_rather_than_raising(tmp_path):
    with open(run_mod.index_path(str(tmp_path)), "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert run_mod.index(str(tmp_path)) == []


def test_the_index_can_be_rebuilt_from_the_directories(tmp_path):
    """The directories are the record; the index only saves opening them all."""
    root = str(tmp_path)
    for run_id in ("20260916-080000-a", "20260916-200000-b"):
        run_mod.save(a_run(root, run_id))
    os.remove(run_mod.index_path(root)) if os.path.exists(
        run_mod.index_path(root)) else None

    rows = run_mod.rebuild_index(root)
    assert sorted(row["id"] for row in rows) == ["20260916-080000-a",
                                                 "20260916-200000-b"]


def test_rebuilding_ignores_a_directory_with_no_record_in_it(tmp_path):
    root = str(tmp_path)
    run_mod.save(a_run(root, "good-run"))
    workspace.create("empty-run", root)
    assert [row["id"] for row in run_mod.rebuild_index(root)] == ["good-run"]


# ------------------------------------------------------------- as a run goes
def test_the_record_is_written_while_the_run_is_still_going(tmp_path):
    """A killed process should leave a readable partial run, not nothing."""
    root = str(tmp_path)
    run = a_run(root)
    persister = run_mod.Persister(root=root)
    persister.run_start(run, {"nodes": []})

    assert run_mod.load(run.workspace) is not None

    run.steps["build"].status = SUCCESS
    persister.step_end(run.steps["build"])
    assert run_mod.load(run.workspace).steps["build"].status == SUCCESS


def test_the_index_records_start_and_end_so_interrupted_runs_are_discoverable(tmp_path):
    root = str(tmp_path)
    run = a_run(root)
    run.status = "running"
    persister = run_mod.Persister(root=root)
    persister.run_start(run, {})
    persister.step_end(run.steps["build"])
    assert run_mod.index(root)[0]["status"] == "running"
    run.status = SUCCESS

    persister.run_end(run)
    assert [row["id"] for row in run_mod.index(root)] == [run.id]


def test_a_skipped_step_is_written_down_too(tmp_path):
    root = str(tmp_path)
    run = a_run(root)
    persister = run_mod.Persister(root=root)
    persister.run_start(run, {})
    persister.step_skipped(run.steps["deploy"], "tests failed")
    assert run_mod.load(run.workspace).steps["deploy"].status == SKIPPED


# -------------------------------------------------------------------- pruning
def test_pruning_keeps_the_newest_and_removes_the_rest(tmp_path):
    root = str(tmp_path)
    for run_id in ("20260916-010000-a", "20260916-020000-b",
                   "20260916-030000-c"):
        run_mod.save(a_run(root, run_id))

    removed = run_mod.prune(keep=1, root=root)
    assert removed == ["20260916-020000-b", "20260916-010000-a"]
    assert workspace.existing_runs(root) == ["20260916-030000-c"]


def test_pruning_updates_the_index(tmp_path):
    root = str(tmp_path)
    for run_id in ("20260916-010000-a", "20260916-020000-b"):
        run_mod.remember(a_run(root, run_id), root)
        run_mod.save(a_run(root, run_id))

    run_mod.prune(keep=1, root=root)
    assert [row["id"] for row in run_mod.index(root)] == ["20260916-020000-b"]


def test_pruning_with_nothing_to_prune_does_nothing(tmp_path):
    assert run_mod.prune(keep=10, root=str(tmp_path)) == []
