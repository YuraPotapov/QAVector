"""Turning the launcher's two output channels into something the pages can show."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import runner as runner_mod
from cms_gui.runner import (LIVE_STATES, SERVER_LOG_LINES, LauncherProcess,
                            RunState, parse_log_line)


# ------------------------------------------------------------------ log lines

def test_parses_the_launchers_console_format():
    record = parse_log_line("13:38:26  INFO    All 1 windows launched.")
    assert record["ts"] == "13:38:26"
    assert record["level"] == "INFO"
    assert record["session"] == ""
    assert record["text"] == "All 1 windows launched."


def test_picks_up_the_session_prefix_of_a_parallel_run():
    record = parse_log_line("13:38:27  WARNING [dev-agent]  Retry #1")
    assert record["session"] == "dev-agent"
    assert record["level"] == "WARNING"
    assert "Retry #1" in record["text"]


def test_an_unrecognisable_line_is_kept_whole():
    record = parse_log_line("Traceback (most recent call last):")
    assert record["text"] == "Traceback (most recent call last):"
    assert record["level"] == ""


# ------------------------------------------------------------------ run state

TREE = {"id": "0", "label": "smoke", "kind": "group", "children": [
    {"id": "0/0", "label": "goto", "kind": "step", "step_index": 0},
    {"id": "0/1", "label": "assert_visible", "kind": "step", "step_index": 1},
]}


def _feed(state, *events):
    for event in events:
        state.handle(event)


def test_a_window_appears_as_soon_as_it_launches(qapp):
    state = RunState()
    _feed(state, {"kind": "window.launched", "session": "dev-admin",
                  "login": "admin", "pid": 42})
    assert [s["name"] for s in state.ordered()] == ["dev-admin"]
    assert state.ordered()[0]["pid"] == 42
    assert state.ordered()[0]["state"] == "launched"


def test_sessions_keep_their_launch_order(qapp):
    state = RunState()
    _feed(state,
          {"kind": "window.launched", "session": "b", "pid": 2},
          {"kind": "window.launched", "session": "a", "pid": 1})
    assert [s["name"] for s in state.ordered()] == ["b", "a"]


def test_steps_progress_from_the_event_stream(qapp):
    state = RunState()
    _feed(state,
          {"kind": "session.attached", "session": "s", "login": "admin"},
          {"kind": "session.start", "session": "s", "scenarios": ["smoke"]},
          {"kind": "flow.start", "session": "s", "scenario": "smoke",
           "tree": TREE, "steps": 2},
          {"kind": "step.start", "session": "s", "index": 0},
          {"kind": "step.end", "session": "s", "index": 0, "status": "pass"})
    session = state.sessions["s"]
    assert session["state"] == "running"
    assert session["total"] == 2 and session["done"] == 1
    assert session["steps"][0]["status"] == "pass"
    assert session["tree"]["children"][1]["label"] == "assert_visible"


def test_a_failed_step_marks_the_session_failed_at_flow_end(qapp):
    state = RunState()
    _feed(state,
          {"kind": "flow.start", "session": "s", "scenario": "smoke",
           "tree": TREE, "steps": 2},
          {"kind": "step.end", "session": "s", "index": 0, "status": "fail",
           "message": "not visible"},
          {"kind": "flow.end", "session": "s", "status": "fail", "passed": 0,
           "total": 2})
    session = state.sessions["s"]
    assert session["state"] == "failed"
    assert session["steps"][0]["message"] == "not visible"
    assert session["flows"] == [{"scenario": "smoke", "status": "fail",
                                 "passed": 0, "total": 2}]


def test_a_second_scenario_resets_the_step_tally(qapp):
    state = RunState()
    _feed(state,
          {"kind": "flow.start", "session": "s", "scenario": "one", "tree": TREE,
           "steps": 2},
          {"kind": "step.end", "session": "s", "index": 0, "status": "pass"},
          {"kind": "flow.end", "session": "s", "status": "pass", "passed": 1,
           "total": 2},
          {"kind": "flow.start", "session": "s", "scenario": "two", "tree": TREE,
           "steps": 2})
    session = state.sessions["s"]
    assert session["scenario"] == "two" and session["done"] == 0
    assert len(session["flows"]) == 1     # the finished one is remembered


def test_attach_failure_is_visible_rather_than_silent(qapp):
    state = RunState()
    _feed(state, {"kind": "session.attach_failed", "session": "s", "login": "x"})
    assert state.sessions["s"]["state"] == "failed"


def test_run_dir_and_summary_are_captured(qapp):
    state = RunState()
    seen = []
    state.run_dir_known.connect(seen.append)
    _feed(state,
          {"kind": "run.dir", "dir": "/tmp/reports/20260813-120000"},
          {"kind": "run.summary", "passed": 2, "total": 3, "exit_code": 1})
    assert state.run_dir == "/tmp/reports/20260813-120000"
    assert seen == ["/tmp/reports/20260813-120000"]
    assert state.summary["passed"] == 2


def test_totals_add_up_across_sessions(qapp):
    state = RunState()
    for name in ("a", "b"):
        _feed(state, {"kind": "flow.start", "session": name, "scenario": "s",
                      "tree": TREE, "steps": 2},
              {"kind": "step.end", "session": name, "index": 0, "status": "pass"})
    assert state.totals() == (2, 4)


def test_an_unknown_event_kind_is_ignored(qapp):
    # A newer core may emit things this GUI has never heard of.
    state = RunState()
    _feed(state, {"kind": "something.new", "session": "s", "detail": 1})
    assert state.ordered() == []


def test_the_worker_count_in_force_comes_off_the_event_stream(qapp):
    state = RunState()
    assert state.workers is None            # a fixed number reports nothing
    _feed(state, {"kind": "governor.limit", "limit": 7, "ceiling": 7,
                  "unit": "sessions", "why": "starting at one per core"})
    assert state.workers["limit"] == 7
    _feed(state, {"kind": "governor.limit", "limit": 6, "ceiling": 7,
                  "unit": "sessions", "why": "cpu at 96%"})
    # The number in force AND why it moved: a count that drops with no reason
    # given is the thing that made the old throttle look broken.
    assert (state.workers["limit"], state.workers["why"]) == (6, "cpu at 96%")


def test_a_new_run_forgets_the_last_run_s_worker_count(qapp):
    state = RunState()
    _feed(state, {"kind": "governor.limit", "limit": 3, "ceiling": 7})
    state.reset()
    assert state.workers is None


def test_a_session_asked_to_stop_says_so_before_the_core_answers(qapp):
    state = RunState()
    _feed(state, {"kind": "window.launched", "session": "dev-agent", "pid": 1})
    state.mark_stopping("dev-agent")
    # The core has to finish the step it is in before it can reply, and a menu
    # entry that looks like it did nothing invites a second click.
    assert state.sessions["dev-agent"]["state"] == "stopping"


def test_the_core_announcing_a_stop_marks_that_session_only(qapp):
    state = RunState()
    _feed(state,
          {"kind": "window.launched", "session": "a", "pid": 1},
          {"kind": "window.launched", "session": "b", "pid": 2},
          {"kind": "session.stopping", "session": "a"})
    assert state.sessions["a"]["state"] == "stopping"
    assert state.sessions["b"]["state"] == "launched"


def test_the_windows_still_up_are_the_ones_stop_can_act_on(qapp):
    """One definition of "still there", shared by the Stop menu and the rail."""
    state = RunState()
    _feed(state,
          {"kind": "window.launched", "session": "a", "pid": 1},
          {"kind": "window.launched", "session": "b", "pid": 2},
          {"kind": "window.launched", "session": "c", "pid": 3})
    state.sessions["b"]["state"] = "passed"
    state.sessions["c"]["state"] = "stopping"
    # "stopping" has already been told to go: there is nothing left to stop.
    assert [s["name"] for s in state.live()] == ["a"]
    # And the whole set is still there, which is what the report is written from.
    assert len(state.sessions) == 3


def test_a_session_that_never_ran_is_still_a_window_to_close(qapp):
    state = RunState()
    _feed(state, {"kind": "window.launched", "session": "a", "pid": 1})
    assert state.sessions["a"]["state"] in LIVE_STATES
    state.reset()
    assert state.live() == []


def _flow(state, name, scenario, labels, status="pass"):
    tree = {"kind": "flow", "label": scenario,
            "children": [{"kind": "step", "step_index": i, "label": l}
                         for i, l in enumerate(labels)]}
    _feed(state, {"kind": "flow.start", "session": name, "scenario": scenario,
                  "tree": tree, "steps": len(labels)})
    for i, _l in enumerate(labels):
        _feed(state, {"kind": "step.start", "session": name, "index": i},
              {"kind": "step.end", "session": name, "index": i, "status": "pass"})
    _feed(state, {"kind": "flow.end", "session": name, "status": status,
                  "passed": len(labels), "total": len(labels)})


def test_every_scenario_a_session_runs_is_kept(qapp):
    # The Run page could only ever show the last scenario, because flow.start
    # overwrote the tree and the steps. The in-page overlay always showed the
    # whole list; this is the state that lets the page match it.
    state = RunState()
    _feed(state, {"kind": "window.launched", "session": "s", "pid": 1},
          {"kind": "session.start", "session": "s",
           "scenarios": ["access_agent", "multicompany_agent"]})
    _flow(state, "s", "access_agent", ["click a", "click b"])
    _flow(state, "s", "multicompany_agent", ["click c"], status="fail")
    runs = state.sessions["s"]["runs"]
    assert list(runs) == ["access_agent", "multicompany_agent"]   # in run order
    assert runs["access_agent"]["status"] == "pass"
    assert runs["multicompany_agent"]["status"] == "fail"
    # And the earlier scenario kept its own steps rather than the later one's.
    assert len(runs["access_agent"]["tree"]["children"]) == 2
    assert len(runs["multicompany_agent"]["tree"]["children"]) == 1


def test_a_finished_scenario_keeps_its_step_marks(qapp):
    state = RunState()
    _feed(state, {"kind": "window.launched", "session": "s", "pid": 1})
    _flow(state, "s", "first", ["a", "b"])
    _flow(state, "s", "second", ["c"])
    first = state.sessions["s"]["runs"]["first"]
    assert [first["steps"][i]["status"] for i in (0, 1)] == ["pass", "pass"]
    assert first["done"] == 2 and first["total"] == 2


def test_a_session_that_failed_once_does_not_report_pass(qapp):
    # The tag said PASS beside a tree with a red mark in it, because flow.end
    # took the latest outcome instead of the worst one.
    state = RunState()
    _feed(state, {"kind": "window.launched", "session": "s", "pid": 1})
    _flow(state, "s", "first", ["a"], status="fail")
    _flow(state, "s", "second", ["b"], status="pass")
    assert state.sessions["s"]["state"] == "failed"


def test_a_session_where_everything_passed_reports_pass(qapp):
    state = RunState()
    _feed(state, {"kind": "window.launched", "session": "s", "pid": 1})
    _flow(state, "s", "first", ["a"])
    _flow(state, "s", "second", ["b"])
    assert state.sessions["s"]["state"] == "passed"


def test_a_scenario_is_filed_under_its_id_and_timed_by_the_launcher(qapp):
    # session.start lists ids. Filed under its name, a scenario with a name: of
    # its own showed twice - done, and again by id as still to come.
    state = RunState()
    _feed(state,
          {"kind": "session.start", "session": "s",
           "scenarios": ["claim75_dashboard_backend"]},
          {"kind": "flow.start", "session": "s", "id": "claim75_dashboard_backend",
           "scenario": "DEMO-75 - backend tests", "tree": TREE, "steps": 2,
           "ts": 1000.0},
          {"kind": "step.end", "session": "s", "index": 0, "status": "pass"},
          {"kind": "flow.end", "session": "s", "status": "pass", "passed": 2,
           "total": 2, "ts": 1072.5})
    runs = state.sessions["s"]["runs"]
    assert list(runs) == ["claim75_dashboard_backend"]
    run = runs["claim75_dashboard_backend"]
    assert run["scenario"] == "DEMO-75 - backend tests"      # still shown by name
    assert run["steps"][0]["status"] == "pass" and run["status"] == "pass"
    assert (run["started"], run["ended"]) == (1000.0, 1072.5)


def test_a_core_without_ids_still_files_by_name(qapp):
    state = RunState()
    _flow(state, "s", "smoke", ["a"])
    run = state.sessions["s"]["runs"]["smoke"]
    assert run["status"] == "pass" and run["started"] and run["ended"]


def test_the_scenarios_finishing_is_not_the_launcher_finishing(qapp):
    # Without --close-after the launcher stays up holding the windows open, so
    # the page waited for a process exit that was minutes away and kept saying
    # RUNNING with a ticking clock.
    state = RunState()
    assert state.flows_finished is False
    _feed(state, {"kind": "run.finished", "exit_code": 1})
    assert state.flows_finished is True and state.exit_code == 1
    state.reset()
    assert state.flows_finished is False


# ------------------------------------------------------------------- starting

def test_a_run_starts_even_where_qt_lacks_the_process_group_api(qapp, tmp_path):
    """PySide6 does not bind QProcess.setCreateProcessArgumentsModifier.

    Calling it unconditionally raised AttributeError inside start(), before the
    process was ever spawned - so on Windows every run sat at "Launching..."
    with nothing behind it. Losing the process group may cost the graceful
    stop; it must never cost the run.
    """
    launcher = LauncherProcess()
    assert not hasattr(launcher._proc or object(), "setCreateProcessArgumentsModifier")

    script = tmp_path / "quiet.py"
    script.write_text("print('{}')", encoding="utf-8")
    assert launcher.start([sys.executable, str(script)]) is True
    assert launcher._proc is not None
    launcher._proc.waitForFinished(15000)


def test_the_process_group_flag_records_whether_it_was_applied(qapp, tmp_path):
    # stop() reads this: signalling a group that was never created reports a
    # stop that did not happen.
    launcher = LauncherProcess()
    script = tmp_path / "quiet.py"
    script.write_text("print('{}')", encoding="utf-8")
    launcher.start([sys.executable, str(script)])
    expected = os.name == "nt" and hasattr(launcher._proc,
                                           "setCreateProcessArgumentsModifier")
    assert launcher._own_group is expected
    launcher._proc.waitForFinished(15000)


# ------------------------------------------------------- server log batches
def _state_with_lines(*events):
    state = RunState()
    state.handle({"kind": "window.launched", "session": "dev-agent",
                  "login": "agent", "pid": 7})
    for event in events:
        state.handle(event)
    return state.sessions["dev-agent"]


def _batch(session="dev-agent", log="app", *texts, **kw):
    level = kw.get("level", "INFO")
    return {"kind": "serverlog.lines", "session": session, "log": log,
            "lines": [{"ts": 1.0, "level": level, "text": t} for t in texts]}


def test_server_lines_land_on_the_session_they_belong_to():
    session = _state_with_lines(_batch("dev-agent", "app", "one", "two"))
    assert [line["text"] for line in session["server"]] == ["one", "two"]
    assert session["server_logs"] == ["app"]


def test_batches_from_several_logs_keep_their_names():
    session = _state_with_lines(_batch("dev-agent", "app", "a"),
                                _batch("dev-agent", "nginx", "b"),
                                _batch("dev-agent", "app", "c"))
    assert [(l["log"], l["text"]) for l in session["server"]] == [
        ("app", "a"), ("nginx", "b"), ("app", "c")]
    # The order the panel's filter offers them in: first seen, first listed.
    assert session["server_logs"] == ["app", "nginx"]


def test_one_sessions_lines_do_not_reach_another():
    # The launcher already decided who each line belongs to; the model must not
    # blur that back together.
    state = RunState()
    state.handle({"kind": "window.launched", "session": "a", "login": "a"})
    state.handle({"kind": "window.launched", "session": "b", "login": "b"})
    state.handle(_batch("a", "app", "only-a"))
    assert [l["text"] for l in state.sessions["a"]["server"]] == ["only-a"]
    assert list(state.sessions["b"]["server"]) == []


def test_the_level_survives_so_the_panel_can_colour_it():
    session = _state_with_lines(_batch("dev-agent", "app", "boom", level="ERROR"))
    assert session["server"][0]["level"] == "ERROR"


def test_a_long_run_does_not_grow_the_model_without_bound():
    texts = ["line %d" % i for i in range(SERVER_LOG_LINES + 50)]
    session = _state_with_lines(_batch("dev-agent", "app", *texts))
    assert len(session["server"]) == SERVER_LOG_LINES
    # And what it keeps is the recent end - the part next to a failure.
    assert session["server"][-1]["text"] == texts[-1]


def test_lines_arriving_before_the_window_announces_itself_are_kept():
    # The reader starts before the first window opens, deliberately: a line
    # written while Chrome is coming up still belongs to that session.
    state = RunState()
    state.handle(_batch("dev-agent", "app", "early"))
    assert [l["text"] for l in state.sessions["dev-agent"]["server"]] == ["early"]


def test_a_batch_without_a_session_is_ignored_rather_than_crashing():
    state = RunState()
    state.handle({"kind": "serverlog.lines", "log": "app",
                  "lines": [{"ts": 1.0, "level": "INFO", "text": "x"}]})
    assert state.sessions == {}


def test_a_run_without_server_logs_has_an_empty_box():
    state = RunState()
    state.handle({"kind": "window.launched", "session": "dev-agent", "login": "a"})
    assert list(state.sessions["dev-agent"]["server"]) == []
    assert state.sessions["dev-agent"]["server_logs"] == []


# --------------------------------------------------------------------- cycles
# A cycle's events reach RunState through the same getattr dispatch every other
# kind uses, so what is worth checking is the model they build - and that an
# unknown one is still a no-op, which is what keeps an older GUI usable against
# a newer core.

def _diamond():
    return {"id": "demo", "name": "Demo cycle",
            "nodes": [{"id": "a", "plugin": "command.shell", "layer": 0,
                       "row": 0},
                      {"id": "b", "plugin": "report.json", "layer": 1,
                       "row": 0}],
            "edges": [{"from": "a", "to": "b", "kind": "dependency"}]}


def _started(state):
    state.handle({"kind": "cycle.run.start", "run_id": "20260917-000000-demo",
                  "cycle": "demo", "name": "Demo cycle",
                  "workspace": "/runs/demo", "jobs": 4,
                  "variables": {"branch": "main"}, "graph": _diamond()})
    return state


def test_a_run_that_is_not_a_cycle_leaves_the_cycle_empty():
    """So a page can ask rather than guess from which fields are filled in."""
    state = RunState()
    state.handle({"kind": "launcher.start"})
    assert state.cycle is None


def test_a_cycle_run_announces_itself_with_its_graph():
    state = _started(RunState())
    assert state.cycle["cycle"] == "demo"
    assert state.cycle["name"] == "Demo cycle"
    assert state.cycle["workspace"] == "/runs/demo"
    assert state.cycle["jobs"] == 4
    assert state.cycle["variables"] == {"branch": "main"}
    assert state.cycle["graph"] == _diamond()


def test_the_graph_is_handed_on_so_a_page_can_draw_it():
    state = RunState()
    seen = []
    state.cycle_graph_known.connect(seen.append)
    _started(state)
    assert seen == [_diamond()]


def test_every_step_has_a_record_before_anything_has_run():
    """Which is what lets a page render the whole cycle on the first event."""
    state = _started(RunState())
    assert sorted(state.cycle["steps"]) == ["a", "b"]
    assert all(one["status"] == "pending" for one in state.cycle["steps"].values())
    assert state.cycle["steps"]["a"]["plugin"] == "command.shell"


def test_a_step_starting_and_ending_is_written_down():
    state = _started(RunState())
    state.handle({"kind": "cycle.step.start", "step": "a",
                  "plugin": "command.shell", "attempt": 1})
    assert state.cycle["steps"]["a"]["status"] == "running"

    state.handle({"kind": "cycle.step.end", "step": "a", "status": "success",
                  "duration_ms": 1200, "attempts": 2, "message": "exited 0",
                  "outputs": {"exit_code": 0}})
    step = state.cycle["steps"]["a"]
    assert step["status"] == "success"
    assert step["duration_ms"] == 1200
    assert step["attempt"] == 2
    assert step["message"] == "exited 0"
    assert step["outputs"] == {"exit_code": 0}


def test_a_page_is_told_which_step_changed():
    """So it repaints one node rather than re-reading the whole model."""
    state = _started(RunState())
    seen = []
    state.cycle_step_changed.connect(seen.append)
    state.handle({"kind": "cycle.step.end", "step": "a", "status": "success"})
    assert seen == ["a"]


def test_a_skipped_step_keeps_the_reason_it_was_skipped():
    state = _started(RunState())
    state.handle({"kind": "cycle.step.skipped", "step": "b",
                  "status": "skipped", "reason": "a failed"})
    assert state.cycle["steps"]["b"]["status"] == "skipped"
    assert state.cycle["steps"]["b"]["message"] == "a failed"


def test_a_retry_moves_the_attempt_count():
    state = _started(RunState())
    state.handle({"kind": "cycle.step.retry", "step": "a", "attempt": 3})
    assert state.cycle["steps"]["a"]["attempt"] == 3


def test_a_waiting_step_is_held_open_rather_than_closed():
    """It asked to be come back to, so it is still the run's business."""
    state = _started(RunState())
    state.handle({"kind": "cycle.step.waiting", "step": "a",
                  "resume_in_ms": 600000, "message": "waiting 10m",
                  "outputs": {"until": "2026-09-21 09:00:00"}})

    step = state.cycle["steps"]["a"]
    assert step["status"] == "waiting"
    assert step["message"] == "waiting 10m"
    assert step["resume_in_ms"] == 600000
    assert step["outputs"]["until"] == "2026-09-21 09:00:00"


