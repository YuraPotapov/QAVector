"""Reuse the graph's task budget when an implementation really starts again.

This deliberately recognizes only the existing direct counter prerequisite of
agent.implement. Cached provider answers remain free to replay. A new call
after such a replay reserves another task attempt under the memory lock; it
never mutates scheduler-owned run state from a worker.
"""

import copy
import os
import time
from pathlib import Path

from cycle import checkpoints, conditions, memory, model, variables
from domain.cycle import CANCELLED, FAILED, TIMEOUT


class AdmissionError(RuntimeError):
    pass


def contract(cycle, implementation):
    bumps = [step for step in cycle.steps if step.id in implementation.needs
             and step.plugin == "memory.remember" and step.settings.get("bump")]
    reads, gates = set(), set()
    for bump in bumps:
        matching, checks = _budget_nodes(cycle, bump)
        reads.update(matching)
        gates.update(checks)
    return bumps, reads, gates


def _budget_nodes(cycle, bump):
    from cycle.plugins.memory import _names
    ancestors = model.upstream(cycle, {bump.id})
    reads = {step.id for step in cycle.steps if step.id in ancestors and step.plugin == "memory.recall"
             and step.settings.get("key") == bump.settings.get("key")
             and step.settings.get("field") in _names(bump.settings.get("bump"))}
    gates = {step.id for step in cycle.steps if step.id in ancestors and step.plugin == "check.gate"
             and model.upstream(cycle, {step.id}) & reads}
    return reads, gates


def counter(cycle, name):
    return any(name in {step.id for step in contract(cycle, impl)[0]}
               for impl in cycle.steps if impl.plugin == "agent.implement")


def token(run, step):
    state = run.steps[step.id]
    return state.admission_reservation or checkpoints.digest([run.id, step.id, state.execution_generation])


def verifier(context, step):
    reads, gates = _budget_nodes(context.cycle, step)
    return lambda store: _check(context, reads, gates, store)


def _check(context, reads, gates, store):
    from cycle.plugins.check import _held
    cycle = context.cycle
    scope = copy.deepcopy(variables.scope(context.run, cycle, context.run.steps,
                            env=context.environment, secrets=getattr(context, "secrets", {})))
    for read in (one for one in cycle.steps if one.id in reads):
        settings = variables.resolve(read.settings, scope)
        record = store.get(str(settings.get("key") or "")) or {}
        fields = dict(record.get("fields") or {})
        field = str(settings.get("field") or "")
        scope["steps"][read.id]["outputs"] = {"fields": fields, "known": bool(record),
            "value": str(fields.get(field, settings.get("default", ""))),
            "updated_at": record.get("updated_at") or 0, "holder": ""}
    for gate in (one for one in cycle.steps if one.id in gates):
        if gate.disabled or not conditions.evaluate(gate.condition, scope):
            raise AdmissionError("Implementation admission %s does not apply; no new call was sent." % gate.id)
        settings = variables.resolve(gate.settings, scope)
        for label, expression in (settings.get("checks") or {}).items():
            passed, explanation = _held(str(expression))
            if not passed:
                raise AdmissionError("Implementation admission refused by %s (%s): %s. No new call was sent."
                                     % (gate.id, label, explanation))
        scope["steps"][gate.id]["outputs"]["passed"] = True
    return scope


def saved_answers(workspace, name):
    for path in Path(workspace, "steps", name, "operations").glob("*/receipt.json"):
        receipt = checkpoints._read(str(path))
        if receipt.get("state") == "complete" and receipt.get("ok") is True:
            return True
    return False


def refresh_nodes(cycle, run, pending, workspace):
    """The same budget prerequisites for explicit Retry and failed Resume.

    An implementation that never started still owns its earlier reservation.
    Successful service receipts must be replayed before deciding whether a new
    paid attempt is needed, so those executions use Guard below instead.
    """
    refreshed = set()
    for implementation in cycle.steps:
        previous = run.steps[implementation.id]
        if (implementation.id not in pending or implementation.plugin != "agent.implement"
                or previous.status not in (FAILED, TIMEOUT, CANCELLED) or not previous.attempts
                or saved_answers(workspace, implementation.id)):
            continue
        bumps, reads, gates = contract(cycle, implementation)
        refreshed.update(step.id for step in bumps)
        refreshed.update(reads | gates)
    return refreshed


class Guard:
    def __init__(self, context):
        self.context = context
        self.granted = set()
        self.deferred = {step.id for step in context.cycle.steps if step.plugin == "agent.implement"
            and context.earlier.get(step.id) and context.earlier[step.id][1].attempts
            and saved_answers(context.workspace, step.id)}

    def before_call(self, step, operation):
        if step.id not in self.deferred or step.id in self.granted:
            return
        bumps, reads, gates = contract(self.context.cycle, step)
        if not bumps:
            return
        run = self.context.run
        # The request and its previous settled receipt identify the next paid
        # attempt. Unlike resume_count, this survives a crash after reserving
        # but before Operation.begin has written its started receipt.
        reservation = checkpoints.digest([run.id, step.id, os.path.relpath(operation.path, self.context.workspace),
                                          (operation.record or {}).get("epoch")])
        try:
            self._reserve(step, bumps, reads, gates, reservation)
            self.granted.add(step.id)
        except memory.MemoryError_ as exc:
            raise checkpoints.CheckpointError("Cannot save execution state: implementation reservation: %s" % exc) from exc

    def _reserve(self, implementation, bumps, reads, gates, reservation):
        from cycle.plugins.memory import _names
        context, cycle = self.context, self.context.cycle
        # The lock covers both fresh admission checks and the counter update.
        with memory._change(context.memory_path) as store:
            scope = variables.scope(context.run, cycle, context.run.steps,
                                    env=context.environment, secrets=context.secrets)
            resolved_bumps = [(bump, variables.resolve(bump.settings, scope)) for bump in bumps]
            records = [(bump, settings, store.get(str(settings.get("key") or "")) or {})
                       for bump, settings in resolved_bumps]
            already = [checkpoints.digest([reservation, bump.id]) in record.get("reservations", {})
                       for bump, _settings, record in records]
            if all(already):
                return
            if any(already):
                raise AdmissionError("The implementation budget reservation is incomplete; inspect task memory.")
            # The original checks are the admission policy; no cap is guessed
            # from a counter's name or an agent's internal attempt budget.
            _check(context, reads, gates, store)
            for bump, settings, _record in records:
                key = str(settings.get("key") or "")
                if not key:
                    raise AdmissionError("The implementation budget has no task key.")
                memory._remember_once(store, key, settings.get("fields") or {},
                                      _names(settings.get("bump")), checkpoints.digest([reservation, bump.id]))
        checkpoints.Store(context.workspace).write(os.path.join(context.workspace, ".execution", "admissions",
            implementation.id, reservation + ".json"), {"step": implementation.id, "run_id": context.run.id,
                "reservation": reservation, "at": time.time(), "status": "reserved"})
