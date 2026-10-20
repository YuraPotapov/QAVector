import json

import pytest
from PySide6.QtCore import QPoint

from cms_gui import cycleactivity as activity, cyclereplay
from cms_gui.cyclecockpit import ActiveStrip, Cockpit, render
from cms_gui.runner import RunState
from cms_gui.settings import Settings


def state_of():
    state = RunState()
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo", "name": "Demo", "timing_version": 1,
                  "graph": {"nodes": [
                      {"id": "a", "label": "Baseline tests", "plugin": "command.shell", "needs": []},
                      {"id": "b", "label": "Choose task", "plugin": "user.choice", "needs": []},
                      {"id": "c", "label": "Review", "plugin": "approval.gate", "needs": ["a", "b"]}]}})
    for name, started in (("a", 1000), ("b", 1001)):
        state.handle({"kind": "cycle.step.start", "step": name, "started_at": started, "active_elapsed_ms": 0})
    return state


@pytest.mark.parametrize("ms,expected", [(0, "00:00"), (59999, "00:59"), (3599999, "59:59"),
                                          (3600000, "1:00:00"), (86399999, "23:59:59"),
                                          (86400000, "1d 00:00"), (188760000, "2d 04:26"),
                                          (None, "time unavailable")])
def test_adaptive_duration(ms, expected):
    assert activity.duration(ms) == expected


def test_primary_stable_on_progress_user_selection_and_decision():
    state = state_of()
    assert activity.primary(state.cycle) == "a"
    state.handle({"kind": "cycle.step.progress", "step": "b", "title": "Inspecting"})
    assert activity.primary(state.cycle) == "a"
    state.cycle["_activity_selected"] = "b"
    assert activity.primary(state.cycle) == "b"
    state.handle({"kind": "cycle.step.progress", "step": "a", "state": "waiting_for_approval"})
    assert activity.primary(state.cycle) == "a"
    assert "Waiting for approval · +1 parallel" in activity.header(state.cycle, True)


def test_a_late_timing_snapshot_does_not_hide_a_newer_pending_decision():
    state = state_of()
    state.handle({"kind": "cycle.step.progress", "step": "b", "state": "waiting_for_input", "last_activity_at": 2000})
    state.handle({"kind": "cycle.run.timing", "run_id": "r", "steps": {
        "b": {"active_elapsed_ms": 4000, "last_activity_at": 1999, "progress": {"state": "running"}}}})
    assert activity.primary(state.cycle) == "b"
    assert state.cycle["steps"]["b"]["active_elapsed_ms"] == 4000
    assert state.cycle["steps"]["b"]["last_activity_at"] == 2000


def test_timer_only_interpolates_live_bounded_confirmations():
    step = {"active_elapsed_ms": 2000, "status": "running", "_timing_received": 10, "_timing_wall": 1000}
    assert activity.elapsed(step, True, now=12, wall=1002) == 4000
    assert activity.elapsed(step, False, now=12, wall=1002) == 2000
    assert activity.elapsed(step, True, now=70, wall=1060) == 2000
    assert activity.elapsed(step, True, now=12, wall=4602) == 2000
    step["status"] = "cancelled"
    assert activity.elapsed(step, True, now=12, wall=1002) == 2000


def test_cockpit_explains_parallel_work_progress_next_and_unknown_effects(qapp, dispose):
    state = state_of()
    state.handle({"kind": "cycle.step.progress", "step": "a", "current": 12, "total": 74,
                  "unit": "modules", "title": "Testing <claim>"})
    view = Cockpit()
    view.refresh(state.cycle, True)
    text = view.browser.toPlainText()
    assert view.current.title.text() == "Baseline tests"
    assert view.current.state.text() == "Running"
    assert "12 / 74 modules" in view.current.detail.text()
    assert view.current.progress.value() == round(12 / 74 * 1000)
    assert "<claim>" in view.current.detail.text()
    for expected in ("ALSO RUNNING", "Choose task",
                     "NEXT", "Review", "requires your decision", "Not recorded"):
        assert expected in text
    assert "Testing &lt;claim&gt;" in render(state.cycle, True)
    dispose(view)