def test_waiting_is_not_a_retry_in_the_model_either():
    """A reader who cannot tell the two apart sees a healthy cycle as one
    failing over and over."""
    state = _started(RunState())
    state.handle({"kind": "cycle.step.waiting", "step": "a",
                  "resume_in_ms": 1000, "message": "not yet"})
    assert state.cycle["steps"]["a"]["attempt"] == 0


def test_a_step_that_waited_and_then_finished_reads_as_finished():
    state = _started(RunState())
    state.handle({"kind": "cycle.step.waiting", "step": "a",
                  "resume_in_ms": 1000, "message": "not yet"})
    state.handle({"kind": "cycle.step.end", "step": "a", "status": "success",
                  "duration_ms": 3000, "attempts": 2, "message": "waited 1s"})

    assert state.cycle["steps"]["a"]["status"] == "success"


def test_a_step_s_output_is_kept_with_which_stream_it_came_from():
    state = _started(RunState())
    state.handle({"kind": "cycle.step.log", "step": "a", "stream": "out",
                  "lines": ["one", "two"]})
    state.handle({"kind": "cycle.step.log", "step": "a", "stream": "err",
                  "lines": ["bad"]})
    assert state.cycle["steps"]["a"]["log"] == [("out", "one"), ("out", "two"),
                                                ("err", "bad")]


