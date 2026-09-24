"""Reading the last cycle run back, after the application has been restarted.

What matters here is that a restored run is the *same* run: the model this
produces has to be indistinguishable from the one the events built live, or the
page comes back showing something subtly untrue about last night.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import cyclereplay                                   # noqa: E402
from cms_gui import history as history_mod                        # noqa: E402
from cms_gui.runner import RunState                               # noqa: E402

GRAPH = {"id": "demo", "name": "Demo cycle", "nodes":
         [{"id": "a", "label": "A", "plugin": "command.shell", "needs": [],
           "layer": 0, "row": 0},
          {"id": "b", "label": "B", "plugin": "agent.review", "needs": ["a"],
           "layer": 1, "row": 0}],
         "edges": [{"from": "a", "to": "b", "kind": "dependency"}]}


def events(ended=True):
    """One run, as the core writes it into the run's own directory."""
    stream = [
        {"kind": "cycle.run.start", "run_id": "r", "cycle": "demo",
         "name": "Demo cycle", "graph": GRAPH, "jobs": 2, "variables": {}},
        {"kind": "cycle.step.start", "run_id": "r", "step": "a",
         "plugin": "command.shell", "attempt": 1},
        {"kind": "cycle.step.log", "run_id": "r", "step": "a",
         "stream": "out", "lines": ["building"]},
        {"kind": "cycle.step.end", "run_id": "r", "step": "a",
         "status": "success", "duration_ms": 12.0, "attempts": 1},
        {"kind": "cycle.step.start", "run_id": "r", "step": "b",
         "plugin": "agent.review", "attempt": 1},
        {"kind": "cycle.step.stage", "run_id": "r", "step": "b",
         "phase": "thinking", "title": "Thinking"},
    ]
    if ended:
        stream += [
            {"kind": "cycle.step.end", "run_id": "r", "step": "b",
             "status": "success", "duration_ms": 34.0, "attempts": 1},
            {"kind": "cycle.run.end", "run_id": "r", "status": "success",
             "exit_code": 0, "passed": 2, "failed": 0, "skipped": 0},
        ]
    return stream


def a_run(tmp_path, stream=None, name="run"):
    """A run directory with a written event stream; returns its path."""
    where = tmp_path / name
    (where / "logs").mkdir(parents=True)
    with open(where / "logs" / "cycle.jsonl", "w", encoding="utf-8") as handle:
        for event in (events() if stream is None else stream):
            handle.write(json.dumps(event) + "\n")
    return str(where)


class FakeHistory:
    def __init__(self, entries):
        self._entries = entries

    def entries(self, kind=None):
        return [one for one in self._entries
                if kind is None or one.get("kind") == kind]


# --------------------------------------------------------------- reading it
def test_a_run_comes_back_with_everything_it_reported(tmp_path, qapp):
    state = RunState()
    assert cyclereplay.restore(a_run(tmp_path), state)

    assert state.cycle["cycle"] == "demo"
    assert state.cycle["status"] == "success"
    assert state.cycle["steps"]["a"]["status"] == "success"
    assert state.cycle["steps"]["a"]["log"] == [("out", "building")]
    assert state.cycle["stages"][0][0] == "b"
    assert state.cycle["stages"][0][1]["title"] == "Thinking"
    assert state.cycle["graph"]["nodes"][0]["id"] == "a"


def test_a_restored_run_is_not_a_running_one(tmp_path, qapp):
    state = RunState()
    cyclereplay.restore(a_run(tmp_path), state)
    assert not state.cycle_running


def test_a_run_the_application_was_killed_during_says_so(tmp_path, qapp):
    """No cycle.run.end was ever written. The record is still worth having -
    it is what happened - but nothing may go on claiming it is in progress."""
    state = RunState()
    cyclereplay.restore(a_run(tmp_path, events(ended=False)), state)

    assert not state.cycle_running
    assert state.cycle["status"] == "interrupted"
    assert state.cycle["steps"]["b"]["status"] == "cancelled"
    assert state.cycle["steps"]["a"]["status"] == "success"   # that one finished


def test_the_model_is_told_once_rather_than_per_event(tmp_path, qapp):
    """A page listening would otherwise repaint itself for every line of a run
    that has already finished."""
    state = RunState()
    seen = []
    state.changed.connect(lambda: seen.append("changed"))
    state.cycle_step_changed.connect(lambda step: seen.append(step))
    cyclereplay.restore(a_run(tmp_path), state)
    assert "a" not in seen and "b" not in seen
    assert seen.count("changed") <= 3


def test_the_graph_is_announced_so_the_page_can_draw_it(tmp_path, qapp):
    state = RunState()
    drawn = []
    state.cycle_graph_known.connect(drawn.append)
    cyclereplay.restore(a_run(tmp_path), state)
    assert drawn and drawn[0]["id"] == "demo"


