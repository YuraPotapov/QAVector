"""Explicit execution phases. Legacy visual groups confer no execution policy."""

import copy
import os
import threading
import time
from dataclasses import asdict

from domain.cycle import CyclePhase, SUCCESS, SKIPPED, FAILED, TIMEOUT, TERMINAL

POLICIES = ("automatic", "stop_after", "approval_required", "manual")
KEYS = frozenset(CyclePhase.__dataclass_fields__)


def parse(raw):
    from cycle.model import CycleError
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(one, dict) for one in raw):
        raise CycleError("phases must be a list of phase definitions")
    result = []
    for one in raw:
        extra = set(one) - KEYS
        if extra:
            raise CycleError("phases: unknown keys %s" % ", ".join(sorted(extra)))
        for key in ("deliverables", "effects"):
            if key in one and (not isinstance(one[key], list) or any(not isinstance(value, str) for value in one[key])):
                raise CycleError("phases.%s must be a list of names" % key)
        for key in KEYS - {"deliverables", "effects"}:
            if key in one and not isinstance(one[key], str):
                raise CycleError("phases.%s must be text" % key)
        values = {key: tuple(value) if key in ("deliverables", "effects") else value.strip() for key, value in one.items()}
        result.append(CyclePhase(**dict({"id": ""}, **values)))
    return result


def document(phase):
    return {key: list(value) if isinstance(value, tuple) else value for key, value in asdict(phase).items() if value}


def problems(cycle):
    from cycle import model
    found, seen = [], set()
    indices = {phase.id: i for i, phase in enumerate(cycle.phases)}
    for phase in cycle.phases:
        if not model.ID_PATTERN.fullmatch(phase.id) or phase.id in seen:
            found.append("phases: invalid or duplicate phase id %r" % phase.id)
        seen.add(phase.id)
        if phase.policy not in POLICIES:
            found.append("phases.%s: policy must be %s" % (phase.id, ", ".join(POLICIES)))
        members = [step for step in cycle.steps if step.phase == phase.id]
        if not members:
            found.append("phases.%s: phase has no steps" % phase.id)
        for name in phase.deliverables:
            if name not in {step.id for step in members}:
                found.append("phases.%s: deliverable %s must name a step in this phase" % (phase.id, name))
        if phase.policy == "approval_required":
            gate = cycle.step(phase.approval)
            if gate is None or gate.plugin != "approval.gate" or gate.phase != phase.id:
                found.append("phases.%s: approval must name an approval.gate inside the phase" % phase.id)
            elif any(step.phase == phase.id and step.id != gate.id and gate.id not in model.upstream(cycle, {step.id})
                     for step in members):
                found.append("phases.%s: every operation must depend on its approval gate" % phase.id)
        elif phase.approval:
            found.append("phases.%s: approval requires policy approval_required" % phase.id)
    for step in cycle.steps:
        if step.phase and step.phase not in indices:
            found.append("%s: unknown phase %s" % (step.id, step.phase))
        if cycle.phases and not step.phase:
            found.append("%s: declare phase explicitly; group is visual only" % step.id)
        if not cycle.phases and step.phase:
            found.append("%s: phase needs a top-level phases declaration" % step.id)
        for name in step.needs:
            parent = cycle.step(name)
            if parent and indices.get(parent.phase, -1) > indices.get(step.phase, -1):
                found.append("%s: dependency %s points back from a later phase" % (step.id, name))
    return found


