"""Anonymized exports preserve semantic evidence without carrying UI payloads."""

import json
from pathlib import Path

import pytest

from cycle import baseline, outcomes, run as records
from domain.cycle import CycleRun, StepRun


def private_run(tmp_path, kind):
    brief = {"ready": kind != "blocked_prepare", "task": {"key": "PRIVATE-12", "summary": "private-task"},
             "workspace": {"path": "/private-path/repository", "source": "/private-path/source"},
             "git": {"head": "private-sha"}, "task_report": "private-report.html"}
    run = CycleRun("private-run-id", "demo", cycle_name="private-cycle-title", started_at=1792275000,
                   ended_at=1792275010, duration_ms=10000, status="success",
                   workspace=str(tmp_path / "runs/private-run-id"), variables={"token": "private-token"},
                   subject={"key": "PRIVATE-12"}, message="private-message", steps={
                       "brief": StepRun("brief", plugin="task.brief", status="success", phase="prepare",
                                        started_at=1792275001, ended_at=1792275002,
                                        outputs={"brief": brief, "token": "private-output"})})
    if kind in ("prepare", "blocked_prepare"):
        run.scope, run.selected_steps = "partial", ["brief"]
        run.action_id = "prepare_task"
        run.action_definition = {"result": "brief", "steps": ["brief"],
                                 "title": "private-title", "description": "private-description"}
        if kind == "blocked_prepare":
            run.status = run.steps["brief"].status = "failed"
    elif kind == "phase_pause":
        run.status, run.paused, run.ended_at = "waiting", True, None
        run.pause_reason, run.last_completed_phase = "phase_boundary", "prepare"
        run.phase_definition = [{"id": "prepare", "title": "private-phase-title", "purpose": "private-purpose",
                                 "policy": "stop_after", "effects": ["private-remote-write"],
                                 "outcome": "task_prepared", "deliverables": ["brief"]}]
        run.phase_receipts = {"prepare": {"status": "success", "outcome": "task_prepared",
            "ended_at": 1792275002, "deliverables": [{"path": "/private-path/artifact", "sha256": "private-hash"}],
            "evidence": {"private-operation-id": {"command": "private-command"}}}}
        run.next_action = "private-next-action"
        run.control_history = [{"action": "pause", "at": 1792275002, "reason": "private-reason"}]
    elif kind in ("approval", "declined_approval", "policy_history"):
        run.steps["work"] = StepRun("work", plugin="command.shell", status="pending", phase="publish")
        run.status, run.ended_at = "waiting", None
        run.initial_execution_mode, run.execution_mode = "auto", "approve"
        run.node_approvals = {"private-approval-id": {"id": "private-approval-id", "run_id": "private-run-id",
            "step": "work", "status": "pending", "fingerprint": "private-fingerprint", "created_at": 1792275003,
            "label": "private-label", "purpose": "private-action-purpose", "declared_effects": ["private-effect"],
            "resolved_inputs": {"command": "private-command /private-path/repository", "facts": "private-upstream-output"}}}
        if kind == "declined_approval":
            run.paused, run.pause_reason = True, "approval_declined"
            run.node_approvals["private-approval-id"]["status"] = "declined"
        if kind == "policy_history":
            run.policy_changes = [{"id": "private-change-id", "run_id": "private-run-id", "from": "auto",
                                   "to": "approve", "at": 1792275004, "reason": "private-policy-reason",
                                   "invalidated": ["private-approval-id"]}]
    elif kind == "resumed_partial":
        run.scope, run.selected_steps, run.resume_count = "resume", ["brief"], 2
        run.steps["other"] = StepRun("other", plugin="command.shell", status="skipped", reason_code="not_selected")
    elif kind in ("legacy_borrowed", "legacy_resumed_partial"):
        if kind == "legacy_resumed_partial":
            run.resume_count = 2
        run.steps["borrowed"] = StepRun("borrowed", plugin="command.shell", status="success",
                                         message="reused from private-earlier-run (success)")
    return run


@pytest.mark.parametrize("kind", ["prepare", "blocked_prepare", "phase_pause", "approval", "declined_approval",
                                  "policy_history", "resumed_partial", "legacy_borrowed", "legacy_resumed_partial"])
def test_modern_baseline_export_is_private_and_round_trips(tmp_path, kind):
    run = private_run(tmp_path, kind)
    records.save(run)
    original = {key: outcomes.refresh(run)[key] for key in outcomes.FIELDS}
    root = tmp_path / "runs"
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    report, fixtures = baseline.audit(str(root))
    payload = json.dumps([report, fixtures])
    assert "private-" not in payload and "PRIVATE-12" not in payload and "17922750" not in payload
    assert set(report["runs"][0]) == {"run", "cycle", "status", "reached", "duration_ms", *outcomes.FIELDS}
    assert {key: report["runs"][0][key] for key in outcomes.FIELDS} == original
    assert fixtures["schema"] == 2
    sample = json.loads(json.dumps(fixtures["runs"][0]))
    assert sample["metadata"]["schema"] == records.SCHEMA == 1
    replayed = outcomes.refresh(records.from_document(sample["metadata"]))
    assert replayed == sample["expected"]
    assert {key: replayed[key] for key in outcomes.FIELDS} == original
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_audit_cli_writes_versioned_round_trip_fixtures_outside_history(tmp_path):
    run = private_run(tmp_path, "prepare")
    records.save(run)
    output, fixture_path = tmp_path / "report.json", tmp_path / "fixtures.json"
    assert baseline.main(["--runs-dir", str(tmp_path / "runs"), "--output", str(output),
                          "--fixtures", str(fixture_path)]) == 0
    sample = json.loads(fixture_path.read_text())["runs"][0]
    assert outcomes.refresh(records.from_document(sample["metadata"])) == sample["expected"]
    assert sample["expected"]["outcome"] == "task_prepared"
    assert "private-" not in output.read_text() + fixture_path.read_text()


LEGACY = json.loads((Path(__file__).parent / "fixtures/cycles/reliability_baseline.json").read_text())["runs"]


@pytest.mark.parametrize("sample", LEGACY, ids=lambda one: one["metadata"]["id"])
def test_modern_export_also_preserves_legacy_fixture_semantics(tmp_path, sample):
    run = records.from_document(sample["metadata"])
    run.workspace = str(tmp_path / run.id)
    records.save(run)
    _, fixtures = baseline.audit(str(tmp_path))
    exported = fixtures["runs"][0]
    projection = outcomes.refresh(records.from_document(exported["metadata"]))
    assert projection == exported["expected"]
    assert {key: projection[key] for key in outcomes.FIELDS} == sample["expected"]


@pytest.mark.parametrize("started_at", [None, 0, 1792275000])
def test_anonymized_clocks_preserve_reached_phase_for_close_step_endings(tmp_path, started_at):
    run = CycleRun("r", "demo", status="success", started_at=started_at, workspace=str(tmp_path / "r"), steps={
        "first": StepRun("first", status="success", group="First", ended_at=1792275001),
        "last": StepRun("last", status="success", group="Last", ended_at=1792275001.0001)})
    records.save(run)
    _, fixtures = baseline.audit(str(tmp_path))
    sample = fixtures["runs"][0]
    assert sample["expected"]["current_phase"] == "Last"
    assert outcomes.refresh(records.from_document(sample["metadata"])) == sample["expected"]
