"""Run presentation on the wire, with a conservative fallback for old journals.

New cores supply the business outcome. Legacy JSONL has only technical status,
run mode and step messages; infer just what those events can prove. No core
imports: the desktop package still runs with PySide6 alone.
"""

FIELDS = ("execution_status", "scope", "coverage", "outcome", "current_phase")
DETAIL_FIELDS = ("action_id", "task_brief", "phase_definition", "phase_receipts", "last_completed_phase",
                 "pause_reason", "pause_requested", "paused", "next_action", "control_history", "phase_actions",
                 "initial_execution_mode", "execution_mode", "execution_policy_version", "node_approvals", "policy_changes")


def update(cycle, event=None):
    event = event or {}
    steps = cycle.get("steps") or {}
    mode = (cycle.get("mode") or {}).get("mode")
    scope = cycle.get("scope") or ("full" if mode == "fresh" else mode) or "full"
    selected = cycle.get("selected_steps")
    if selected is None:
        selected = [name for name, step in steps.items()
                    if scope not in ("partial", "resume") or not (
                        step.get("source_run") or
                        step.get("reason_code") == "not_selected" or
                        step.get("message", "").startswith(("not part of this run", "reused from ")))]
    selected = [steps[name] for name in dict.fromkeys(selected) if name in steps]
    cycle["scope"] = scope
    cycle["coverage"] = {"completed": sum(s.get("status") == "success" for s in selected),
                         "selected": len(selected), "total": len(steps)}
    status = cycle.get("status") or "pending"
    cycle["execution_status"] = {"success": "completed", "timeout": "failed"}.get(status, status)
    if status in ("running", "pending", "waiting") and (cycle.get("paused") or cycle.get("pause_requested")):
        cycle["execution_status"] = "paused" if cycle.get("paused") else "pausing"
    elif status in ("running", "pending", "waiting") and any(one.get("status") == "pending" for one in (cycle.get("node_approvals") or {}).values()):
        cycle["execution_status"] = "waiting_for_node_approval"
    reached = ""
    for wanted in ("waiting", "running"):
        active = [name for name, step in steps.items() if step.get("status") == wanted]
        if active:
            reached = max(active, key=lambda name: steps[name].get("started_at") or 0)
            break
    if not reached:
        failed = [name for name, s in steps.items()
                  if s.get("status") in ("failed", "timeout") and s.get("ended_at")]
        ended = [name for name, s in steps.items() if s.get("status") == "success" and s.get("ended_at")]
        reached = (min(failed, key=lambda name: steps[name]["ended_at"]) if failed else
                   max(ended, key=lambda name: steps[name]["ended_at"]) if ended else "")
    cycle["current_phase"] = next((node.get("group") or "" for node in
                                   (cycle.get("graph") or {}).get("nodes", [])
                                   if node.get("id") == reached), "")
    if cycle.get("paused") and status == "waiting":
        receipt = (cycle.get("phase_receipts") or {}).get(cycle.get("last_completed_phase")) or {}
        cycle["outcome"] = (receipt.get("outcome") or "paused") if cycle.get("pause_reason") == "phase_boundary" else "paused"
    elif status in ("running", "pending", "waiting"):
        cycle["outcome"] = "in_progress"
    elif status != "success":
        cycle["outcome"] = cycle["execution_status"]
    else:
        executed = [s for s in selected if s.get("status") != "skipped"]
        empty = bool(executed) and all(
            s.get("plugin") == "jira.issues" and s.get("status") == "success"
            and type(s.get("outputs", {}).get("count")) is int
            and s["outputs"]["count"] == 0 for s in executed)
        incomplete = any(s.get("status") != "success" and not (
            s.get("status") == "skipped" and (
                s.get("reason_code") in ("condition_false", "disabled")
                or s.get("message") == "disabled"
                or (s.get("message", "").startswith("if:")
                    and s.get("message", "").endswith("was not true")))) for s in selected)
        cycle["outcome"] = ("no_work_found" if empty else
                            "workflow_incomplete" if incomplete else
                            "no_work_performed" if not any(s.get("status") == "success" for s in selected) else
                            "partial_completed" if scope == "partial" or len(selected) < len(steps) else "workflow_completed")
    # A new core's durable projection wins over any fallback or missed events.
    for key in FIELDS + DETAIL_FIELDS:
        if key in event:
            cycle[key] = event[key]


def completion(record):
    """A terminal success's (label, tone), never an unqualified 'Passed'."""
    outcome = record.get("outcome")
    coverage = record.get("coverage") or {}
    count = (" · %s/%s steps" % (coverage.get("completed", 0), coverage["total"])
             if "total" in coverage else "")
    if outcome == "no_work_found":
        return "No work found", "neutral"
    if outcome == "task_prepared":
        return "Task prepared · Prepare only" + count, "neutral"
    if outcome == "no_work_performed":
        return "No work performed" + count, "neutral"
    if outcome == "workflow_incomplete":
        return "Workflow incomplete" + count, "neutral"
    selected, total = coverage.get("selected"), coverage.get("total")
    partial_coverage = type(selected) is int and type(total) is int and selected < total
    if record.get("scope") == "partial" or outcome == "partial_completed" or partial_coverage:
        return "Partial run completed" + count, "neutral"
    if outcome == "workflow_completed":
        return "Workflow completed" + count, "ok"
    return "Execution completed" + count, "neutral"