def test_a_chatty_step_cannot_grow_the_model_without_bound():
    state = _started(RunState())
    for index in range(runner_mod.CYCLE_LOG_LINES * 2):
        state.handle({"kind": "cycle.step.log", "step": "a", "stream": "out",
                      "lines": ["line %d" % index]})
    log = state.cycle["steps"]["a"]["log"]
    assert len(log) == runner_mod.CYCLE_LOG_LINES
    assert log[-1] == ("out", "line %d" % (runner_mod.CYCLE_LOG_LINES * 2 - 1))


def test_the_run_keeps_its_own_log_of_every_step_in_the_order_they_spoke():
    """A cycle runs several steps at once, so "what is happening" is the run's
    output interleaved - not whichever step somebody happened to click."""
    state = _started(RunState())
    state.handle({"kind": "cycle.step.log", "step": "a", "stream": "out",
                  "lines": ["from a"]})
    state.handle({"kind": "cycle.step.log", "step": "b", "stream": "out",
                  "lines": ["from b"]})
    state.handle({"kind": "cycle.step.log", "step": "a", "stream": "err",
                  "lines": ["a again"]})
    assert state.cycle["log"] == [("a", "out", "from a"),
                                  ("b", "out", "from b"),
                                  ("a", "err", "a again")]


def test_the_run_wide_log_is_capped_too():
    state = _started(RunState())
    for index in range(runner_mod.CYCLE_RUN_LOG_LINES + 50):
        state.handle({"kind": "cycle.step.log", "step": "a", "stream": "out",
                      "lines": ["line %d" % index]})
    assert len(state.cycle["log"]) == runner_mod.CYCLE_RUN_LOG_LINES


