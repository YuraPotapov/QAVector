"""Run outcomes are evidence about scope, independent of the old exit code."""

import json
import threading
from pathlib import Path

import pytest

from cycle import baseline, bus, executor, model, outcomes, registry, sessions
from cycle import run as records
from domain.cycle import CycleRun, StepRun


class Plugin(registry.CyclePlugin):
    def __init__(self, action=None, reusable=True):
        self.metadata = registry.PluginMetadata(id="test.node", name="Node", reusable=reusable)
        self.action = action or (lambda context, step: registry.succeeded())
        self.calls = []

    def execute(self, context, step):
        self.calls.append(step.id)
        return self.action(context, step)


class Plugins:
    def __init__(self, **entries):
        self.entries = entries

    def get(self, name):
        return self.entries.get(name)


def node(name, **extra):
    return dict(id=name, plugin="test", **extra)


def workflow(*nodes):
    return model.parse_cycle({"id": "demo", "steps": list(nodes)}, "demo")


def test_one_of_68_is_partial_in_metadata_index_and_events(tmp_path):
    cycle = workflow(*(node("step%d" % i, group="Intake") for i in range(68)))
    events = []
    observer = bus.Tee([records.Persister(root=str(tmp_path)),
                        bus.EventObserver(sink=lambda kind, **fields: events.append(dict(kind=kind, **fields)))])
    run = executor.run_cycle(cycle, str(tmp_path / "r"), run_id="r", only=["step0"],
                             registry=Plugins(test=Plugin()), observer=observer)
    assert run.status == "success" and run.exit_code == 0
    expected = {"scope": "partial", "execution_status": "completed",
                "coverage": {"completed": 1, "selected": 1, "total": 68},
                "outcome": "partial_completed", "current_phase": "Intake"}
    metadata = json.loads((tmp_path / "r/metadata.json").read_text())
    for payload in (metadata, records.index(str(tmp_path))[0], events[-1]):
        assert {key: payload[key] for key in expected} == expected
        assert payload["status"] == "success"
    assert metadata["schema"] == 1
    assert metadata["steps"]["step1"]["reason_code"] == "not_selected"
    borrowed = [e for e in events if e.get("step") == "step1" and e["kind"] == "cycle.step.end"]
    assert borrowed[0]["reason_code"] == "not_selected"


def test_borrowed_results_do_not_inflate_partial_coverage(tmp_path):
    cycle, plugins = workflow(node("a"), node("b")), Plugins(test=Plugin())
    first = executor.run_cycle(cycle, str(tmp_path / "first"), registry=plugins)
    partial = executor.run_cycle(cycle, str(tmp_path / "partial"), registry=plugins,
                                 only=["b"], reuse=first)
    assert partial.coverage == {"completed": 1, "selected": 1, "total": 2}
    assert partial.outcome == "partial_completed"
    assert partial.steps["a"].source_run == first.id


@pytest.mark.parametrize("extra,code", [
    ({"if": "false"}, "condition_false"),
    ({"disabled": True}, "disabled"),
])
def test_skipped_steps_have_durable_and_streamed_reasons(tmp_path, extra, code):
    seen = []
    cycle = workflow(node("a"), node("b", needs=["a"], **extra))
    run = executor.run_cycle(cycle, str(tmp_path), registry=Plugins(test=Plugin()),
                             observer=bus.EventObserver(sink=lambda kind, **f: seen.append(dict(kind=kind, **f))))
    assert run.steps["b"].reason_code == code
    receipt = json.loads((tmp_path / ".execution/steps/b.json").read_text())
    assert receipt["reason_code"] == code
    skipped = [e for e in seen if e["kind"] == "cycle.step.skipped"]
    assert skipped[0]["reason_code"] == code
    assert run.outcome == "workflow_completed"


def test_unavailable_optional_output_does_not_look_like_a_false_condition(tmp_path):
    cycle = workflow(node("start"), node("a", needs=["start"], **{"if": "false"}),
                     node("b", needs=["a"], **{"if": "${steps.a.outputs.value}"}))
    run = executor.run_cycle(cycle, str(tmp_path), registry=Plugins(test=Plugin()))
    assert run.steps["a"].reason_code == "condition_false"
    assert run.steps["b"].reason_code == "unavailable"
    assert run.outcome == "workflow_incomplete"


