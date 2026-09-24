"""The observer protocol, and what reaches the event stream."""

import json

import pytest

from cycle import workspace as workspace_mod
from cycle.bus import (EventObserver, JsonlObserver, NullObserver, Recorder,
                       Tee)
from domain.cycle import Artifact, CycleRun, StepRun, FAILED, SUCCESS
from engine import events


@pytest.fixture
def stream(tmp_path):
    """Capture the JSONL the observer writes, as parsed events."""
    path = str(tmp_path / "events.jsonl")
    events.configure(path)
    yield lambda: [json.loads(line)
                   for line in open(path, encoding="utf-8")
                   if line.strip()]
    events.close()


def a_run():
    return CycleRun(id="20260916-120000-demo", cycle_id="demo",
                    cycle_name="Demo", workspace="/runs/demo",
                    variables={"branch": "main"},
                    steps={"a": StepRun("a", plugin="command.shell",
                                        status=SUCCESS, duration_ms=1234.56,
                                        attempts=2, message="exited 0",
                                        outputs={"exit_code": 0})})


# ------------------------------------------------------------------ the protocol
def test_the_null_observer_answers_every_call_and_does_nothing():
    """It is the interface as well as the default, so it must be complete."""
    observer = NullObserver()
    run = a_run()
    observer.run_start(run, {"nodes": []}, 4)
    observer.step_start(run.steps["a"])
    observer.step_end(run.steps["a"])
    observer.step_retry(run.steps["a"], 2, 1.0, "again")
    observer.step_skipped(run.steps["a"], "disabled")
    observer.step_log("a", "out", ["line"])
    observer.artifact("a", Artifact("log", "steps/a/out.log", "a"))
    observer.run_end(run)


def test_every_observer_implements_the_whole_protocol():
    """A missing method would be an AttributeError in the middle of a run."""
    expected = {name for name in dir(NullObserver) if not name.startswith("_")}
    for kind in (EventObserver, Tee, Recorder):
        assert expected <= {name for name in dir(kind) if not name.startswith("_")}


# ------------------------------------------------------------------ the stream
def test_the_run_is_announced_with_its_graph(stream):
    observer = EventObserver()
    run = a_run()
    observer.run_start(run, {"nodes": [{"id": "a"}], "edges": []}, jobs=4)

    event = stream()[0]
    assert event["kind"] == "cycle.run.start"
    assert event["run_id"] == run.id
    assert event["cycle"] == "demo"
    assert event["name"] == "Demo"
    assert event["workspace"] == "/runs/demo"
    assert event["jobs"] == 4
    assert event["variables"] == {"branch": "main"}
    assert event["graph"]["nodes"] == [{"id": "a"}]


def test_the_observer_learns_the_run_id_from_the_run(stream):
    """So a caller does not have to hand it over twice and get them out of step."""
    observer = EventObserver()
    run = a_run()
    observer.run_start(run, {}, 1)
    observer.step_start(run.steps["a"])
    assert {event["run_id"] for event in stream()} == {run.id}


def test_a_step_starting_says_which_attempt_it_is(stream):
    run = a_run()
    EventObserver("r").step_start(run.steps["a"], attempt=3)

    event = stream()[0]
    assert event["kind"] == "cycle.step.start"
    assert event["step"] == "a"
    assert event["plugin"] == "command.shell"
    assert event["attempt"] == 3


def test_a_step_ending_carries_what_it_came_to(stream):
    run = a_run()
    EventObserver("r").step_end(run.steps["a"])

    event = stream()[0]
    assert event["kind"] == "cycle.step.end"
    assert event["status"] == SUCCESS
    assert event["attempts"] == 2
    assert event["message"] == "exited 0"
    assert event["outputs"] == {"exit_code": 0}
    assert event["duration_ms"] == 1234.6      # rounded; a front-end shows 1.2s


def test_a_retry_says_how_long_the_wait_will_be(stream):
    run = a_run()
    EventObserver("r").step_retry(run.steps["a"], 2, 1.5, "not yet")

    event = stream()[0]
    assert event["kind"] == "cycle.step.retry"
    assert event["attempt"] == 2
    assert event["delay_ms"] == 1500


def test_a_skipped_step_says_why(stream):
    run = a_run()
    run.steps["a"].status = "skipped"
    EventObserver("r").step_skipped(run.steps["a"], "tests failed")

    event = stream()[0]
    assert event["kind"] == "cycle.step.skipped"
    assert event["reason"] == "tests failed"


def test_output_travels_as_a_batch_of_lines(stream):
    """One event per line would serialise every other step on the stream's lock."""
    EventObserver("r").step_log("a", "out", ["one", "two", "three"])

    event = stream()[0]
    assert event["kind"] == "cycle.step.log"
    assert event["stream"] == "out"
    assert event["lines"] == ["one", "two", "three"]


def test_an_artifact_is_announced_with_where_it_is(stream):
    EventObserver("r").artifact("a", Artifact("log", "steps/a/out.log", "a",
                                              name="stdout", bytes=42))
    event = stream()[0]
    assert event["kind"] == "cycle.artifact"
    assert event["path"] == "steps/a/out.log"
    assert event["name"] == "stdout"
    assert event["bytes"] == 42


def test_the_end_of_a_run_counts_how_its_steps_went(stream):
    run = a_run()
    run.status = FAILED
    run.duration_ms = 5000.0
    run.steps["b"] = StepRun("b", status=FAILED)
    run.steps["c"] = StepRun("c", status="skipped")
    EventObserver("r").run_end(run)

    event = stream()[0]
    assert event["kind"] == "cycle.run.end"
    assert event["status"] == FAILED
    assert event["exit_code"] == 1
    assert event["passed"] == 1
    assert event["failed"] == 1
    assert event["skipped"] == 1