# ------------------------------------------------------------------- stages
def test_a_stage_is_filed_under_its_step_and_on_the_run():
    state = _started(RunState())
    state.handle({"kind": "cycle.step.stage", "step": "a", "phase": "thinking",
                  "title": "Thinking", "detail": "weighing it",
                  "status": "done"})
    assert state.cycle["steps"]["a"]["stages"] == [
        {"kind": "thinking", "title": "Thinking", "detail": "weighing it",
         "status": "done", "body": {}}]
    assert state.cycle["stages"][0][0] == "a"


def test_a_stage_s_kind_arrives_under_a_name_the_envelope_cannot_take():
    """Every event carries "kind" as its own type. A stage sent under that name
    would be overwritten by "cycle.step.stage" and lose what it was."""
    state = _started(RunState())
    state.handle({"kind": "cycle.step.stage", "step": "a", "phase": "tool",
                  "title": "Read main.py"})
    assert state.cycle["steps"]["a"]["stages"][0]["kind"] == "tool"


def test_a_stage_reaches_the_page_as_a_change_to_its_step():
    state = _started(RunState())
    seen = []
    state.cycle_step_changed.connect(seen.append)
    state.handle({"kind": "cycle.step.stage", "step": "a", "title": "Thinking"})
    assert seen == ["a"]