def test_failed_dependency_is_different_from_false_condition(tmp_path):
    plugin = Plugin(lambda c, s: registry.failed("bad") if s.id == "a" else registry.succeeded())
    run = executor.run_cycle(workflow(node("a", on_failure="continue"), node("b", needs=["a"])),
                             str(tmp_path), registry=Plugins(test=plugin))
    assert run.steps["b"].reason_code == "blocked"
    assert run.steps["b"].blocked_by == "a"
    assert run.outcome == "failed"


@pytest.mark.parametrize("count,outcome", [(0, "no_work_found"), (1, "workflow_completed")])
def test_only_a_proven_empty_query_is_no_work(tmp_path, count, outcome):
    cycle = model.parse_cycle({"id": "demo", "steps": [{"id": "query", "plugin": "jira.issues"}]}, "demo")
    plugins = Plugins(**{"jira.issues": Plugin(lambda c, s: registry.succeeded(count=count))})
    run = executor.run_cycle(cycle, str(tmp_path), registry=plugins)
    assert not run.subject
    assert run.outcome == outcome


def test_empty_subject_cannot_hide_other_work():
    run = CycleRun("r", "demo", status="success", steps={
        "query": StepRun("query", plugin="jira.issues", status="success", outputs={"count": 0}),
        "publish": StepRun("publish", plugin="git.push", status="success")})
    assert outcomes.refresh(run)["outcome"] == "workflow_completed"


def test_legacy_borrow_message_is_evidence_of_a_partial_scope():
    run = records.from_document({"id": "r", "cycle_id": "demo", "status": "success", "steps": {
        "a": {"step_id": "a", "status": "success", "message": "reused from old (success)"},
        "b": {"step_id": "b", "status": "success"}}})
    assert run.scope == "partial"
    assert run.coverage == {"completed": 1, "selected": 1, "total": 2}


def test_rejected_approval_names_the_causal_gate():
    run = CycleRun("r", "demo", status="failed", steps={
        "gate": StepRun("gate", plugin="approval.gate", status="failed", outputs={"approved": False}),
        "work": StepRun("work", status="skipped", message="gate failed")})
    outcomes.refresh(run)
    assert run.steps["work"].reason_code == "rejected"
    assert run.steps["work"].blocked_by == "gate"


def test_unavailable_selected_work_is_incomplete_even_without_successes():
    run = CycleRun("r", "demo", status="success", steps={
        "work": StepRun("work", status="skipped", message="if: output unavailable")})
    assert outcomes.refresh(run)["outcome"] == "workflow_incomplete"


def test_legacy_sessions_are_enriched_without_migrating_directories(tmp_path):
    directory = tmp_path / "r"
    directory.mkdir()
    metadata = {"id": "r", "cycle_id": "demo", "status": "success", "steps": {
        "a": {"step_id": "a", "status": "success", "ended_at": 10},
        "b": {"step_id": "b", "status": "skipped", "message": "not part of this run"}}}
    records._atomic(str(directory / "metadata.json"), metadata)
    records._atomic(str(directory / "graph.json"), {"nodes": [{"id": "a", "group": "Prepare"}]})
    records._atomic(str(tmp_path / "index.json"), {"runs": [{"id": "r", "cycle": "demo",
        "status": "success", "subject": {}, "reached": "a", "started_at": 1}]})
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    session = sessions.page(str(tmp_path), memory_path=str(tmp_path / "memory.json"))["sessions"][0]
    assert session["outcome"] == "partial_completed"
    assert session["coverage"] == {"completed": 1, "selected": 1, "total": 2}
    assert session["current_phase"] == "Prepare"
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert set(tmp_path.rglob("*.json")) == set(before)


