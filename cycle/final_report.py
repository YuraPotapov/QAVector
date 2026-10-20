"""Immutable terminal reports, separate from the snapshots made by DAG nodes."""

import logging
import os
import tempfile

from cycle import checkpoints, run as records
from domain.cycle import Artifact, SUCCESS, TERMINAL

log = logging.getLogger("cycle.final_report")


def execution_id(run):
    return "%04d-%04d" % (run.resume_count, run.phase_retry_count)


def report_path(run, kind):
    """Immutable evidence for this continuation, retained across later Resume."""
    return os.path.join("reports", "final", execution_id(run), "run." + kind)


def current_path(kind):
    """Convenience output for the latest recorded verdict, not hashed evidence."""
    return os.path.join("reports", "final", "run." + kind)


def _text(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".report-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def finalize(context):
    """Publish a final pair only for runs that actually requested a report.

    Node artifacts and their hashes remain untouched. Each Resume/phase retry
    gets its own report directory and receipt. A safe headless Pause has no
    terminal verdict and therefore does not publish a final report.
    """
    run = context.run
    if run.status not in TERMINAL or run.ended_at is None:
        return None
    selected = set(run.selected_steps if run.selected_steps is not None else run.steps)
    sources = [name for name, step in run.steps.items()
               if name in selected and step.plugin in ("report.json", "report.html")
               and step.status == SUCCESS and not step.source_run]
    if not sources:
        return None

    from cycle.plugins.report import _page

    document = records.to_document(run)
    fingerprint = checkpoints.digest(document)
    document.update(report_kind="final", generated_by="cycle.executor")
    receipt_path = os.path.join(context.checkpoints.directory, "reports",
                                execution_id(run) + ".json")
    if os.path.exists(receipt_path):
        receipt = checkpoints._read(receipt_path)
        if receipt.get("run_id") != run.id or receipt.get("fingerprint") != fingerprint:
            raise checkpoints.CheckpointError("The final report already records a different execution result.")
        for artifact in receipt.get("artifacts", []):
            if checkpoints.file_digest(os.path.join(context.workspace, artifact["path"])) != artifact["sha256"]:
                raise checkpoints.CheckpointError("A final report artifact changed after publication.")
        _current(context, document)
        return receipt

    paths = {kind: os.path.join(context.workspace, report_path(run, kind))
             for kind in ("json", "html")}
    records._atomic(paths["json"], document)
    prefix = os.path.relpath(context.workspace, os.path.dirname(paths["html"]))
    _text(paths["html"], _page(run, run.cycle_name or run.cycle_id, "cycle.executor",
                                artifact_prefix=prefix, report_kind="final"))
    artifacts = [{"type": kind, "path": report_path(run, kind),
                  "name": "Final run report", "bytes": os.path.getsize(path),
                  "sha256": checkpoints.file_digest(path)} for kind, path in paths.items()]
    receipt = {"schema": 1, "run_id": run.id, "execution_id": execution_id(run),
               "fingerprint": fingerprint, "status": run.status, "outcome": run.outcome,
               "ended_at": run.ended_at, "source_reports": sources, "artifacts": artifacts}
    context.checkpoints.write(receipt_path, receipt)
    _current(context, document)
    for item in artifacts:
        context.observer.artifact("", Artifact(item["type"], item["path"], "",
                                             name=item["name"], bytes=item["bytes"]))
    return receipt


def _current(context, document):
    """Refresh discoverable current reports while retaining all recorded evidence."""
    from cycle.plugins.report import _page

    run = context.run
    paths = {kind: os.path.join(context.workspace, current_path(kind)) for kind in ("json", "html")}
    records._atomic(paths["json"], dict(document, report_binding="current",
                                       immutable_path=report_path(run, "json"),
                                       receipt=os.path.join(".execution", "reports", execution_id(run) + ".json")))
    prefix = os.path.relpath(context.workspace, os.path.dirname(paths["html"]))
    immutable = os.path.relpath(os.path.join(context.workspace, report_path(run, "html")),
                                os.path.dirname(paths["html"]))
    _text(paths["html"], _page(run, run.cycle_name or run.cycle_id, "cycle.executor",
                                artifact_prefix=prefix, report_kind="final", immutable_path=immutable))


def publish(context):
    """Report export errors must not replace the already saved execution verdict."""
    try:
        return finalize(context)
    except (OSError, ValueError, TypeError, checkpoints.CheckpointError) as exc:
        message = "Final report unavailable: %s" % exc
        log.warning("%s", message)
        try:
            context.checkpoints.write(os.path.join(context.checkpoints.directory, "reports",
                                                   execution_id(context.run) + "-error.json"), {
                "schema": 1, "run_id": context.run.id, "state": "failed",
                "execution_id": execution_id(context.run), "message": message})
        except checkpoints.CheckpointError:
            pass  # A full disk may also prevent the diagnostic error receipt.
        return None
