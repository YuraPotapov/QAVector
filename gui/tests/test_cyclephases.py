import json

import pytest

from cms_gui import cycleactivity, cyclereplay
from cms_gui.cyclecockpit import ActiveStrip, render
from cms_gui.cycledraft import Draft
from cms_gui.pages.cycledialogs import NodeDialog
from cms_gui.pages.cycles import CyclesPage
from cms_gui.runner import RunState
from cms_gui.settings import Settings
from test_cycles_page import FakeCore, FakeInventory, SHELL_PLUGIN, graph, payload, row


DEFINITIONS = [{"id": "prepare", "title": "Prepare task", "policy": "stop_after"},
               {"id": "build", "title": "Build", "policy": "manual"}]


def started():
    state = RunState()
    made = graph(("a", []), ("b", ["a"]))
    made["nodes"][0]["phase"], made["nodes"][1]["phase"] = "prepare", "build"
    made["phases"] = DEFINITIONS
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo", "name": "Demo", "graph": made,
                  "control_version": 1, "phase_definition": DEFINITIONS})
    state.handle({"kind": "cycle.run.state", "run_id": "r", "action": "start", "status": "running",
                  "phase_actions": {"pause": True}})
    return state


def pause(state, manual=False):
    state.handle({"kind": "cycle.run.state", "run_id": "r", "action": "paused", "status": "waiting",
                  "paused": True, "pause_requested": True, "execution_status": "paused", "outcome": "task_prepared",
                  "pause_reason": "phase_boundary", "next_action": "Run phase: Build" if manual else "Continue run",
                  "last_completed_phase": "prepare", "current_phase": "prepare",
                  "phase_actions": {"continue": not manual, "run": "build", "retry": ""}})


def test_paused_status_and_timers_are_distinct_from_plugin_waiting():
    state = started()
    state.handle({"kind": "cycle.step.start", "step": "a", "active_elapsed_ms": 1000})
    state.handle({"kind": "cycle.step.waiting", "step": "a", "active_elapsed_ms": 2000})
    assert "Waiting" in cycleactivity.header(state.cycle, True)
    pause(state)
    assert "Paused after Prepare task" in cycleactivity.header(state.cycle, True)
    assert cycleactivity.elapsed(state.cycle["steps"]["a"], True) == 2000
    assert state.cycle_running
    assert "No parallel steps" in render(state.cycle, True)
    assert "PHASES" in render(state.cycle, True)


def test_live_continue_does_not_turn_into_resume_or_a_new_run():
    state = started()
    state.handle({"kind": "cycle.step.end", "step": "a", "status": "success"})
    pause(state)
    state.handle({"kind": "cycle.run.state", "run_id": "r", "action": "continue", "status": "running",
                  "paused": False, "pause_requested": False, "pause_reason": "", "execution_status": "running"})
    assert state.cycle["steps"]["a"]["status"] == "success"
    assert state.cycle["steps"]["b"]["status"] == "pending"
    assert state.cycle["run_id"] == "r" and state.cycle["mode"] == {}


@pytest.mark.parametrize("reason", ["user_request", "phase_failed"])
def test_pause_inside_a_phase_does_not_claim_that_phase_has_finished(reason):
    state = started()
    pause(state)
    state.cycle.update(pause_reason=reason, current_phase="build")
    assert "Paused in Build" in cycleactivity.header(state.cycle, True)