def test_current_node_stays_visible_above_history_and_follows_the_actual_primary(qapp, dispose):
    state = state_of()
    view = Cockpit()
    view.resize(800, 320)
    view.refresh(state.cycle, True)
    view.show()
    qapp.processEvents()
    assert view.current.isVisible() and view.current.title.text() == "Baseline tests"
    top = view.current.geometry().top()
    scroll = view.browser.verticalScrollBar()
    assert scroll.maximum() > 0
    scroll.setValue(scroll.maximum())
    assert view.current.geometry().top() == top
    assert view.current.geometry().bottom() < view.browser.geometry().top()
    state.handle({"kind": "cycle.step.progress", "step": "b", "title": "Review task facts"})
    view.refresh(state.cycle, True)
    assert view.current.step_id == "a", "parallel activity must not make the current node oscillate"
    state.handle({"kind": "cycle.step.end", "step": "a", "status": "success"})
    view.refresh(state.cycle, True)
    assert view.current.step_id == "b" and view.current.title.text() == "Choose task"
    assert view.current.progress.isHidden(), "unknown progress must not imply a percentage"
    assert view.current.isVisible() and view.current.geometry().top() == top
    opened = []
    view.node_selected.connect(opened.append)
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    QTest.mouseClick(view.current, Qt.LeftButton)
    assert opened == ["b"]
    state.handle({"kind": "cycle.run.end", "status": "cancelled"})
    view.refresh(state.cycle, False)
    assert view.current.state.text() == "Stopped" and view.current.step_id == ""
    dispose(view)


@pytest.mark.parametrize("action", ["cancel", "stop"])
def test_control_changes_keep_the_visible_text_and_offset_after_layout_settles(qapp, dispose, action):
    state = RunState()
    nodes = [{"id": "done%s" % i, "label": "Completed step %s" % i, "needs": []} for i in range(40)]
    nodes.append({"id": "publish", "label": "Publish branch", "plugin": "command.shell", "needs": []})
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo", "execution_mode": "approve",
                  "graph": {"nodes": nodes}})
    for node in nodes[:-1]:
        state.handle({"kind": "cycle.step.end", "step": node["id"], "status": "success",
                      "active_elapsed_ms": 1000, "message": "Result for " + node["id"]})
    receipt = {"id": "request", "fingerprint": "proof", "step": "publish", "status": "pending",
               "label": "Publish branch", "purpose": "Publish the reviewed work",
               "resolved_inputs": {"branch": "work", "remote": "origin"}}
    state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": receipt})
    view = Cockpit()
    view.resize(800, 500)
    view.refresh(state.cycle, True)
    view.show()
    view.activateWindow()
    view.browser.setFocus()
    qapp.processEvents()
    scroll = view.browser.verticalScrollBar()
    assert scroll.maximum() > 1200
    scroll.setValue(1200)

    def reading():
        block = view.browser.cursorForPosition(QPoint(5, 5)).block()
        rect = view.browser.document().documentLayout().blockBoundingRect(block)
        return block.text(), rect.top() - scroll.value()

    before = reading()
    if action == "cancel":
        actions = view.approval_actions[receipt["id"]]
        actions.cancel_button.setFocus()
        actions.cancel_button.click()
    else:
        state.cycle["_stopping"] = True
    state.handle({"kind": "cycle.node.approval", "run_id": "r",
                  "receipt": dict(receipt, status="declined" if action == "cancel" else "invalidated")})
    view.refresh(state.cycle, True)
    qapp.processEvents()
    assert reading() == before
    if action == "cancel":
        state.handle({"kind": "cycle.run.state", "run_id": "r", "status": "waiting", "paused": True,
                      "pause_reason": "approval_declined", "execution_status": "paused"})
        assert activity.active(state.cycle) == [] and activity.primary(state.cycle) == ""
        assert "node approval declined" in activity.header(state.cycle, True)
    else:
        state.handle({"kind": "cycle.step.end", "step": "publish", "status": "cancelled", "message": "Stopped by user"})
        state.handle({"kind": "cycle.run.end", "run_id": "r", "status": "cancelled"})
    for _ in range(3):
        view.refresh(state.cycle, action == "cancel")
        qapp.processEvents()
        assert reading() == before
    scroll.setValue(scroll.value() + 70)
    moved = reading()
    view.refresh(state.cycle, action == "cancel")
    qapp.processEvents()
    assert reading() == moved, "the next refresh must respect manual scrolling"
    view.refresh(dict(state.cycle, run_id="another-run"), False)
    assert scroll.value() == 0
    dispose(view)