def test_where_the_run_wrote_its_files_comes_back_too(tmp_path, qapp):
    """So the Artifacts page can open them again. run.dir is the launcher's
    event, not the cycle's, so it is not in the file - the history knows it."""
    state = RunState()
    where = a_run(tmp_path)
    cyclereplay.restore(where, state)
    assert state.run_dir == where


# ------------------------------------------------------------- what it skips
def test_a_line_half_written_when_the_core_died_is_skipped(tmp_path, qapp):
    where = a_run(tmp_path)
    with open(cyclereplay.log_path(where), "a", encoding="utf-8") as handle:
        handle.write('{"kind": "cycle.step.stage", "step": "b", "titl')
    state = RunState()
    assert cyclereplay.restore(where, state)
    assert len(state.cycle["stages"]) == 1


def test_a_run_directory_that_is_not_there_restores_nothing(tmp_path, qapp):
    state = RunState()
    assert not cyclereplay.restore(str(tmp_path / "gone"), state)
    assert state.cycle is None


def test_an_empty_stream_restores_nothing(tmp_path, qapp):
    state = RunState()
    assert not cyclereplay.restore(a_run(tmp_path, []), state)
    assert state.cycle is None


def test_the_run_that_started_it_is_read_however_long_the_stream_is(tmp_path,
                                                                    qapp):
    """The cap keeps the tail, and the first line whatever happens: it is the
    run starting, and without it nothing else has a graph to attach to."""
    chatty = events()[:1] + [
        {"kind": "cycle.step.stage", "run_id": "r", "step": "b",
         "phase": "tool", "title": "stage %d" % n} for n in range(50)]
    state = RunState()
    where = a_run(tmp_path, chatty)
    assert cyclereplay.restore(where, state, limit=10)
    assert state.cycle["cycle"] == "demo"          # the start survived the cap
    assert state.cycle["stages"][-1][1]["title"] == "stage 49"
    assert len(state.cycle["stages"]) == 9


# ------------------------------------------------------ finding the last one
def test_switching_cycles_recovers_that_cycles_interrupted_run(tmp_path, qapp):
    where = a_run(tmp_path, events(ended=False))
    history = FakeHistory([
        {"kind": history_mod.CYCLE, "run_dir": "some-newer-run",
         "config": {"cycle": "other"}},
        {"kind": history_mod.CYCLE, "run_dir": where, "config": {"cycle": "demo"}},
    ])
    state = RunState()
    state.cycle = {"cycle": "other", "status": "success", "run_id": "newer"}
    assert cyclereplay.restore_cycle(history, state, "demo")
    assert state.cycle["cycle"] == "demo"
    assert state.cycle["status"] == "interrupted"
    assert state.cycle["run_id"] == "r"
    assert not cyclereplay.restore_cycle(history, state, "demo")


def test_switching_cycles_does_not_replace_a_live_run(tmp_path, qapp):
    where = a_run(tmp_path, events(ended=False))
    history = FakeHistory([
        {"kind": history_mod.CYCLE, "run_dir": where, "config": {"cycle": "demo"}},
    ])
    state = RunState()
    state.handle({"kind": "cycle.run.start", "run_id": "live", "cycle": "other",
                  "graph": dict(GRAPH, id="other")})
    assert not cyclereplay.restore_cycle(history, state, "demo")
    assert state.cycle["run_id"] == "live"


def test_the_newest_cycle_run_with_a_directory_is_the_one_read(tmp_path):
    older = a_run(tmp_path, name="older")
    newer = a_run(tmp_path, name="newer")
    history = FakeHistory([
        {"kind": history_mod.CYCLE, "run_dir": newer},
        {"kind": history_mod.CYCLE, "run_dir": older},
    ])
    assert cyclereplay.last_run_dir(history) == newer


def test_an_entry_whose_run_directory_has_gone_is_passed_over(tmp_path):
    kept = a_run(tmp_path, name="kept")
    history = FakeHistory([
        {"kind": history_mod.CYCLE, "run_dir": str(tmp_path / "deleted")},
        {"kind": history_mod.CYCLE, "run_dir": ""},
        {"kind": history_mod.CYCLE, "run_dir": kept},
    ])
    assert cyclereplay.last_run_dir(history) == kept


def test_no_cycle_has_ever_run_here(tmp_path):
    assert cyclereplay.last_run_dir(FakeHistory([])) == ""
    assert cyclereplay.last_run_dir(None) == ""


def test_restoring_the_last_one_names_the_cycle_it_restored(tmp_path, qapp):
    state = RunState()
    history = FakeHistory([{"kind": history_mod.CYCLE,
                            "run_dir": a_run(tmp_path)}])
    assert cyclereplay.restore_last(history, state) == "demo"


def test_restoring_survives_a_history_that_cannot_be_read(qapp):
    """Coming back to a page as it was is a courtesy; it may cost only itself."""
    class Broken:
        def entries(self, kind=None):
            raise OSError("no")

    assert cyclereplay.restore_last(Broken(), RunState()) == ""