def test_every_kind_a_run_can_emit_starts_with_cycle(stream):
    """So a front-end can route them all with one prefix check."""
    observer = EventObserver()
    run = a_run()
    observer.run_start(run, {}, 1)
    observer.step_start(run.steps["a"])
    observer.step_end(run.steps["a"])
    observer.step_retry(run.steps["a"], 2, 0.0)
    observer.step_skipped(run.steps["a"], "why")
    observer.step_log("a", "out", ["x"])
    observer.artifact("a", Artifact("log", "p", "a"))
    observer.run_end(run)

    kinds = [event["kind"] for event in stream()]
    assert all(kind.startswith("cycle.") for kind in kinds)
    assert len(kinds) == 8


def test_an_event_kind_maps_onto_a_handler_name_the_gui_already_looks_for(stream):
    """RunState dispatches getattr(self, "_on_" + kind.replace(".", "_"))."""
    observer = EventObserver()
    observer.run_start(a_run(), {}, 1)
    kind = stream()[0]["kind"]
    assert "_on_" + kind.replace(".", "_") == "_on_cycle_run_start"


def test_writing_to_a_closed_stream_is_survived():
    """The consumer going away must cost a dropped line, not a failed run."""
    events.close()
    EventObserver("r").step_log("a", "out", ["line"])   # must not raise


# --------------------------------------------------------------------- fanning
def test_a_tee_reaches_every_observer():
    first, second = Recorder(), Recorder()
    Tee([first, second]).step_log("a", "out", ["x"])
    assert first.kinds() == second.kinds() == ["step_log"]


def test_a_tee_ignores_the_observers_that_are_not_there():
    """So a caller can build one from optional parts without filtering first."""
    only = Recorder()
    Tee([None, only, None]).step_log("a", "out", ["x"])
    assert only.kinds() == ["step_log"]


def test_one_observer_breaking_does_not_stop_the_others():
    class Broken:
        def step_end(self, step_run):
            raise RuntimeError("no")

    working = Recorder()
    Tee([Broken(), working]).step_end(StepRun("a"))
    assert working.kinds() == ["step_end"]


def test_a_tee_passes_every_call_through_with_its_arguments():
    seen = Recorder()
    tee = Tee([seen])
    run = a_run()
    tee.run_start(run, {"nodes": []}, 7)
    tee.step_start(run.steps["a"], attempt=2)
    tee.step_retry(run.steps["a"], 2, 1.0, "again")
    tee.step_skipped(run.steps["a"], "because")
    tee.artifact("a", Artifact("log", "p", "a", name="out"))
    tee.run_end(run)

    assert seen.of("run_start")[0]["jobs"] == 7
    assert seen.of("step_start")[0]["attempt"] == 2
    assert seen.of("step_retry")[0]["attempt"] == 2
    assert seen.of("step_skipped")[0]["reason"] == "because"
    assert seen.of("artifact")[0]["name"] == "out"


# ------------------------------------------------------------------- recording
def test_the_recorder_keeps_the_calls_in_order():
    recorder = Recorder()
    run = a_run()
    recorder.run_start(run, {}, 1)
    recorder.step_start(run.steps["a"])
    recorder.step_end(run.steps["a"])
    recorder.run_end(run)
    assert recorder.kinds() == ["run_start", "step_start", "step_end", "run_end"]


def test_the_recorder_can_be_asked_for_one_kind():
    recorder = Recorder()
    recorder.step_log("a", "out", ["one"])
    recorder.step_log("b", "err", ["two"])
    assert [call["step"] for call in recorder.of("step_log")] == ["a", "b"]


def test_asking_for_a_kind_that_never_happened_gives_nothing():
    assert Recorder().of("run_end") == []


# ----------------------------------------------------- the run's own record
def _lines(workspace):
    with open(workspace_mod.log_path(workspace), encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_the_run_keeps_a_copy_of_everything_it_said(tmp_path, stream):
    """Without it, a run's stages and output existed only on a pipe - so a
    front-end that was not watching at the time had no way back to them."""
    workspace = str(tmp_path / "run")
    observer = JsonlObserver(workspace, "20260916-120000-demo")
    run = a_run()
    observer.run_start(run, {"nodes": [{"id": "a"}]}, 2)
    observer.step_stage("a", {"kind": "tool", "title": "Read main.py"})
    observer.run_end(run)
    observer.close()

    written = _lines(workspace)
    assert [one["kind"] for one in written] == [
        "cycle.run.start", "cycle.step.stage", "cycle.run.end"]
    assert [one["seq"] for one in written] == [1, 2, 3]
    assert written[1]["title"] == "Read main.py"


def test_the_record_and_the_stream_say_exactly_the_same_thing(tmp_path, stream):
    """One implementation of the payloads, so the file a reader replays and the
    stream it already understands can never drift apart."""
    workspace = str(tmp_path / "run")
    record = JsonlObserver(workspace, "r")
    live = EventObserver("r")
    run = a_run()
    for observer in (record, live):
        observer.step_end(run.steps["a"])
        observer.step_log("a", "out", ["one", "two"])
    record.close()

    def bare(event):
        return {k: v for k, v in event.items() if k not in ("ts", "seq")}

    assert [bare(one) for one in _lines(workspace)] == [bare(one)
                                                        for one in stream()]


def test_a_record_that_cannot_be_written_is_simply_not_written(tmp_path):
    """A diagnostic is never the reason a run fails."""
    blocked = tmp_path / "run"
    blocked.write_text("not a directory")
    observer = JsonlObserver(str(blocked), "r")
    observer.run_start(a_run(), {}, 1)
    observer.close()