def test_interrupted_session_cannot_keep_an_in_progress_outcome(tmp_path):
    run = CycleRun("r", "demo", status="running", workspace=str(tmp_path / "r"))
    records.save(run)
    records.remember(run, str(tmp_path))
    session = sessions.page(str(tmp_path), memory_path=str(tmp_path / "memory.json"))["sessions"][0]
    assert session["execution_status"] == session["outcome"] == "interrupted"


FIXTURES = Path(__file__).parent / "fixtures/cycles/reliability_baseline.json"


@pytest.mark.parametrize("sample", json.loads(FIXTURES.read_text())["runs"],
                         ids=lambda s: s["metadata"]["id"])
def test_real_anonymized_runs_keep_their_expected_outcome(sample):
    run = records.from_document(sample["metadata"])
    assert outcomes.refresh(run) == sample["expected"]


def test_baseline_audit_does_not_export_private_data_or_write_history(tmp_path):
    root = tmp_path / "runs"
    run = CycleRun("private-run-id", "demo", status="success", workspace=str(root / "private-run-id"),
                   variables={"token": "private-token"}, subject={"key": "PRIVATE-12"},
                   steps={"a": StepRun("a", status="success", message="private-message",
                                       outputs={"secret": "private-output"})})
    records.save(run)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report, samples = baseline.audit(str(root))
    text = json.dumps([report, samples])
    assert "private-" not in text and "PRIVATE-12" not in text
    assert report["cycles"]["demo"]["runs"] == 1
    assert all(p.read_bytes() == contents for p, contents in before.items())


def test_baseline_rejects_output_inside_history(tmp_path):
    with pytest.raises(SystemExit) as exc:
        baseline.main(["--runs-dir", str(tmp_path), "--output", str(tmp_path / "index.json")])
    assert exc.value.code == 2
    assert not (tmp_path / "index.json").exists()


def test_parallel_approval_failure_stop_and_resume_preserve_proven_work(tmp_path):
    """One synthetic long workflow crosses all baseline lifecycle boundaries.

    Barriers prove overlap; no sleeps, network, paid calls or external effects.
    The approval must be renewed, while successful parallel work is retained.
    """
    barrier = threading.Barrier(2, timeout=10)
    parallel = Plugin(lambda c, s: (barrier.wait(), registry.succeeded())[1])
    approval = Plugin(reusable=False)
    work = Plugin(lambda c, s: registry.failed("test failure"))
    cycle = workflow(node("left", group="Prepare"), node("right", group="Prepare"),
                     dict(id="approve", plugin="approve", needs=["left", "right"], group="Plan"),
                     dict(id="work", plugin="work", needs=["approve"], group="Implement"),
                     node("finish", needs=["work"], group="Verify"))
    plugins = Plugins(test=parallel, approve=approval, work=work)
    # The final step is not one of the parallel pair.
    original = parallel.action
    parallel.action = lambda c, s: registry.succeeded() if s.id == "finish" else original(c, s)
    journal = bus.JsonlObserver(str(tmp_path))
    first = executor.run_cycle(cycle, str(tmp_path), registry=plugins, observer=journal)
    assert first.outcome == "failed"

    def stop(context, step):
        executor.request_stop(context.run.id)
        return registry.failed("stopped")

    work.action = stop
    second = executor.run_cycle(cycle, str(tmp_path), registry=plugins, observer=journal, resume=True)
    assert second.status == second.outcome == "cancelled"
    work.action = lambda c, s: registry.succeeded()
    final = executor.run_cycle(cycle, str(tmp_path), registry=plugins, observer=journal, resume=True)
    journal.close()
    assert final.outcome == "workflow_completed" and final.scope == "resume"
    assert final.coverage == {"completed": 5, "selected": 5, "total": 5}
    assert parallel.calls.count("left") == parallel.calls.count("right") == 1
    assert approval.calls == ["approve"] * 3 and work.calls == ["work"] * 3
    stream = [json.loads(line) for line in (tmp_path / "logs/cycle.jsonl").read_text().splitlines()]
    assert [e["outcome"] for e in stream if e["kind"] == "cycle.run.end"] == [
        "failed", "cancelled", "workflow_completed"]