def test_a_step_that_emits_no_stage_simply_has_none():
    """Which is every plugin but the agent, and must not look like a fault."""
    state = _started(RunState())
    assert state.cycle["steps"]["a"]["stages"] == []


def test_an_agent_that_ran_away_cannot_grow_the_model_without_bound():
    state = _started(RunState())
    for index in range(runner_mod.CYCLE_STAGES + 20):
        state.handle({"kind": "cycle.step.stage", "step": "a",
                      "title": "turn %d" % index})
    assert len(state.cycle["steps"]["a"]["stages"]) == runner_mod.CYCLE_STAGES
    assert len(state.cycle["stages"]) == runner_mod.CYCLE_STAGES


def test_a_stage_for_a_step_the_graph_never_mentioned_is_still_kept():
    state = _started(RunState())
    state.handle({"kind": "cycle.step.stage", "step": "ghost",
                  "title": "Thinking"})
    assert state.cycle["steps"]["ghost"]["stages"][0]["title"] == "Thinking"


def test_an_artifact_is_filed_under_the_step_that_made_it():
    state = _started(RunState())
    state.handle({"kind": "cycle.artifact", "step": "a", "name": "stdout",
                  "path": "steps/a/stdout.log", "type": "log", "bytes": 42})
    assert state.cycle["steps"]["a"]["artifacts"] == [
        {"name": "stdout", "path": "steps/a/stdout.log", "type": "log",
         "bytes": 42}]