@pytest.mark.parametrize("unstarted_status", ["skipped", "cancelled"])
def test_stop_keeps_cockpit_stable_while_the_model_settles_unstarted_nodes(qapp, dispose, unstarted_status):
    state = RunState()
    remaining = ["remaining_%s" % i for i in range(20)]
    nodes = [{"id": "build", "label": "Build task", "needs": []},
             {"id": "review", "label": "Review request", "needs": []}]
    for index, name in enumerate(remaining):
        nodes.append({"id": name, "label": "Unstarted node %s" % index,
                      "needs": [remaining[index - 1] if index else "build"]})
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo", "graph": {"nodes": nodes}})
    state.handle({"kind": "cycle.step.start", "step": "build", "active_elapsed_ms": 1000})
    state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": {
        "id": "request", "fingerprint": "proof", "step": "review", "label": "Review request", "status": "pending"}})
    view = Cockpit()
    view.refresh(state.cycle, True)
    assert view.current.step_id == "review" and view.approval_actions
    state.cycle["_stopping"] = True
    view.refresh(state.cycle, True)
    assert view.current.step_id == "build" and view.current.state.text() == "Stopping"
    assert not view.approval_actions
    assert "Unstarted node" not in view.browser.toPlainText()
    assert activity.next_nodes(state.cycle) == []
    state.handle({"kind": "cycle.step.end", "step": "build", "status": "cancelled", "message": "Stopped by user"})
    view.refresh(state.cycle, True)
    assert view.current.title.text() == "Stopping cycle…" and view.current.state.text() == "Stopping"
    before = view.browser.toPlainText()
    for name in ["review"] + remaining:
        if unstarted_status == "skipped":
            state.handle({"kind": "cycle.step.skipped", "step": name, "reason": "the run was stopped"})
            view.refresh(state.cycle, True)
            assert view.browser.toPlainText() == before
        state.handle({"kind": "cycle.step.end", "step": name, "status": unstarted_status,
                      "message": "the run was stopped"})
        view.refresh(state.cycle, True)
        assert view.browser.toPlainText() == before
        assert view.current.title.text() == "Stopping cycle…" and view.current.step_id == ""
    assert all(state.cycle["steps"][name]["status"] == unstarted_status for name in ["review"] + remaining)
    state.handle({"kind": "cycle.run.end", "run_id": "r", "status": "cancelled"})
    view.refresh(state.cycle, False)
    assert view.current.state.text() == "Stopped" and view.current.title.text() == "Run stopped"
    assert "Unstarted node" not in view.browser.toPlainText() and not view.approval_actions
    dispose(view)