def test_continue_excludes_pause_from_live_elapsed_time(monkeypatch):
    clock = {"monotonic": 10, "wall": 1000}
    monkeypatch.setattr("cms_gui.runner.time.monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr("cms_gui.runner.time.time", lambda: clock["wall"])
    state = started()
    state.handle({"kind": "cycle.step.waiting", "step": "a", "active_elapsed_ms": 2000})
    pause(state)
    clock.update(monotonic=13, wall=1003)
    state.handle({"kind": "cycle.run.state", "run_id": "r", "status": "running",
                  "paused": False, "pause_requested": False})
    assert cycleactivity.elapsed(state.cycle["steps"]["a"], True) == 2000
    clock.update(monotonic=14, wall=1004)
    assert cycleactivity.elapsed(state.cycle["steps"]["a"], True) == 3000


def test_phase_retry_keeps_previous_artifacts_out_of_the_new_execution():
    state = started()
    state.handle({"kind": "cycle.step.start", "step": "a", "execution_generation": 1})
    state.handle({"kind": "cycle.artifact", "step": "a", "name": "Old report", "path": "old.json"})
    state.handle({"kind": "cycle.step.log", "step": "a", "lines": ["First execution"]})
    state.handle({"kind": "cycle.step.stage", "step": "a", "title": "First review"})
    state.handle({"kind": "cycle.step.end", "step": "a", "status": "failed", "execution_generation": 1})
    assert state.cycle["steps"]["a"]["artifacts"]
    state.handle({"kind": "cycle.step.end", "step": "a", "status": "pending", "execution_generation": 2,
                  "execution_segments": [{"generation": 1, "elapsed_ms": 2000}]})
    step = state.cycle["steps"]["a"]
    assert step["artifacts"] == step["log"] == step["stages"] == []
    assert step["execution_segments"] == [{"generation": 1, "elapsed_ms": 2000}]
    assert state.cycle["log"] and state.cycle["stages"]
    state.handle({"kind": "cycle.step.start", "step": "a", "execution_generation": 2})
    state.handle({"kind": "cycle.artifact", "step": "a", "name": "New report", "path": "new.json"})
    state.handle({"kind": "cycle.step.end", "step": "a", "status": "success", "execution_generation": 2})
    assert [artifact["path"] for artifact in step["artifacts"]] == ["new.json"]


def test_primary_run_control_uses_core_offers_without_extra_buttons(qapp, dispose):
    state, page, settings = started(), CyclesPage(Settings()), Settings()
    page.set_core(FakeCore({"demo": payload(cycle=state.cycle["graph"])}))
    page.set_inventory(FakeInventory([row()], [SHELL_PLUGIN]))
    page.open("demo")
    page.set_run_state(state)
    page._graph_known(state.cycle["graph"])
    sent = []
    page.control_requested.connect(lambda *values: sent.append(values))
    page._refresh_cockpit()
    launches = []
    page.run_requested.connect(lambda *args: launches.append(args))
    assert page.run_button.text() == "Pause" and page.run_button.isEnabled()
    page.run_button.click()
    assert sent[-1] == ("pause", "")
    state.handle({"kind": "cycle.run.state", "run_id": "r", "status": "running", "action": "pausing",
                  "pause_requested": True, "phase_actions": {"pause": False}})
    assert page.run_button.text() == "Pausing…" and not page.run_button.isEnabled()
    pause(state)
    page._refresh_cockpit()
    assert page.run_button.text() == "Continue" and page.run_button.isEnabled()
    page.run_button.click()
    assert sent[-1] == ("continue", "")
    assert page.phase_action.isVisible()
    page.phase_action.trigger()
    assert sent[-1] == ("run_phase", "build")
    pause(state, manual=True)
    page._refresh_cockpit()
    assert page.run_button.text() == "Run phase"
    page.run_button.click()
    assert sent[-1] == ("run_phase", "build")
    state.cycle["phase_actions"] = {"retry": "build"}
    page._refresh_cockpit()
    assert page.run_button.text() == "Retry phase"
    page.run_button.click()
    assert sent[-1] == ("retry_phase", "build")
    assert not launches
    page._update_buttons()
    assert page.run_button.isEnabled()
    state.cycle_running = False
    page._update_buttons()
    assert page.run_button.text() == "Resume"
    dispose(page)


def test_global_strip_navigates_to_the_run_without_duplicating_controls(qapp, dispose):
    state = started()
    strip = ActiveStrip(state, Settings())
    from PySide6.QtWidgets import QPushButton
    opened = []
    strip.opened.connect(lambda: opened.append(True))
    assert not strip.findChildren(QPushButton)
    strip.click()
    pause(state, manual=True)
    assert "Paused" in strip.summary.text()
    strip.click()
    state.cycle["phase_actions"] = {"retry": "build"}
    strip.refresh()
    strip.click()
    assert len(opened) == 3
    dispose(strip)


def test_replay_keeps_safe_pause_pending_nodes_and_phase_receipt(tmp_path):
    state = started()
    record = {"id": "r", "status": "waiting", "paused": True, "execution_status": "paused", "pause_reason": "phase_boundary",
              "phase_definition": DEFINITIONS, "last_completed_phase": "prepare", "phase_receipts": {},
              "steps": {"a": {"status": "success", "active_elapsed_ms": 7000}, "b": {"status": "pending"}}}
    (tmp_path / "metadata.json").write_text(json.dumps(record))
    directory = tmp_path / ".execution" / "phases"
    directory.mkdir(parents=True)
    (directory / "prepare.json").write_text(json.dumps({"phase": "prepare", "status": "success", "outcome": "task_prepared"}))
    assert cyclereplay.restore(str(tmp_path), state, events=[{"kind": "cycle.run.start", "cycle": "demo", "run_id": "r", "graph": state.cycle["graph"]}])
    assert not state.cycle_running and state.cycle["paused"]
    assert state.cycle["steps"]["b"]["status"] == "pending"
    assert state.cycle["phase_receipts"]["prepare"]["outcome"] == "task_prepared"
    assert "Paused" in cycleactivity.header(state.cycle)


def phase_replay(tmp_path, saved_generation=1, receipt_generation=1, changed=None, ended=False):
    evidence = {"status": "success", "execution_generation": 1,
                "definition_digest": "definition", "input_digest": "input"}
    receipt = {"phase": "prepare", "status": "success", "generation": receipt_generation,
               "evidence": {"a": dict(evidence)}}
    record = {"id": "r", "status": "running", "phase_definition": DEFINITIONS,
              "phase_receipts": {"prepare": dict(receipt, generation=saved_generation, invalidated=True)},
              "steps": {"a": dict(evidence)}}
    if ended:
        record.update(status="waiting", paused=True, pause_reason="phase_boundary", last_completed_phase="prepare")
    (tmp_path / "metadata.json").write_text(json.dumps(record))
    directory = tmp_path / ".execution"
    (directory / "phases").mkdir(parents=True)
    (directory / "phases" / "prepare.json").write_text(json.dumps(receipt))
    if changed:
        (directory / "steps").mkdir()
        (directory / "steps" / "a.json").write_text(json.dumps(dict(evidence, step_id="a", **changed)))
    state = started()
    events = [{"kind": "cycle.run.start", "run_id": "r", "cycle": "demo", "graph": state.cycle["graph"]}]
    if ended:
        events.append({"kind": "cycle.run.end", "run_id": "r", "status": "waiting", "paused": True,
                       "phase_receipts": {"prepare": dict(receipt, generation=1, invalidated=False)}})
    assert cyclereplay.restore(str(tmp_path), state, events=events)
    assert not state.cycle_running
    return state.cycle


@pytest.mark.parametrize("saved,receipt,expected,invalidated", [(1, 1, 1, True), (1, 2, 2, False), (2, 1, 2, True)])
def test_replay_phase_overlay_keeps_invalidation_and_newest_generation(tmp_path, saved, receipt, expected, invalidated):
    cycle = phase_replay(tmp_path, saved, receipt)
    receipt = cycle["phase_receipts"]["prepare"]
    assert receipt["generation"] == expected
    assert bool(receipt.get("invalidated")) is invalidated
    assert ("Needs rerun" in render(cycle)) is invalidated


@pytest.mark.parametrize("changed", [
    {"status": "pending"}, {"execution_generation": 2},
    {"definition_digest": "new definition"}, {"input_digest": "new input"}])
def test_replay_invalidates_phase_after_a_newer_step_receipt(tmp_path, changed):
    cycle = phase_replay(tmp_path, saved_generation=0, changed=changed)
    assert cycle["phase_receipts"]["prepare"]["invalidated"]
    assert "Needs rerun" in render(cycle)


@pytest.mark.parametrize("receipt_generation,invalidated", [(1, True), (2, False)])
def test_ended_headless_pause_restores_newer_phase_metadata(tmp_path, receipt_generation, invalidated):
    cycle = phase_replay(tmp_path, receipt_generation=receipt_generation, ended=True)
    receipt = cycle["phase_receipts"]["prepare"]
    assert receipt["generation"] == receipt_generation
    assert bool(receipt.get("invalidated")) is invalidated
    assert cycle["phase_definition"] == DEFINITIONS
    assert cycle["paused"] and cycle["status"] == "waiting"
    assert cycle["execution_status"] == "paused"
    assert ("Needs rerun" in render(cycle)) is invalidated


def test_new_nodes_inherit_execution_phase_and_node_editor_can_change_it(qapp, dispose):
    draft = Draft("demo", {"phases": DEFINITIONS, "steps": [{"id": "a", "plugin": "command.shell", "phase": "prepare"}]})
    name = draft.add_step("command.shell", after="a")
    assert draft.step(name)["phase"] == "prepare"
    dialog = NodeDialog(dict(draft.step(name), **{"with": {"command": "true"}}), SHELL_PLUGIN, phases=DEFINITIONS)
    combo = dialog._widgets["phase"]
    combo.setCurrentIndex(combo.findData("build"))
    dialog.save_button.click()
    assert dialog.saved["phase"] == "build"
    dispose(dialog)