def test_the_end_of_a_run_is_recorded_with_its_tally():
    state = _started(RunState())
    state.handle({"kind": "cycle.run.end", "status": "failed", "exit_code": 1,
                  "duration_ms": 5000, "passed": 1, "failed": 1, "skipped": 0,
                  "message": "a failed"})
    assert state.cycle["status"] == "failed"
    assert state.cycle["passed"] == 1
    assert state.exit_code == 1
    assert state.flows_finished is True


def test_an_event_about_a_step_the_graph_never_mentioned_is_kept_anyway():
    """A cycle can be edited between a run starting and somebody reading it."""
    state = _started(RunState())
    state.handle({"kind": "cycle.step.end", "step": "surprise",
                  "status": "success"})
    assert state.cycle["steps"]["surprise"]["status"] == "success"


def test_a_cycle_event_before_a_run_started_is_a_no_op():
    state = RunState()
    state.handle({"kind": "cycle.step.end", "step": "a", "status": "success"})
    assert state.cycle is None


def test_a_cycle_kind_this_version_does_not_know_is_ignored():
    """A newer core talking to an older GUI loses a detail, not a run."""
    state = _started(RunState())
    state.handle({"kind": "cycle.something.new", "step": "a"})
    assert state.cycle["steps"]["a"]["status"] == "pending"


