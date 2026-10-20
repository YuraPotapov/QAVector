"""Business presentation of schema-1 runs, including records made before it.

No I/O and no changes to the technical verdict. Coverage counts successful
selected steps, including retained work on Resume, but never results borrowed
from another run. Groups are descriptive phases until phase policies exist.
"""

from domain.cycle import (FAILED, PENDING, RUNNING, SKIPPED,
                          SUCCESS, TIMEOUT, WAITING)

FIELDS = ("execution_status", "scope", "coverage", "outcome", "current_phase")
DETAIL_FIELDS = ("action_id", "task_brief", "phase_definition", "phase_receipts", "last_completed_phase",
                 "pause_reason", "pause_requested", "paused", "next_action", "control_history", "phase_actions")
POLICY_FIELDS = ("initial_execution_mode", "execution_mode", "execution_policy_version", "node_approvals", "policy_changes")


def skip_reason(step, steps):
    """A stable reason and causal dependency, with conservative legacy inference."""
    if step.reason_code:
        return step.reason_code, step.blocked_by
    message = step.message or ""
    if message.startswith("not part of this run"):
        return "not_selected", ""
    if message == "disabled":
        return "disabled", ""
    if message.startswith("if:"):
        return ("condition_false" if message.endswith("was not true")
                else "unavailable"), ""
    for name, dependency in steps.items():
        if message == "%s %s" % (name, dependency.status):
            if dependency.plugin == "approval.gate" and dependency.outputs.get("approved") is False:
                return "rejected", name
            return "blocked", name
    if message.startswith("needs "):
        return "unavailable", ""
    return "plugin_skipped", ""


def refresh(run, graph=None):
    """Refresh a projection from step evidence. Safe to call at each checkpoint.

    Legacy inference uses saved receipts and the run's own graph, never today's
    YAML. Nothing is written or migrated just because a run was opened.
    """
    nodes = {node["id"]: node for node in (graph or {}).get("nodes", [])
             if node.get("id")}
    for name, step in run.steps.items():
        if name in nodes:
            step.group = nodes[name].get("group") or ""
        if step.status == SKIPPED:
            step.reason_code, step.blocked_by = skip_reason(step, run.steps)
    if not run.scope:
        partial = any(s.source_run or s.message.startswith("reused from ")
                      or s.reason_code == "not_selected"
                      for s in run.steps.values())
        run.scope = ("resume" if run.resume_count else
                     "partial" if partial else "full")
    if run.selected_steps is None:
        selected = [name for name, s in run.steps.items()
                    if not s.source_run and not s.message.startswith("reused from ")
                    and s.reason_code != "not_selected"]
    else:
        selected = [name for name in run.selected_steps if name in run.steps]
    # A stale or duplicated id must not inflate coverage.
    selected = list(dict.fromkeys(selected))
    steps = [run.steps[name] for name in selected]
    run.coverage = {"completed": sum(s.status == SUCCESS for s in steps),
                    "selected": len(steps), "total": len(run.steps)}
    run.execution_status = {SUCCESS: "completed", TIMEOUT: "failed"}.get(
        run.status, run.status)
    if run.status in (RUNNING, PENDING, WAITING) and (run.paused or run.pause_requested):
        run.execution_status = "paused" if run.paused else "pausing"
    elif run.status in (RUNNING, PENDING, WAITING) and any(receipt.get("status") == "pending"
            and run.steps.get(receipt.get("step")) is not None and run.steps[receipt["step"]].status == PENDING
            for receipt in run.node_approvals.values()):
        run.execution_status = "waiting_for_node_approval"
    reached = None
    for state in (WAITING, RUNNING):
        active = [s for s in run.steps.values() if s.status == state]
        if active:
            reached = max(active, key=lambda s: s.started_at or 0)
            break
    if reached is None:
        failed = [s for s in steps if s.status in (FAILED, TIMEOUT) and s.ended_at]
        ended = [s for s in steps if s.ended_at and s.status == SUCCESS]
        reached = (min(failed, key=lambda s: s.ended_at) if failed else
                   max(ended, key=lambda s: s.ended_at) if ended else None)
    run.current_phase = (reached.phase or reached.group) if reached else ""
    if run.phase_definition:
        for phase in run.phase_definition:
            members = [node["id"] for node in nodes.values() if node.get("phase") == phase["id"]]
            if not members:
                members = [name for name, step in run.steps.items() if step.phase == phase["id"]]
            if any(run.steps[name].status not in (SUCCESS, SKIPPED) for name in members if name in run.steps):
                run.current_phase = phase["id"]
                break
        if run.paused and run.pause_reason == "phase_boundary":
            run.current_phase = run.last_completed_phase
    run.task_brief = _task_brief(run, selected)
    if run.paused and run.status == WAITING:
        completed_phase = run.phase_receipts.get(run.last_completed_phase) or {}
        run.outcome = (completed_phase.get("outcome") or "paused") if run.pause_reason == "phase_boundary" else "paused"
    elif run.status in (RUNNING, PENDING, WAITING):
        run.outcome = "in_progress"
    elif run.status != SUCCESS:
        run.outcome = run.execution_status
    elif _empty_query(steps):
        run.outcome = "no_work_found"
    elif run.action_id == "prepare_task" and run.task_brief.get("ready") is True:
        run.outcome = "task_prepared"
    elif any(s.status != SUCCESS and not (
            s.status == SKIPPED and s.reason_code in ("condition_false", "disabled"))
             for s in steps):
        run.outcome = "workflow_incomplete"
    elif not any(s.status == SUCCESS for s in steps):
        run.outcome = "no_work_performed"
    elif run.scope == "partial" or len(selected) < len(run.steps):
        run.outcome = "partial_completed"
    else:
        run.outcome = "workflow_completed"
    detailed = (run.action_id or run.task_brief or run.phase_definition or run.phase_actions or
                run.control_history or run.paused or run.pause_requested)
    return {name: getattr(run, name) for name in FIELDS + (DETAIL_FIELDS if detailed else ()) +
            (POLICY_FIELDS if run.initial_execution_mode else ())}


def _task_brief(run, selected):
    """Project the action result or the full run's declared brief deliverable.

    Phase deliverables disambiguate multiple producers. A cycle with just one
    selected Task Brief also has an unambiguous result without named phases.
    Failed preparation may still provide useful evidence in its brief.
    """
    if run.action_id:
        names = [run.action_definition.get("result", "")]
    else:
        producers = [name for name in selected if run.steps[name].plugin == "task.brief"]
        declared = [name for phase in run.phase_definition
                    for name in phase.get("deliverables", []) if name in producers]
        names = declared or (producers if len(producers) == 1 else [])
    candidates = [run.steps[name] for name in names if name in run.steps
                  and name in selected and run.steps[name].plugin == "task.brief"
                  and isinstance(run.steps[name].outputs.get("brief"), dict)]
    if not candidates:
        return {}
    # Later evidence replaces an earlier brief; order resolves identical clocks
    # consistently during live checkpoints and metadata replay.
    result = max(enumerate(candidates), key=lambda pair: (pair[1].ended_at or 0, pair[0]))[1]
    return dict(result.outputs["brief"])


def _empty_query(steps):
    # An empty subject alone proves nothing: a run may have stopped before
    # looking, or may have done unrelated work. Only successful empty Jira
    # reads with no other executed work justify this no-op outcome.
    executed = [s for s in steps if s.status != SKIPPED]
    return bool(executed) and all(
        s.status == SUCCESS and s.plugin == "jira.issues"
        and type(s.outputs.get("count")) is int and s.outputs["count"] == 0
        for s in executed)
