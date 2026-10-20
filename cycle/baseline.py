"""Read-only run-history audit: ``python -m cycle.baseline --runs-dir PATH``.

Reports and optional representative fixtures exclude subjects, variables,
commands, logs, artifacts and output text. Run ids and clocks are anonymized.
Only the explicitly supplied output files are written; history is never saved.
"""

import argparse
import json
import os
from collections import Counter

from cycle import outcomes, run as records, workspace
from domain.cycle import SKIPPED


# Export formats must not grow when the live UI projection gains new details.
# Fixture schema 2 preserves modern execution evidence; the checked-in schema-1
# historical fixtures remain a separate compatibility baseline.
FIXTURE_SCHEMA = 2


def audit(root, cycles=()):
    report, samples = [], {}
    for run_id in reversed(workspace.existing_runs(root)):
        run = records.load(workspace.path_of(run_id, root))
        if run is None or (cycles and run.cycle_id not in cycles):
            continue
        presentation = outcomes.refresh(run)
        projection = {key: presentation[key] for key in outcomes.FIELDS}
        row = {"run": "run-%03d" % (len(report) + 1), "cycle": run.cycle_id,
               "status": run.status, "reached": records.reached(run),
               "duration_ms": round(run.duration_ms, 1), **projection}
        report.append(row)
        kind = (run.cycle_id, run.scope, run.status, run.outcome)
        if kind not in samples or len(run.steps) >= len(samples[kind]["metadata"]["steps"]):
            samples[kind] = _sample(run, row)
    grouped = {}
    for cycle_id in sorted({row["cycle"] for row in report}):
        rows = [row for row in report if row["cycle"] == cycle_id]
        grouped[cycle_id] = {
            "runs": len(rows), "scopes": dict(Counter(row["scope"] for row in rows)),
            "statuses": dict(Counter(row["status"] for row in rows)),
            "outcomes": dict(Counter(row["outcome"] for row in rows)),
            "reached_phases": dict(Counter(row["current_phase"] or "ungrouped" for row in rows)),
            "duration_ms": round(sum(row["duration_ms"] for row in rows), 1),
        }
    return {"schema": 1, "cycles": grouped, "runs": report}, {
        "schema": FIXTURE_SCHEMA, "runs": list(samples.values())}


def _sample(run, row):
    """Keep execution evidence, never copy a UI receipt or plugin payload.

    Structural cycle/step/phase ids and milestone names remain useful for
    comparing graphs. Human descriptions, settings, paths, resource identities
    and operation receipts do not belong in an anonymized fixture.
    """
    clocks = [value for step in run.steps.values() for value in (step.started_at, step.ended_at) if value]
    origin = run.started_at or min(clocks, default=0)
    steps = {}
    for name, step in run.steps.items():
        saved = {"step_id": name, "plugin": step.plugin, "status": step.status,
                 "attempts": step.attempts, "duration_ms": round(step.duration_ms, 1),
                 "reason_code": step.reason_code, "blocked_by": step.blocked_by,
                 "group": step.group, "phase": step.phase,
                 "source_run": "earlier-run" if step.source_run or step.message.startswith("reused from ") else ""}
        for clock in ("started_at", "ended_at"):
            value = getattr(step, clock)
            # Keep ordering even for sub-millisecond steps or a legacy run
            # without started_at; rounded ties can change the reached phase.
            saved[clock] = 1000 + (value - origin) if value else value
        if step.status == SKIPPED:
            # Messages are only retained in their canonical generic forms.
            saved["message"] = {
                "not_selected": "not part of this run", "disabled": "disabled",
                "condition_false": "if: false was not true",
                "unavailable": "if: output unavailable",
                "blocked": "%s %s" % (step.blocked_by,
                    getattr(run.steps.get(step.blocked_by), "status", "skipped")),
            }.get(step.reason_code, "")
        if step.plugin == "jira.issues" and type(step.outputs.get("count")) is int:
            saved["outputs"] = {"count": step.outputs["count"]}
        elif step.plugin == "task.brief" and isinstance(step.outputs.get("brief"), dict):
            brief = step.outputs["brief"]
            saved["outputs"] = {"brief": {"ready": brief["ready"]}
                                if type(brief.get("ready")) is bool else {}}
        elif step.plugin == "approval.gate" and type(step.outputs.get("approved")) is bool:
            saved["outputs"] = {"approved": step.outputs["approved"]}
        steps[name] = saved
    metadata = {"id": row["run"], "cycle_id": run.cycle_id,
                "schema": records.SCHEMA, "status": run.status,
                "started_at": 1000, "ended_at": 1000 + run.duration_ms / 1000
                if run.ended_at is not None else None,
                "duration_ms": round(run.duration_ms, 1),
                "resume_count": run.resume_count, "scope": run.scope,
                "selected_steps": list(run.selected_steps) if run.selected_steps is not None else None,
                "steps": steps}
    if run.action_id:
        metadata.update(action_id=run.action_id,
                        action_definition={"result": run.action_definition.get("result", "")})
    if run.phase_definition:
        metadata["phase_definition"] = [{"id": phase["id"],
            "deliverables": list(phase.get("deliverables", []))} for phase in run.phase_definition]
        metadata["phase_receipts"] = {name: {key: receipt[key] for key in ("status", "outcome") if key in receipt}
                                      for name, receipt in run.phase_receipts.items()}
        metadata["last_completed_phase"] = run.last_completed_phase
    if run.paused or run.pause_requested:
        metadata.update(paused=run.paused, pause_requested=run.pause_requested,
                        pause_reason="phase_boundary" if run.pause_reason == "phase_boundary" else "user_request")
    if run.initial_execution_mode:
        metadata.update(initial_execution_mode=run.initial_execution_mode,
                        execution_mode=run.execution_mode,
                        execution_policy_version=run.execution_policy_version,
                        node_approvals={"approval-%03d" % (index + 1): {
                            "step": receipt.get("step", ""), "status": receipt.get("status", "")}
                            for index, receipt in enumerate(run.node_approvals.values())})
    expected = outcomes.refresh(records.from_document(metadata))
    if any(expected[key] != row[key] for key in outcomes.FIELDS):
        raise ValueError("Anonymized fixture did not preserve execution semantics.")
    return {"metadata": metadata, "expected": expected}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--cycle", action="append", default=[])
    parser.add_argument("--output", help="Anonymized JSON report; stdout if omitted")
    parser.add_argument("--fixtures", help="Representative anonymized JSON fixtures (fixture schema 2)")
    args = parser.parse_args(argv)
    root = os.path.realpath(args.runs_dir)
    if not os.path.isdir(root):
        parser.error("runs directory does not exist")
    for destination in (args.output, args.fixtures):
        if destination and os.path.commonpath([root, os.path.realpath(destination)]) == root:
            parser.error("output must be outside the run history")
    report, fixtures = audit(root, args.cycle)
    for destination, payload in ((args.output, report), (args.fixtures, fixtures)):
        if destination:
            with open(destination, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
    if not args.output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
