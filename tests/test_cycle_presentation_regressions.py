"""Execution evidence stays available and honest across UI-facing projections."""

import json

import pytest

from cycle import bus, checkpoints, executor, model, outcomes, registry, sessions
from cycle import run as records
from domain.cycle import CycleRun, StepRun


class BriefPlugin(registry.CyclePlugin):
    metadata = registry.PluginMetadata(id="task.brief", name="Brief")

    def execute(self, context, step):
        return registry.succeeded(brief={"ready": True, "task": {"key": "QA-1"},
                                         "workspace": {"path": "/sandbox/QA-1"}, "git": {"head": "prepared-sha"}})


class Plugins:
    def get(self, name):
        return BriefPlugin()


def test_full_run_prepare_pause_publishes_brief_in_every_projection(tmp_path):
    cycle = model.parse_cycle({"id": "demo", "phases": [
        {"id": "prepare", "policy": "stop_after", "outcome": "task_prepared", "deliverables": ["brief"]},
        {"id": "implement"}], "steps": [
            {"id": "brief", "plugin": "task.brief", "phase": "prepare"},
            {"id": "work", "plugin": "task.brief", "phase": "implement", "needs": ["brief"]}]}, "demo")
    events = []
    observer = bus.Tee([records.Persister(root=str(tmp_path)),
                        bus.EventObserver(sink=lambda kind, **fields: events.append(dict(kind=kind, **fields)))])
    run = executor.run_cycle(cycle, str(tmp_path / "r"), run_id="r", registry=Plugins(), observer=observer)
    expected = run.steps["brief"].outputs["brief"]
    assert run.paused and run.outcome == "task_prepared" and not run.action_id
    metadata = json.loads((tmp_path / "r/metadata.json").read_text())
    session = sessions.page(str(tmp_path), memory_path=str(tmp_path / "memory.json"))["sessions"][0]
    for projection in (metadata, records.summary(run), records.index(str(tmp_path))[0], session, events[-1]):
        assert projection["task_brief"] == expected
    assert records.load(str(tmp_path / "r")).task_brief == expected


def test_phase_deliverable_disambiguates_multiple_brief_producers():
    run = CycleRun("r", "demo", status="failed", phase_definition=[{"id": "prepare", "deliverables": ["result"]}], steps={
        "result": StepRun("result", plugin="task.brief", status="failed", phase="prepare", ended_at=10,
                          outputs={"brief": {"ready": False, "git": {"contains_base": False}}}),
        "other": StepRun("other", plugin="task.brief", status="success", phase="prepare", ended_at=20,
                         outputs={"brief": {"ready": True, "task": {"key": "UNRELATED-1"}}})})
    projection = outcomes.refresh(run)
    assert projection["task_brief"] == run.steps["result"].outputs["brief"]


def test_unselected_borrowed_brief_is_not_presented_as_current_preparation():
    run = CycleRun("r", "demo", status="success", scope="partial", selected_steps=["work"], steps={
        "brief": StepRun("brief", plugin="task.brief", status="success", source_run="earlier",
                         outputs={"brief": {"ready": True, "task": {"key": "OLD-1"}}}),
        "work": StepRun("work", status="success")})
    outcomes.refresh(run)
    assert run.task_brief == {}


@pytest.mark.parametrize("held,paused,status", [(False, False, "interrupted"), (True, False, "waiting"),
                                                (False, True, "paused"), (True, True, "paused")])
def test_waiting_sessions_distinguish_executor_loss_from_safe_pause(tmp_path, held, paused, status):
    directory = tmp_path / "r"
    run = CycleRun("r", "demo", status="waiting", paused=paused,
                   pause_reason="phase_boundary" if paused else "", workspace=str(directory),
                   steps={"ask": StepRun("ask", plugin="approval.gate", status="waiting")})
    records.save(run)
    records.remember(run, str(tmp_path))

    def read():
        return sessions.page(str(tmp_path), memory_path=str(tmp_path / "memory.json"))["sessions"][0]

    if held:
        with checkpoints.lock(str(directory)):
            session = read()
    else:
        session = read()
    assert session["status"] == status and session["running"] is held
    assert session["execution_status"] == status
    assert session["outcome"] == ("in_progress" if status == "waiting" else status)