def test_resetting_a_scenario_run_leaves_the_cycle_alone():
    """reset() is what starting a *scenario* run calls. Forgetting the cycle
    there left the Cycles page with a coloured graph beside an empty panel."""
    state = _started(RunState())
    state.reset()
    assert state.cycle is not None


def test_forgetting_the_cycle_is_its_own_call():
    state = _started(RunState())
    state.reset_cycle()
    assert state.cycle is None
    assert not state.cycle_running


def test_a_cycle_is_live_only_between_its_own_start_and_end():
    state = RunState()
    assert not state.cycle_running
    state = _started(state)
    assert state.cycle_running
    state.handle({"kind": "cycle.run.end", "status": "success", "exit_code": 0})
    assert not state.cycle_running


def test_a_core_that_went_without_saying_so_stops_the_cycle():
    """No cycle.run.end ever arrives when the process is killed. The record is
    kept - it is what happened - but nothing still claims to be running."""
    state = _started(RunState())
    state.handle({"kind": "cycle.step.start", "step": "a", "plugin": "x"})
    state.cycle_interrupted()
    assert not state.cycle_running
    assert state.cycle["status"] == "interrupted"
    assert state.cycle["steps"]["a"]["status"] == "cancelled"


# ------------------------------------------------------------ what a run is on
def _cycle_started():
    from cms_gui.runner import RunState
    state = RunState()
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "dev",
                  "graph": {"nodes": [{"id": "todo"}]}})
    return state


