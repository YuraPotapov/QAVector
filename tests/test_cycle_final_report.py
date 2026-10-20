"""Final verdicts are exported without rewriting historical step snapshots."""

import json
from html.parser import HTMLParser
import pytest

from cycle import checkpoints, executor, final_report, model, registry
from cycle.context import RunContext


class Work(registry.CyclePlugin):
    metadata = registry.PluginMetadata(id="test.final", name="Synthetic work")

    def __init__(self, fail=False):
        self.fail = fail

    def execute(self, context, step):
        return registry.failed("synthetic failure") if self.fail else registry.succeeded()


class Plugins:
    def __init__(self, work):
        self.work = work

    def get(self, name):
        return self.work if name == "test.final" else registry.get(name)


def workflow():
    return model.parse_cycle({"id": "final_reports", "steps": [
        {"id": "json", "plugin": "report.json"},
        {"id": "html", "plugin": "report.html", "needs": ["json"]},
        {"id": "work", "plugin": "test.final", "needs": ["html"]}]}, "final_reports")


def execute(tmp_path, work=None, **options):
    return executor.run_cycle(workflow(), str(tmp_path), registry=Plugins(work or Work()), **options)


def read(tmp_path, path):
    return json.loads((tmp_path / path).read_text(encoding="utf-8"))


def links(path):
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.targets = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.targets.extend(value for key, value in attrs if key == "href")

    parser = Links()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.targets


def test_final_reports_contain_terminal_verdict_and_keep_snapshot_hashes(tmp_path):
    run = execute(tmp_path)
    snapshot = read(tmp_path, run.steps["json"].outputs["path"])
    final = read(tmp_path, final_report.report_path(run, "json"))
    page = (tmp_path / final_report.report_path(run, "html")).read_text(encoding="utf-8")
    receipt = read(tmp_path, ".execution/reports/0000-0000.json")

    assert snapshot["report_kind"] == "snapshot"
    assert snapshot["status"] == "running" and snapshot["snapshot_at"] > 0
    assert final["report_kind"] == "final"
    assert final["status"] == "success" and final["outcome"] == "workflow_completed"
    assert final["ended_at"] == run.ended_at and final["duration_ms"] > 0
    assert all(step["status"] == "success" for step in final["steps"].values())
    assert "Final execution result" in page and "Workflow completed" in page
    assert "still running" not in page
    for step in run.steps.values():
        for path, digest in step.artifact_digests.items():
            assert checkpoints.file_digest(str(tmp_path / path)) == digest
    for artifact in receipt["artifacts"]:
        assert checkpoints.file_digest(str(tmp_path / artifact["path"])) == artifact["sha256"]


def test_failure_after_report_exports_failed_final_result(tmp_path):
    run = execute(tmp_path, Work(fail=True))
    final = read(tmp_path, final_report.report_path(run, "json"))
    assert run.status == final["status"] == "failed"
    assert final["steps"]["work"]["status"] == "failed"
    assert final["ended_at"] is not None


def test_partial_final_report_uses_neutral_business_result(tmp_path):
    run = execute(tmp_path, only={"json", "html"})
    final = read(tmp_path, final_report.report_path(run, "json"))
    page = (tmp_path / final_report.report_path(run, "html")).read_text(encoding="utf-8")
    assert final["scope"] == "partial" and final["outcome"] == "partial_completed"
    assert final["coverage"] == {"completed": 2, "selected": 2, "total": 3}
    assert "Partial run completed" in page and "Workflow completed" not in page


