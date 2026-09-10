"""Tests for the JSONL event stream (engine/events.py).

The stream is what a front-end reads instead of parsing log text, so the shape
of a line - and the promise that emitting can never raise into a run - are the
things worth pinning down.
"""

import io
import json

import pytest

from domain.plan import PlanNode, GROUP, STEP
from engine import events


@pytest.fixture(autouse=True)
def reset_stream():
    """Every test starts and ends with the module-level sink disabled."""
    events.close()
    yield
    events.close()


def _capture():
    """Point the stream at an in-memory buffer and return it."""
    buf = io.StringIO()
    events._stream = buf          # what configure("-") does, minus the real stdout
    events._own_stream = False
    events._seq = 0
    return buf


def _lines(buf):
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


# ------------------------------------------------------------------ emitting

def test_disabled_by_default():
    assert not events.enabled()
    events.emit("nothing")        # must not raise, must not write anywhere


def test_emit_writes_one_json_line_per_event():
    buf = _capture()
    events.emit("window.launched", login="admin", pid=42)
    events.emit("windows.ready", count=1)
    lines = _lines(buf)
    assert [l["kind"] for l in lines] == ["window.launched", "windows.ready"]
    assert lines[0]["login"] == "admin" and lines[0]["pid"] == 42


def test_every_event_carries_a_timestamp_and_a_growing_sequence():
    buf = _capture()
    for _ in range(3):
        events.emit("tick")
    lines = _lines(buf)
    assert [l["seq"] for l in lines] == [1, 2, 3]
    assert all(isinstance(l["ts"], float) for l in lines)


def test_unserializable_values_degrade_to_their_string_form():
    # A diagnostics channel must not be the thing that raises mid-run.
    buf = _capture()
    events.emit("odd", value=object())
    assert "object object at" in _lines(buf)[0]["value"]


def test_a_broken_sink_never_raises():
    class Broken(io.StringIO):
        def write(self, _data):
            raise OSError("broken pipe")

    events._stream = Broken()
    events.emit("anything")       # the consumer died; the run carries on


def test_configure_with_a_path_appends_and_close_releases_it(tmp_path):
    path = tmp_path / "events.jsonl"
    assert events.configure(str(path)) is True
    events.emit("first")
    events.close()
    assert events.configure(str(path)) is True
    events.emit("second")
    events.close()
    kinds = [json.loads(l)["kind"] for l in path.read_text(encoding="utf-8").splitlines()]
    assert kinds == ["first", "second"]


def test_configure_with_an_unwritable_path_disables_rather_than_fails(tmp_path):
    assert events.configure(str(tmp_path / "no" / "such" / "dir" / "e.jsonl")) is False
    assert not events.enabled()


def test_configure_none_disables():
    _capture()
    assert events.configure(None) is False
    assert not events.enabled()


def test_emit_artifacts_lists_the_directory(tmp_path):
    buf = _capture()
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    (tmp_path / "screenshot.png").write_bytes(b"")
    events.emit_artifacts(str(tmp_path), scenario="smoke", session="dev-admin")
    line = _lines(buf)[0]
    assert line["kind"] == "artifacts.written"
    assert line["files"] == ["result.json", "screenshot.png"]
    assert line["scenario"] == "smoke" and line["session"] == "dev-admin"


def test_emit_artifacts_on_a_missing_directory_is_silent(tmp_path):
    buf = _capture()
    events.emit_artifacts(str(tmp_path / "gone"))
    assert _lines(buf) == []


# ------------------------------------------------------------ EventObserver

def _plan():
    """A two-step plan tree, as the compiler builds one."""
    return PlanNode(id="0", label="smoke", kind=GROUP, children=[
        PlanNode(id="0/0", label="goto", kind=STEP, step_index=0, action="goto"),
        PlanNode(id="0/1", label="assert_visible", kind=STEP, step_index=1,
                 action="assert_visible"),
    ])


def test_observer_tags_every_event_with_its_session():
    buf = _capture()
    observer = events.EventObserver("dev-admin")
    observer.session_start(["smoke"])
    observer.flow_start(_plan(), role="Admin")
    observer.step_start(0)
    observer.step_end(0, "pass", 1, "ok")
    observer.flow_end("pass", 1, 2)
    assert {l["session"] for l in _lines(buf)} == {"dev-admin"}


def test_flow_start_carries_the_plan_tree_the_hud_draws():
    buf = _capture()
    events.EventObserver("s").flow_start(_plan(), role="Admin")
    line = _lines(buf)[0]
    assert line["kind"] == "flow.start"
    assert line["scenario"] == "smoke" and line["role"] == "Admin" and line["steps"] == 2
    assert [c["label"] for c in line["tree"]["children"]] == ["goto", "assert_visible"]


def test_flow_start_names_the_scenario_by_id_as_well_as_by_name():
    # session.start lists ids; without the id here a consumer cannot tell that
    # "Smoke - app loads" and "demo_smoke" are one scenario.
    buf = _capture()
    events.EventObserver("s").flow_start(_plan(), role="Admin", scenario_id="smoke_id")
    line = _lines(buf)[0]
    assert line["id"] == "smoke_id" and line["scenario"] == "smoke"


def test_the_tee_passes_the_scenario_id_on():
    seen = []

    class Observer:
        def flow_start(self, root, role=None, scenario_id=None):
            seen.append(scenario_id)

    events.Tee([Observer()]).flow_start(_plan(), role="Admin", scenario_id="x")
    assert seen == ["x"]


def test_step_end_reports_status_attempts_and_message():
    buf = _capture()
    events.EventObserver("s").step_end(3, "fail", 2, "not visible")
    line = _lines(buf)[0]
    assert (line["index"], line["status"], line["attempts"], line["message"]) == (
        3, "fail", 2, "not visible")


def test_observer_does_not_duplicate_log_records():
    # They already reach the consumer on stderr; forwarding them here would
    # double every line.
    buf = _capture()
    events.EventObserver("s").log("INFO", "hello")
    events.EventObserver("s").mark("#id", "click", None)
    assert _lines(buf) == []


# ---------------------------------------------------------------------- Tee

class _Recorder:
    enabled = True

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record


def test_tee_fans_each_hook_out_to_every_observer():
    a, b = _Recorder(), _Recorder()
    tee = events.Tee([a, b])
    tee.session_start(["smoke"])
    tee.step_end(1, "pass", 1, "ok")
    tee.flow_end("pass", 2, 2)
    assert [c[0] for c in a.calls] == ["session_start", "step_end", "flow_end"]
    assert [c[0] for c in b.calls] == [c[0] for c in a.calls]


def test_tee_survives_one_observer_raising():
    # The HUD talks to a live page, which can vanish mid-run; the event stream
    # must still get its events.
    class Exploding:
        enabled = True

        def step_start(self, _index):
            raise RuntimeError("page closed")

    good = _Recorder()
    events.Tee([Exploding(), good]).step_start(0)
    assert [c[0] for c in good.calls] == ["step_start"]


def test_tee_is_enabled_when_any_observer_is():
    class Off:
        enabled = False

    assert events.Tee([Off(), _Recorder()]).enabled is True
    assert events.Tee([Off()]).enabled is False