def test_a_cycle_run_learns_its_subject():
    state = _cycle_started()
    heard = []
    state.cycle_subject_changed.connect(lambda: heard.append(True))
    state.handle({"kind": "cycle.subject", "run_id": "r",
                  "subject": {"kind": "task", "key": "QA-1", "step": "todo"}})
    assert state.cycle["subject"]["key"] == "QA-1"
    assert heard == [True]


def test_a_cycle_run_says_how_it_began():
    state = _cycle_started()
    state.handle({"kind": "cycle.run.mode", "run_id": "r", "mode": "resume",
                  "resume_count": 2, "kept": ["todo"],
                  "rerun": [{"step": "work", "reason": "failed last time"}]})
    assert state.cycle["mode"]["mode"] == "resume"
    assert state.cycle["mode"]["rerun"][0]["reason"] == "failed last time"


def test_every_revision_round_is_kept():
    state = _cycle_started()
    for number in (1, 2):
        state.handle({"kind": "cycle.revision", "run_id": "r", "number": number,
                      "feedback": "again", "steps": ["plan"]})
    assert [one["number"] for one in state.cycle["revisions"]] == [1, 2]


def test_a_new_run_starts_with_no_subject():
    state = _cycle_started()
    state.handle({"kind": "cycle.subject", "run_id": "r",
                  "subject": {"kind": "task", "key": "QA-1"}})
    state.handle({"kind": "cycle.run.start", "run_id": "r2", "cycle": "dev",
                  "graph": {}})
    assert state.cycle["subject"] == {} and state.cycle["mode"] == {}