def test_explicit_gate_decline_keeps_recorded_failure_without_previewing_31_cancelled_nodes(qapp, dispose):
    """Ready_To_Release's recorded path has no toolbar _stopping flag."""
    state = RunState()
    gate = "ask_what_to_merge"
    remaining = ["remaining_%s" % i for i in range(31)]
    nodes = [{"id": gate, "label": "Review release", "plugin": "approval.gate", "needs": [], "on_failure": "stop"}]
    for index, name in enumerate(remaining):
        nodes.append({"id": name, "label": "Unstarted node %s" % index,
                      "needs": [remaining[index - 1] if index else gate], "on_failure": "continue" if index % 3 else "stop"})
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo", "graph": {"nodes": nodes}})
    state.handle({"kind": "cycle.step.start", "step": gate})
    view = Cockpit()
    view.refresh(state.cycle, True)
    state.handle({"kind": "cycle.step.end", "step": gate, "status": "failed", "message": "not approved: Stop",
                  "outputs": {"approved": False, "answer": "Stop"}, "attempts": 1})
    view.refresh(state.cycle, True)
    assert not state.cycle.get("_stopping") and state.cycle["status"] == "running"
    assert view.current.state.text() == "Stopping"
    assert activity.next_nodes(state.cycle) == []
    content = view.browser.toPlainText()
    assert "not approved: Stop" in content and "Unstarted node" not in content
    for name in remaining:
        state.handle({"kind": "cycle.step.end", "step": name, "status": "cancelled", "attempts": 0,
                      "started_at": None, "message": "ask_what_to_merge failed"})
        view.refresh(state.cycle, True)
        assert view.browser.toPlainText() == content
        assert view.current.state.text() == "Stopping"
    state.handle({"kind": "cycle.run.end", "run_id": "r", "status": "failed",
                  "message": "ask_what_to_merge failed: not approved: Stop"})
    view.refresh(state.cycle, False)
    assert state.cycle["status"] == "failed" and view.current.state.text() == "Failed"
    assert not activity.stopping(state.cycle)
    assert all(state.cycle["steps"][name]["status"] == "cancelled" for name in remaining)
    dispose(view)


@pytest.mark.parametrize("policy,phases,action_id,stopping", [
    ("continue", False, "", False), ("stop", True, "", False), ("stop", True, "prepare_task", True)])
def test_shutdown_preview_respects_continue_and_phase_recovery(qapp, dispose, policy, phases, action_id, stopping):
    state = RunState()
    made = {"nodes": [
        {"id": "gate", "label": "Review release", "plugin": "approval.gate", "needs": [], "on_failure": policy},
        {"id": "recover", "label": "Handle declined review", "needs": ["gate"], "condition": "not approved"}]}
    if phases:
        made["phases"] = [{"id": "review", "title": "Review", "policy": "automatic"}]
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo", "graph": made, "action_id": action_id})
    state.handle({"kind": "cycle.step.end", "step": "gate", "status": "failed", "message": "not approved: Stop"})
    view = Cockpit()
    view.refresh(state.cycle, True)
    assert activity.stopping(state.cycle) is stopping
    assert ("Handle declined review" in view.browser.toPlainText()) is not stopping
    if phases and not action_id:
        state.handle({"kind": "cycle.run.state", "run_id": "r", "status": "waiting", "paused": True,
                      "pause_reason": "phase_failed", "phase_actions": {"retry": "review"}})
        view.refresh(state.cycle, True)
        assert not activity.stopping(state.cycle) and view.current.state.text() == "Paused"
        assert state.cycle["phase_actions"] == {"retry": "review"}
    dispose(view)


def test_skipped_node_is_hidden_even_before_its_approval_receipts_are_invalidated(qapp, dispose):
    state = state_of()
    for name in ("a", "b"):
        state.handle({"kind": "cycle.step.end", "step": name, "status": "success"})
    for status in ("pending", "approved"):
        state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": {
            "id": status, "fingerprint": "proof", "step": "c", "label": "Review", "status": status}})
    state.handle({"kind": "cycle.step.skipped", "step": "c", "reason": "condition was not true"})
    view = Cockpit()
    view.refresh(state.cycle, True)
    assert view.current.step_id == "" and "Review" not in view.current.title.text()
    assert "Review" not in view.browser.toPlainText() and not view.approval_actions
    dispose(view)


