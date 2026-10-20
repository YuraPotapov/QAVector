"""Business outcomes in live events, legacy replay, Subjects and Search."""

import json

import pytest

from cms_gui import cyclereplay, cycleresult, cyclesubjects, theme
from cms_gui.runner import RunState


def projection(scope="partial", outcome="partial_completed", completed=1, selected=1, total=68):
    return {"scope": scope, "outcome": outcome, "execution_status": "completed",
            "coverage": {"completed": completed, "selected": selected, "total": total},
            "current_phase": "Prepare"}


def stream(legacy=False):
    fields = {} if legacy else projection()
    events = [
        {"kind": "cycle.run.start", "run_id": "r", "cycle": "demo", "graph": {"nodes": [
            {"id": "a", "plugin": "command.shell"}, {"id": "b", "plugin": "command.shell"}]}},
        {"kind": "cycle.run.mode", "mode": "partial", "kept": [], "rerun": [{"step": "a"}]},
        {"kind": "cycle.step.end", "step": "b", "status": "skipped", "message": "not part of this run",
         "reason_code": "not_selected", "blocked_by": ""},
        {"kind": "cycle.step.end", "step": "a", "status": "success"},
        {"kind": "cycle.run.end", "status": "success", **fields},
    ]
    return events


@pytest.mark.parametrize("legacy", [False, True])
def test_live_and_replayed_runs_have_the_same_partial_outcome(tmp_path, qapp, legacy):
    events = stream(legacy)
    live = RunState()
    for event in events:
        live.handle(event)
    path = tmp_path / "logs"
    path.mkdir()
    (path / "cycle.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    replayed = RunState()
    assert cyclereplay.restore(str(tmp_path), replayed)
    for key in cycleresult.FIELDS:
        assert live.cycle.get(key) == replayed.cycle.get(key)
    assert live.cycle["scope"] == "partial"
    assert live.cycle["outcome"] == "partial_completed"
    assert live.cycle["coverage"] == {"completed": 1, "selected": 1, "total": 2 if legacy else 68}


def test_borrowed_results_are_excluded_from_legacy_coverage(qapp):
    events = stream(legacy=True)
    events[2].update(status="success", message="reused from earlier (success)")
    state = RunState()
    for event in events:
        state.handle(event)
    assert state.cycle["coverage"] == {"completed": 1, "selected": 1, "total": 2}


@pytest.mark.parametrize("selected", [["a"], ["a", "b"]])
def test_resume_outcome_uses_the_preserved_selection_in_live_and_replay(tmp_path, qapp, selected):
    events = [dict(stream()[0], selected_steps=selected),
              {"kind": "cycle.run.mode", "mode": "resume", "resume_count": 1, "source_run": "r"},
              {"kind": "cycle.step.end", "step": "a", "status": "success"},
              {"kind": "cycle.step.end", "step": "b", "status": "success" if "b" in selected else "skipped",
               "reason_code": "" if "b" in selected else "not_selected"},
              {"kind": "cycle.run.end", "run_id": "r", "status": "success"}]
    live = RunState()
    for event in events:
        live.handle(event)
    restored = RunState()
    assert cyclereplay.restore(str(tmp_path), restored, events=events)
    expected = "partial_completed" if selected == ["a"] else "workflow_completed"
    for state in (live, restored):
        assert state.cycle["scope"] == "resume"
        assert state.cycle["outcome"] == expected
        assert state.cycle["coverage"] == {"completed": len(selected), "selected": len(selected), "total": 2}
        assert cycleresult.completion(state.cycle)[1] == ("neutral" if selected == ["a"] else "ok")


@pytest.mark.parametrize("outside", [
    {"status": "skipped", "reason_code": "not_selected"},
    {"status": "skipped", "message": "not part of this run"},
    {"status": "success", "source_run": "previous"},
    {"status": "success", "message": "reused from previous (success)"},
])
def test_legacy_resumed_partial_selection_can_be_proven_from_saved_steps(qapp, outside):
    state = RunState()
    for event in [stream()[0], {"kind": "cycle.run.mode", "mode": "resume", "resume_count": 1},
                  {"kind": "cycle.step.end", "step": "a", "status": "success"},
                  {"kind": "cycle.step.end", "step": "b", **outside},
                  {"kind": "cycle.run.end", "status": "success"}]:
        state.handle(event)
    assert state.cycle["outcome"] == "partial_completed"
    assert state.cycle["coverage"] == {"completed": 1, "selected": 1, "total": 2}
    assert cyclesubjects.search_state(state.cycle) == ("Partial run completed · 1/2 steps", "neutral", False)


def test_full_resume_with_a_conditional_skip_is_still_a_completed_workflow(qapp):
    state = RunState()
    for event in [stream()[0], {"kind": "cycle.run.mode", "mode": "resume", "resume_count": 1},
                  {"kind": "cycle.step.end", "step": "a", "status": "success"},
                  {"kind": "cycle.step.end", "step": "b", "status": "skipped", "reason_code": "condition_false"},
                  {"kind": "cycle.run.end", "status": "success"}]:
        state.handle(event)
    assert state.cycle["coverage"] == {"completed": 1, "selected": 2, "total": 2}
    assert state.cycle["outcome"] == "workflow_completed"
    assert cycleresult.completion(state.cycle) == ("Workflow completed · 1/2 steps", "ok")


def test_new_core_outcome_wins_when_some_step_events_are_missing(qapp):
    state = RunState()
    events = stream()
    state.handle(events[0])
    state.handle(events[-1])
    assert {key: state.cycle[key] for key in cycleresult.FIELDS} == projection()


def test_interruption_withdraws_the_running_outcome(qapp):
    state = RunState()
    state.handle(stream()[0])
    state.cycle_interrupted()
    assert state.cycle["execution_status"] == state.cycle["outcome"] == "interrupted"


@pytest.mark.parametrize("waiting_for", ["poll", "node_approval"])
def test_process_loss_interrupts_waiting_runs_without_a_safe_pause(qapp, waiting_for):
    state = RunState()
    state.handle(stream()[0])
    if waiting_for == "poll":
        state.handle({"kind": "cycle.step.start", "step": "a"})
        state.handle({"kind": "cycle.step.waiting", "step": "a", "message": "Waiting for deployment"})
    else:
        state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": {
            "id": "approval", "step": "a", "status": "pending", "fingerprint": "inputs"}})
    state.handle({"kind": "cycle.run.state", "run_id": "r", "status": "waiting", "paused": False})
    state.cycle_interrupted()
    assert not state.cycle_running
    assert state.cycle["status"] == state.cycle["execution_status"] == state.cycle["outcome"] == "interrupted"
    if waiting_for == "poll":
        assert state.cycle["steps"]["a"]["status"] == "cancelled"
        assert state.cycle["steps"]["a"]["interrupted"]
    else:
        assert state.cycle["node_approvals"]["approval"]["status"] == "pending"