def test_resume_preserves_old_final_and_node_receipts(tmp_path):
    work = Work(fail=True)
    first = execute(tmp_path, work)
    old_report = tmp_path / final_report.report_path(first, "json")
    old_receipt = tmp_path / ".execution/reports/0000-0000.json"
    snapshot = tmp_path / first.steps["json"].outputs["path"]
    preserved = {path: path.read_bytes() for path in (old_report, old_receipt, snapshot)}

    work.fail = False
    resumed = execute(tmp_path, work, resume=True)

    assert resumed.status == "success"
    assert final_report.report_path(resumed, "json") != final_report.report_path(first, "json")
    assert read(tmp_path, final_report.report_path(resumed, "json"))["status"] == "success"
    current = read(tmp_path, resumed.steps["json"].outputs["final_path"])
    assert current["status"] == "success" and current["report_binding"] == "current"
    assert current["immutable_path"] == final_report.report_path(resumed, "json")
    assert (tmp_path / current["receipt"]).is_file()
    assert all(path.read_bytes() == value for path, value in preserved.items())
    assert resumed.steps["json"].attempts == 1


def test_final_receipt_is_idempotent_and_rejects_modified_artifacts(tmp_path):
    run = execute(tmp_path)
    context = RunContext(run, workflow(), str(tmp_path))
    context.checkpoints = checkpoints.Store(str(tmp_path))
    context.observer = None  # A replay with a saved receipt emits no new artifact events.
    receipt = tmp_path / ".execution/reports/0000-0000.json"
    original = receipt.read_bytes()
    assert final_report.finalize(context)["run_id"] == run.id
    assert receipt.read_bytes() == original
    final = tmp_path / final_report.report_path(run, "json")
    final.write_text("tampered", encoding="utf-8")
    with pytest.raises(checkpoints.CheckpointError, match="changed after publication"):
        final_report.finalize(context)
    assert final.read_text(encoding="utf-8") == "tampered"


def test_final_html_links_resolve_from_its_nested_directory(tmp_path):
    run = execute(tmp_path)
    path = tmp_path / final_report.report_path(run, "html")

    assert links(path)
    assert all((path.parent / target).is_file() for target in links(path))


def test_nested_snapshot_and_current_html_links_resolve(tmp_path):
    definition = workflow()
    definition.step("html").settings["path"] = "nested/snapshot.html"
    run = executor.run_cycle(definition, str(tmp_path), registry=Plugins(Work()))
    for name in ("path", "final_path"):
        path = tmp_path / run.steps["html"].outputs[name]
        assert links(path)
        assert all((path.parent / target).is_file() for target in links(path))


def test_safe_phase_pause_does_not_publish_a_terminal_report(tmp_path):
    definition = model.parse_cycle({"id": "paused_reports", "phases": [
        {"id": "first", "policy": "stop_after"}, {"id": "second"}], "steps": [
        {"id": "json", "plugin": "report.json", "phase": "first"},
        {"id": "work", "plugin": "test.final", "phase": "second"}]}, "paused_reports")
    run = executor.run_cycle(definition, str(tmp_path), registry=Plugins(Work()))
    assert run.paused and run.status == "waiting" and run.ended_at is None
    assert not (tmp_path / "reports/final").exists()


def test_skipped_report_does_not_publish_a_final_report(tmp_path):
    definition = model.parse_cycle({"id": "skip_reports", "steps": [
        {"id": "json", "plugin": "report.json", "if": "'yes' == 'no'"}]}, "skip_reports")
    run = executor.run_cycle(definition, str(tmp_path))
    assert run.status == "success"
    assert not (tmp_path / "reports/final").exists()


def test_report_export_failure_keeps_execution_verdict_and_records_diagnostic(tmp_path, monkeypatch, caplog):
    def full_disk(*args, **kwargs):
        raise OSError("synthetic full disk")

    monkeypatch.setattr(final_report, "_text", full_disk)
    run = execute(tmp_path)
    assert run.status == "success"
    assert read(tmp_path, "metadata.json")["status"] == "success"
    assert not (tmp_path / ".execution/reports/0000-0000.json").exists()
    failure = read(tmp_path, ".execution/reports/0000-0000-error.json")
    assert failure["state"] == "failed" and "synthetic full disk" in failure["message"]
    assert "Final report unavailable" in caplog.text