def test_causal_chain_and_failure_counts_are_visible():
    state = state_of()
    state.handle({"kind": "cycle.step.end", "step": "a", "status": "failed", "message": "Scenario failed at step 7"})
    state.handle({"kind": "cycle.step.end", "step": "c", "status": "skipped", "reason_code": "blocked",
                  "blocked_by": "a", "message": "a failed"})
    assert [name for name, _ in activity.causes(state.cycle, "c")] == ["c", "a"]
    markup = render(state.cycle, True)
    assert "1 root failure(s) · 1 downstream step(s) blocked" in markup
    assert "Scenario failed at step 7" in markup
    assert "Review" not in markup, "the skipped dependent is counted without displaying its node"


def test_global_strip_remains_visible_off_cycles_and_hides_on_end(qapp, dispose):
    state = state_of()
    strip = ActiveStrip(state, Settings())
    strip.show()
    assert strip.isVisible()
    assert "Baseline tests" in strip.summary.text()
    opened = []
    strip.opened.connect(lambda: opened.append(True))
    strip.click()
    assert opened
    state.handle({"kind": "cycle.run.end", "status": "success"})
    assert not strip.isVisible()
    dispose(strip)


def test_replay_prefers_receipts_and_never_counts_offline_time(tmp_path):
    state = state_of()
    events = [{"kind": "cycle.run.start", "run_id": "r", "cycle": "demo", "graph": state.cycle["graph"]},
              {"kind": "cycle.step.start", "step": "a", "active_elapsed_ms": 1000}]
    (tmp_path / "metadata.json").write_text(json.dumps({"id": "r", "active_elapsed_ms": 9000,
        "steps": {"a": {"active_elapsed_ms": 8000, "execution_generation": 2}}}))
    assert cyclereplay.restore(str(tmp_path), state, events=events)
    assert not state.cycle_running
    assert activity.elapsed(state.cycle["steps"]["a"], False) == 8000
    assert state.cycle["steps"]["a"]["execution_generation"] == 2
    assert "Running" not in activity.header(state.cycle)


def test_bounded_replay_keeps_early_results_and_prefers_individual_step_receipts(tmp_path):
    state = state_of()
    events = [{"kind": "cycle.run.start", "run_id": "r", "cycle": "demo", "graph": state.cycle["graph"]}]
    record = {"id": "r", "status": "success", "scope": "full", "outcome": "workflow_completed",
              "coverage": {"completed": 3, "selected": 3, "total": 3}, "steps": {
        name: {"status": "success", "message": "Saved result", "active_elapsed_ms": 1000} for name in ("a", "b", "c")}}
    (tmp_path / "metadata.json").write_text(json.dumps(record))
    receipts = tmp_path / ".execution" / "steps"
    receipts.mkdir(parents=True)
    (receipts / "a.json").write_text(json.dumps({"step_id": "a", "status": "success", "active_elapsed_ms": 7000}))
    assert cyclereplay.restore(str(tmp_path), state, events=events)
    assert not state.cycle_running
    assert state.cycle["status"] == "success"
    assert state.cycle["passed"] == 3
    assert state.cycle["steps"]["a"]["active_elapsed_ms"] == 7000
    assert "COMPLETED · 3" in render(state.cycle)
    assert "Workflow completed" in activity.header(state.cycle)


def test_silence_is_diagnostic_and_does_not_change_status():
    step = {"status": "running", "last_activity_at": 1000, "timeout_remaining_ms": 17000}
    assert activity.activity_text(step, True, threshold=60, now=1134) == "No output for 02:14 · timeout in 00:17"
    assert step["status"] == "running"


def test_label_falls_back_to_plugin_display_name_then_id():
    cycle = {"graph": {"nodes": [{"id": "a", "label": "a", "plugin": "agent.implement"}]}}
    assert activity.label(cycle, "a", {"agent.implement": "Implement change"}) == "Implement change"
    assert activity.label(cycle, "a") == "a"