def test_process_loss_preserves_an_explicit_safe_pause(qapp):
    state = RunState()
    state.handle(stream()[0])
    state.handle({"kind": "cycle.run.state", "run_id": "r", "status": "waiting", "paused": True,
                  "execution_status": "paused", "pause_reason": "phase_boundary", "outcome": "task_prepared"})
    state.cycle_interrupted()
    assert not state.cycle_running
    assert state.cycle["status"] == "waiting" and state.cycle["paused"]
    assert state.cycle["execution_status"] == "paused" and state.cycle["outcome"] == "task_prepared"


@pytest.mark.parametrize("fields,label,tone", [
    (projection(), "Partial run completed · 1/68 steps", "neutral"),
    (projection(outcome="no_work_found"), "No work found", "neutral"),
    (projection(outcome="workflow_incomplete"), "Workflow incomplete · 1/68 steps", "neutral"),
    (projection(scope="full", outcome="workflow_completed", completed=68, selected=68),
     "Workflow completed · 68/68 steps", "accent"),
    (projection(scope="resume", outcome="workflow_completed"), "Partial run completed · 1/68 steps", "neutral"),
    ({}, "Execution completed", "neutral"),
])
def test_subjects_and_search_never_call_partial_or_unknown_success_passed(fields, label, tone):
    session = {"status": "success", **fields}
    assert cyclesubjects.status_word(session) == label
    assert cyclesubjects.search_state(session) == (label, tone, False)
    assert cyclesubjects.status_colour(session) == (theme.OK if tone == "accent" else theme.NEUTRAL[500])


def test_remembered_commit_cannot_relabel_a_new_partial_run_as_complete():
    session = {"status": "success", "memory": {"fields": {"state": "committed"}}, **projection()}
    assert cyclesubjects.search_state(session)[1] == "neutral"
    assert cyclesubjects.status_word(session).startswith("Partial run")


def test_details_explain_selected_coverage_and_phase():
    text = cyclesubjects.details_html({"status": "success", **projection()})
    assert "1/1 selected steps completed" in text and "68 total" in text
    assert "Phase: Prepare" in text


def test_skip_reason_and_cause_survive_live_projection(qapp):
    state = RunState()
    state.handle(stream()[0])
    state.handle({"kind": "cycle.step.skipped", "step": "b", "reason": "a failed",
                  "reason_code": "blocked", "blocked_by": "a"})
    assert state.cycle["steps"]["b"]["reason_code"] == "blocked"
    assert state.cycle["steps"]["b"]["blocked_by"] == "a"


def test_prepare_brief_and_milestone_survive_replay_and_render_without_raw_logs(tmp_path, qapp):
    events = stream()
    brief = {"ready": True, "task": {"key": "QA-1", "summary": "Fix <login>", "status": "To Do"},
             "workspace": {"path": "/sandbox/QA-1", "source": "/source", "operation": "recreated"},
             "git": {"branch": "QA-1-login", "head": "abc123", "base_ref": "origin/main", "base_commit": "def456"},
             "external_effects": [], "effects_verification": "declared"}
    events[-1].update(outcome="task_prepared", action_id="prepare_task", task_brief=brief)
    state = RunState()
    assert cyclereplay.restore(str(tmp_path), state, events=events)
    assert state.cycle["task_brief"] == brief
    record = state.cycle
    assert cyclesubjects.status_word(record).startswith("Task prepared · Prepare only")
    assert cyclesubjects.search_state(record)[1] == "neutral"
    text = cyclesubjects.details_html(record)
    for evidence in ("Task Brief", "QA-1-login", "abc123", "def456", "/sandbox/QA-1", "recreated", "None declared"):
        assert evidence in text
    assert "Fix &lt;login&gt;" in text