class Phases:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.run, self.cycle = scheduler.run, scheduler.cycle
        self.run_one = (self.run.execution_bound or {}).get("phase", "")
        self.allowed_manual = {self.run_one} if self.run_one else set()
        self.consumed = {name for name, receipt in self.run.phase_receipts.items()
                         if receipt.get("status") == SUCCESS and name != self.run_one} if self.run.resume_count else set()
        self.selected = set(self.run.selected_steps or [])

    def members(self, phase):
        return [step.id for step in self.cycle.steps if step.phase == phase.id and step.id in self.selected]

    def frontier(self):
        for phase in self.cycle.phases:
            members = self.members(phase)
            if members and any(self.run.steps[name].status not in TERMINAL or self.run.steps[name].status in (FAILED, TIMEOUT) for name in members):
                return phase
        return None

    def refresh(self):
        """Write completed receipts before any later phase can be submitted."""
        from cycle import checkpoints
        for phase in self.cycle.phases:
            members = self.members(phase)
            if not members:
                continue
            steps = [self.run.steps[name] for name in members]
            terminal = all(step.status in TERMINAL for step in steps)
            failed = any(step.status in (FAILED, TIMEOUT) for step in steps)
            old = self.run.phase_receipts.get(phase.id)
            if not terminal and not failed:
                if old and old.get("status") in TERMINAL:
                    old["invalidated"] = True
                break
            signature = checkpoints.digest([{key: getattr(step, key) for key in (
                "step_id", "status", "input_digest", "definition_digest", "artifact_digests", "execution_generation")} for step in steps])
            if old and old.get("signature") == signature:
                continue
            complete = all(step.status == SUCCESS or (step.status == SKIPPED and step.reason_code in ("condition_false", "disabled")) for step in steps)
            # A no-task branch cannot produce a phase milestone.
            from cycle import outcomes
            empty = outcomes._empty_query(steps)
            status = FAILED if failed else SUCCESS if complete and not empty and any(step.status == SUCCESS for step in steps) else SKIPPED
            deliverables = []
            for name in phase.deliverables or members:
                if name not in members:
                    continue
                step = self.run.steps[name]
                if step.status != SUCCESS:
                    continue
                for artifact in step.artifacts:
                    deliverables.append({"name": artifact.name, "path": artifact.path, "producer": name,
                                         "sha256": step.artifact_digests.get(artifact.path, "")})
            receipt = {"phase": phase.id, "title": phase.title or phase.id, "status": status,
                       "generation": (old or {}).get("generation", 0) + 1,
                       "started_at": min((step.started_at for step in steps if step.started_at), default=None),
                       "ended_at": max((step.ended_at for step in steps if step.ended_at), default=time.time()),
                       "steps": members, "deliverables": deliverables, "signature": signature,
                       "scope": "full" if len(members) == sum(step.phase == phase.id for step in self.cycle.steps) else "partial",
                       "evidence": {step.step_id: {"status": step.status, "receipt": ".execution/steps/" + step.step_id + ".json",
                                                  "execution_generation": step.execution_generation,
                                                  "input_digest": step.input_digest, "definition_digest": step.definition_digest} for step in steps},
                       "effects": list(phase.effects), "effects_verification": "declared",
                       "outcome": phase.outcome if status == SUCCESS and len(members) == sum(step.phase == phase.id for step in self.cycle.steps) else ""}
            self.scheduler.context.checkpoints.phase(self.run, receipt)
            if status == SUCCESS:
                self.run.last_completed_phase = phase.id
            self.scheduler.observer.phase(self.run, receipt)
            if not terminal:
                break

    def before_submit(self):
        self.refresh()
        frontier = self.frontier()
        if not frontier:
            self.run.execution_bound = {}
            self.run_one = ""
            return
        members = self.members(frontier)
        if frontier.policy == "approval_required" and self.run.steps[frontier.approval].status in (FAILED, TIMEOUT):
            # A conditional dependency may deliberately run after a failed
            # ordinary step. It cannot override the phase's approval policy.
            self.scheduler.pause_at("phase_failed", "Retry phase: " + frontier.id)
            return
        if (not self.run.action_id and all(self.run.steps[name].status in TERMINAL for name in members)
                and any(self.run.steps[name].status in (FAILED, TIMEOUT) for name in members)):
            self.scheduler.pause_at("phase_failed", "Retry phase: " + frontier.id)
            return
        # An explicit action is already bounded and finishes normally.
        if self.run.action_id:
            return
        for phase in self.cycle.phases:
            if phase.id == frontier.id:
                break
            receipt = self.run.phase_receipts.get(phase.id, {})
            bounded = phase.id == self.run_one and receipt.get("status") in (SUCCESS, SKIPPED)
            if not receipt.get("invalidated") and phase.id not in self.consumed and (
                    bounded or (receipt.get("status") == SUCCESS and phase.policy == "stop_after")):
                self.consumed.add(phase.id)
                self.scheduler.pause_at("phase_boundary", "Continue after " + (phase.title or phase.id))
                return
        if frontier.policy == "manual" and frontier.id not in self.allowed_manual:
            self.scheduler.pause_at("manual_phase", "Run phase: " + (frontier.title or frontier.id))

    def permits(self, step, execution=True):
        frontier = self.frontier()
        if frontier is not None and step.phase and step.phase != frontier.id:
            return False
        phase = next((phase for phase in self.cycle.phases if phase.id == step.phase), None)
        return (not execution or phase is None or phase.policy != "approval_required" or step.id == phase.approval
                or self.run.steps[phase.approval].status == SUCCESS)

    def continue_phase(self, phase_id="", one=False):
        frontier = self.frontier()
        if not frontier:
            return "There is no pending phase."
        if phase_id and phase_id != frontier.id:
            return "Only the next phase (%s) can run; completed boundaries cannot be skipped." % frontier.id
        if frontier.policy == "manual" and not one:
            return "This phase needs the explicit Run phase action."
        self.allowed_manual.add(frontier.id)
        self.run_one = frontier.id if one else ""
        self.run.execution_bound = {"phase": frontier.id, "action": "run_phase"} if one else {}
        return ""


class Control:
    """A pause request and node submission share the same linearization lock."""
    def __init__(self, wake):
        self.lock = threading.RLock()
        self.pending = []
        self.requested = False
        self.closed = False
        self.wake = wake

    def submit(self, command, phase=""):
        if command not in ("pause", "continue", "run_phase", "retry_phase", "set_mode") or not isinstance(phase, str):
            return "Unknown cycle control action."
        with self.lock:
            if self.closed:
                return "The run has ended."
            if len(self.pending) >= 32:
                return "Too many pending control actions."
            if command == "pause":
                self.requested = True
            self.pending.append((command, phase))
        self.wake()
        return ""

    def drain(self):
        with self.lock:
            pending, self.pending = self.pending, []
        return pending