@pytest.mark.parametrize("borrow", [False, True])
def test_successful_partial_resume_keeps_neutral_outcome_and_excludes_borrowed_work(tmp_path, borrow):
    class Work(registry.CyclePlugin):
        metadata = registry.PluginMetadata(id="test.projection", name="Synthetic work")
        fail = False

        def execute(self, context, step):
            return registry.failed("temporary failure") if self.fail and step.id == "one" else registry.succeeded()

    class WorkPlugins:
        def get(self, name):
            return work

    work, plugins = Work(), WorkPlugins()
    cycle = model.parse_cycle({"id": "demo", "steps": [
        {"id": "one", "plugin": "test.projection"}, {"id": "publish", "plugin": "test.projection"}]}, "demo")
    earlier = executor.run_cycle(cycle, str(tmp_path / "earlier"), registry=plugins) if borrow else None
    work.fail = True
    first = executor.run_cycle(cycle, str(tmp_path / "r"), run_id="r", only=["one"], reuse=earlier, registry=plugins)
    assert first.status == "failed"
    work.fail = False
    events = []
    observer = bus.Tee([records.Persister(root=str(tmp_path)),
                        bus.EventObserver(sink=lambda kind, **fields: events.append(dict(kind=kind, **fields)))])
    resumed = executor.run_cycle(cycle, str(tmp_path / "r"), resume=True, registry=plugins, observer=observer)
    assert resumed.status == "success" and resumed.scope == "resume"
    assert resumed.selected_steps == ["one"]
    assert resumed.steps["publish"].status == ("success" if borrow else "skipped")
    assert bool(resumed.steps["publish"].source_run) is borrow
    for projection in (records.summary(resumed), records.index(str(tmp_path))[0],
                       json.loads((tmp_path / "r/metadata.json").read_text()), events[-1]):
        assert projection["outcome"] == "partial_completed"
        assert projection["coverage"] == {"completed": 1, "selected": 1, "total": 2}


@pytest.mark.parametrize("outside", [
    {"status": "skipped", "message": "not part of this run"},
    {"status": "success", "source_run": "earlier"},
    {"status": "success", "message": "reused from earlier (success)"},
])
def test_legacy_resumed_partial_infers_original_selection_from_evidence(outside):
    run = records.from_document({"id": "r", "cycle_id": "demo", "status": "success", "resume_count": 1,
                                 "steps": {"one": {"step_id": "one", "status": "success"},
                                           "outside": {"step_id": "outside", **outside}}})
    assert run.scope == "resume" and run.selected_steps is None
    assert run.outcome == "partial_completed"
    assert run.coverage == {"completed": 1, "selected": 1, "total": 2}


def test_full_resume_with_optional_branch_still_completes_workflow():
    run = CycleRun("r", "demo", status="success", scope="resume", resume_count=1, selected_steps=["one", "optional"],
                   steps={"one": StepRun("one", status="success"),
                          "optional": StepRun("optional", status="skipped", reason_code="condition_false")})
    assert outcomes.refresh(run)["outcome"] == "workflow_completed"
    assert run.coverage == {"completed": 1, "selected": 2, "total": 2}


def test_resumed_prepare_keeps_task_prepared_milestone_before_partial_completion():
    run = CycleRun("r", "demo", status="success", scope="resume", resume_count=1, selected_steps=["brief"],
                   action_id="prepare_task", action_definition={"result": "brief"}, steps={
                       "brief": StepRun("brief", plugin="task.brief", status="success", outputs={"brief": {"ready": True}}),
                       "publish": StepRun("publish", status="skipped", reason_code="not_selected")})
    assert outcomes.refresh(run)["outcome"] == "task_prepared"
    assert run.coverage == {"completed": 1, "selected": 1, "total": 2}
