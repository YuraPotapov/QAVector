"""Explicit human feedback revisits only the read-only path to its approval.

A revision is a durable state transition within one execution, not a new run
or a hidden agent loop inside the gate. Previous plans and feedback are kept.
"""

import copy
import os
import shutil
import time

from cycle import checkpoints, model, run as records, variables
from domain.cycle import PENDING, SKIPPED, StepRun


def targets(cycle, gate_id, target_id):
    if cycle is None:
        raise ValueError("Plan revision requires a cycle graph.")
    target, gate = cycle.step(target_id), cycle.step(gate_id)
    if not gate or gate.plugin != "approval.gate":
        raise ValueError("Only an approval gate can request a plan revision.")
    if not target or target.plugin != "agent.review":
        raise ValueError("Plan step to revise must name an agent.review step.")
    before = model.upstream(cycle, {gate_id})
    if target_id not in before:
        raise ValueError("The approval must depend on the plan step to revise.")
    path = model.downstream(cycle, {target_id}) & before
    if any(cycle.step(name).plugin not in ("agent.review", "check.gate") for name in path):
        raise ValueError("Plan revision may revisit only read-only agents and checks before approval.")
    return path | {gate_id}


def settings(step, run, scope):
    resolved = variables.resolve(step.settings, scope)
    feedback = run.revision_inputs.get(step.id)
    if feedback:
        resolved = copy.deepcopy(resolved)
        resolved["inputs"] = dict(resolved.get("inputs") or {}, user_revision=feedback)
        resolved["task"] = str(resolved.get("task") or "") + (
            "\n\nRevise the previous plan using inputs.user_revision. Address the person's "
            "feedback and preserve prior agreed requirements. Return the complete updated "
            "plan in the required review format, explaining anything that remains unresolved.")
    return resolved


def apply(cycle, run, gate, request, store, inflight):
    target = str(request.get("target") or "")
    if target != gate.settings.get("revision_step"):
        raise ValueError("The requested plan revision differs from the configured target.")
    names = targets(cycle, gate.id, target)
    feedback = str(request.get("feedback") or "").strip()
    if not feedback or len(feedback) > 8000:
        raise ValueError("Plan feedback must contain between 1 and 8,000 characters.")
    previous = [one for one in run.revisions if one["gate"] == gate.id]
    if len(previous) >= int(gate.settings.get("max_revisions", 3)):
        raise ValueError("The plan revision limit has been reached.")
    if names & set(inflight):
        raise ValueError("Wait until the plan review has finished before revising it.")
    outsiders = model.downstream(cycle, {target}) - names
    started = [name for name in outsiders if run.steps[name].status not in (PENDING, SKIPPED)]
    if started:
        raise ValueError("Cannot revise a plan after its dependent work started: %s." % ", ".join(sorted(started)))

    number = len(run.revisions) + 1
    directory = os.path.join(store.directory, "revisions", "%04d" % number)
    record = {"number": number, "run_id": run.id, "gate": gate.id, "target": target,
              "feedback": feedback, "who": str(request.get("who") or ""),
              "created_at": time.time(), "steps": sorted(names)}
    # Archive before any reset or new provider call. Operation receipts remain
    # in their original paths too; differing revision inputs give new call keys.
    store.write(os.path.join(directory, "before.json"), records.to_document(run))
    store.write(os.path.join(directory, "feedback.json"), record)
    try:
        for name in names:
            source = os.path.join(run.workspace, "steps", name)
            if os.path.isdir(source):
                shutil.copytree(source, os.path.join(directory, "steps", name), dirs_exist_ok=True)
    except OSError as exc:
        raise checkpoints.CheckpointError("Cannot save execution state: plan history: %s" % exc) from exc

    updated = copy.deepcopy(run)
    updated.revisions.append(record)
    updated.revision_inputs[target] = {
        "round": len(previous) + 1, "feedback": feedback,
        "previous_feedback": [one["feedback"] for one in previous],
        "previous_plan": copy.deepcopy(run.steps[target].outputs)}
    for name in names:
        updated.steps[name] = StepRun(name, plugin=cycle.step(name).plugin,
                                      attempts=run.steps[name].attempts)
    store.publish(updated, names)
    run.steps, run.revisions, run.revision_inputs = (
        updated.steps, updated.revisions, updated.revision_inputs)
    store.finish_transition()
    return names
