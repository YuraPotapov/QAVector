"""Execution records are a prerequisite for work, not a best-effort observer.

A resume keeps one run identity and its workspace. Finished results are never
turned into success just to satisfy an edge. Unknown external outcomes require
reconciliation; they must not silently cause another billable request.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
from contextlib import contextmanager
from dataclasses import asdict

from cycle import model, run as records, variables
from domain.cycle import (CANCELLED, FAILED, PENDING, RUNNING, SKIPPED, SUCCESS,
                          TIMEOUT, WAITING, StepRun)


class CheckpointError(RuntimeError):
    pass


@contextmanager
def lock(workspace):
    """One executor owns a run, including across processes and GUI restarts."""
    os.makedirs(workspace, exist_ok=True)
    with open(os.path.join(workspace, ".execution.lock"), "a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.write(b"\0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CheckpointError("This run is already being executed.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_digest(path):
    digest_ = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest_.update(block)
    return digest_.hexdigest()


def signatures(cycle, overrides, registry):
    values = dict(cycle.variables, **(overrides or {}))
    result = {}
    for step in cycle.steps:
        definition = asdict(step)
        for key in ("label", "source_index"):
            definition.pop(key, None)
        refs = variables.REFERENCE.findall(json.dumps(definition, default=str))
        used = {ref.split(".")[1] for ref in refs if ref.startswith("vars.")}
        plugin = registry.get(step.plugin)
        result[step.id] = digest({
            "step": definition,
            "variables": {name: values.get(name) for name in used
                          if name not in cycle.secrets},
            "version": getattr(getattr(plugin, "metadata", None), "version", ""),
        })
    return result


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise CheckpointError("Cannot read execution record %s: %s" % (path, exc)) from exc


class Store:
    def __init__(self, workspace):
        self.workspace = os.path.abspath(workspace)
        self.directory = os.path.join(self.workspace, ".execution")
        self.refreshed = set()
        #: Why each step a resume runs again does - see :func:`_reasons`.
        self.reasons = {}
        self.definitions = {}
        self.earlier = {}

    def write(self, path, data):
        try:
            return records._atomic(path, data)
        except (OSError, ValueError, TypeError) as exc:
            raise CheckpointError("Cannot save execution state: %s" % exc) from exc

    def begin(self, run, cycle, registry):
        self.definitions = signatures(cycle, run.variables, registry)
        self.write(os.path.join(self.directory, "manifest.json"), {
            "schema": 1, "cycle": cycle.id,
            "steps": self.definitions})
        self.write(os.path.join(self.workspace, "graph.json"), model.to_graph(cycle))
        self.save(run)
        self.finish_transition()

    def save(self, run):
        self.write(records.metadata_path(self.workspace), records.to_document(run))

    def step(self, run, step_run):
        if step_run.status == SUCCESS:
            try:
                step_run.artifact_digests = {
                    item.path: file_digest(os.path.join(self.workspace, item.path))
                    for item in step_run.artifacts}
            except OSError as exc:
                raise CheckpointError("Cannot save execution state: missing result artifact: %s" % exc) from exc
        # The individual receipt survives even if updating the summary fails.
        self.write(os.path.join(self.directory, "steps", step_run.step_id + ".json"),
                   asdict(step_run))
        self.save(run)

    def publish(self, run, changed):
        """Journal a multi-node reset before materializing its individual files."""
        transition = {"run": records.to_document(run), "changed": sorted(changed)}
        self.write(os.path.join(self.directory, "transition.json"), transition)
        self._materialize(transition)

    def _materialize(self, transition):
        document = transition["run"]
        for name in transition["changed"]:
            self.write(os.path.join(self.directory, "steps", name + ".json"),
                       document["steps"][name])
        self.write(records.metadata_path(self.workspace), document)

    def finish_transition(self):
        path = os.path.join(self.directory, "transition.json")
        if not os.path.exists(path):
            return
        try:
            os.unlink(path)
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as exc:
            raise CheckpointError("Cannot save execution state: %s" % exc) from exc

    def load(self):
        path = os.path.join(self.directory, "transition.json")
        if os.path.exists(path):
            # No affected worker may start until this transition is finished.
            # A crash at any intermediate write is replayed without paid work.
            self._materialize(_read(path))
            self.finish_transition()
        run = records.from_document(_read(records.metadata_path(self.workspace)))
        if run is None:
            raise CheckpointError("The run has no readable execution record.")
        run.workspace = self.workspace
        for step_id in run.steps:
            path = os.path.join(self.directory, "steps", step_id + ".json")
            if os.path.exists(path):
                raw = _read(path)
                restored = records.from_document({"id": run.id, "cycle_id": run.cycle_id,
                                                  "steps": {step_id: raw}})
                run.steps[step_id] = restored.steps[step_id]
        return run

    def resume(self, cycle, registry):
        from cycle.registry import ManagedPlugin

        previous = self.load()
        if previous.cycle_id != cycle.id:
            raise CheckpointError("This run belongs to another cycle.")
        manifest_path = os.path.join(self.directory, "manifest.json")
        if not os.path.exists(manifest_path):
            raise CheckpointError(
                "This run predates Resume: its original step settings were not recorded. "
                "Use Run from here to reuse its earlier results explicitly.")
        manifest = _read(manifest_path)
        current = signatures(cycle, previous.variables, registry)
        if set(current) != set(previous.steps):
            raise CheckpointError("The cycle's steps changed. Resume requires the same graph.")
        uncertain = []
        for step in previous.steps.values():
            receipts = list(Path(self.workspace, "steps", step.step_id,
                                 "operations").glob("*/receipt.json"))
            # A caught exception can leave the node marked failed while the
            # remote call remains unresolved. Check all requests, even if the
            # user edited this node and its next request would have a new key.
            complete = all(_read(str(path)).get("state") == "complete" for path in receipts)
            if not complete:
                uncertain.append(step.step_id)
                continue
            if step.status != RUNNING and "still running when the run ended" not in step.message:
                continue
            if step.plugin == "approval.gate":
                continue  # an interrupted human question is asked again
            plugin = registry.get(step.plugin)
            recoverable = (getattr(plugin.metadata, "recoverable", False) and receipts
                           and "still running when the run ended" not in previous.message
                           and complete)
            if not recoverable:
                uncertain.append(step.step_id)
        if uncertain:
            raise CheckpointError(
                "Uncertain outcome for %s. Check the saved logs and external service "
                "before explicitly retrying; Resume will not repeat this work."
                % ", ".join(uncertain))

        # Business refusals are results too; retrying a failed agent is different
        # from trying to force a budget/approval gate to say yes.
        decisions = {s.step_id for s in previous.steps.values()
                     if s.status == FAILED and "passed" in s.outputs}
        retry = {s.step_id for s in previous.steps.values()
                 if s.status in (FAILED, TIMEOUT, CANCELLED, PENDING, WAITING, RUNNING)
                 and s.step_id not in decisions}
        if not retry:
            raise CheckpointError("There is no failed or interrupted work to resume.")
        pending = model.downstream(cycle, retry)
        pending.update(s.step_id for s in previous.steps.values() if s.status == SKIPPED)
        # Refresh process-local handles and approval, without replaying successful
        # paid descendants. Gates directly using those fresh answers run again.
        fresh = {s.id for s in cycle.steps
                 if not getattr(registry.get(s.plugin).metadata, "reusable", True)
                 or isinstance(registry.get(s.plugin), ManagedPlugin)}
        pending.update(fresh)
        asked = set(fresh)

        self.refreshed = fresh
        while True:
            gates = {s.id for s in cycle.steps if s.plugin == "check.gate"
                     and set(s.needs) & fresh}
            if gates <= fresh:
                break
            fresh.update(gates)
        pending.update(fresh)

        kept = set(previous.steps) - pending
        self.reasons = _reasons(cycle, previous, retry, fresh, asked, pending)
        # A result imported from a run that predates fingerprints has none of
        # its own, and none is invented for it. It is held to this run's
        # manifest instead: the partial run that imported it accepted it under
        # exactly those settings, so resuming that run with them unchanged
        # trusts it no further than the run already did. Refusing here used to
        # send people to Run from here, which checks less, and left every run
        # descended from an old one unresumable for good.
        changed = [name for name in kept
                   if (previous.steps[name].definition_digest or manifest["steps"].get(name))
                   != current[name]]
        if changed:
            raise CheckpointError(
                "Completed steps changed: %s. Their paid results were preserved; "
                "start a new run or restore their original settings." % ", ".join(sorted(changed)))
        for name in kept:
            for artifact in previous.steps[name].artifacts:
                if not os.path.isfile(os.path.join(self.workspace, artifact.path)):
                    raise CheckpointError("Saved artifact is missing: %s (step %s). "
                                          "No paid work was restarted." % (artifact.path, name))
                expected = previous.steps[name].artifact_digests.get(artifact.path)
                if expected and file_digest(os.path.join(self.workspace, artifact.path)) != expected:
                    raise CheckpointError("Saved artifact changed: %s (step %s). "
                                          "No paid work was restarted." % (artifact.path, name))

        self.write(os.path.join(self.directory, "history",
                                "%04d.json" % previous.resume_count),
                   {"run": records.to_document(previous), "manifest": manifest})
        run = copy.deepcopy(previous)
        run.resume_count += 1
        run.status, run.message, run.ended_at = RUNNING, "", None
        #: What the steps about to run again did last time, before it is
        #: cleared below - see RunContext.earlier.
        self.earlier = {name: (previous.id, copy.deepcopy(previous.steps[name]))
                        for name in pending}
        for name in pending:
            step = cycle.step(name)
            was = run.steps[name]
            run.steps[name] = StepRun(name, plugin=step.plugin,
                                      attempts=was.attempts)
        self.publish(run, pending)
        return run, sorted(kept)


#: Why a step that ended this way runs again, in the words a person reads.
_AGAIN = {FAILED: "failed last time", TIMEOUT: "timed out last time",
          CANCELLED: "was stopped last time", RUNNING: "was interrupted",
          WAITING: "was still waiting", PENDING: "never got its turn"}


def _reasons(cycle, previous, retry, fresh, asked, pending):
    """``{step: why it runs again}`` for every step a resume will run.

    The same decisions :meth:`Store.resume` makes, said once per step and in
    order of what matters most: a step's own failure is the reason even when it
    also sits below another one. A step that was only stopped or never started
    because something above it broke is described by that, not by its own
    status - "was stopped" would send somebody looking at the wrong step.
    """
    broken = {name for name in retry
              if previous.steps[name].status not in (CANCELLED, PENDING)}
    below = model.downstream(cycle, broken) - broken
    reasons = {}
    for name in pending:
        step = previous.steps.get(name)
        if name in retry and name not in below:
            reasons[name] = _AGAIN.get(step.status, "did not finish")
        elif name in asked:
            reasons[name] = ("asks again on every run"
                             if step.plugin == "approval.gate"
                             else "holds something only for the run it is in")
        elif name in fresh:
            reasons[name] = "checks an answer that is asked again"
        elif step is not None and step.status == SKIPPED:
            reasons[name] = "was skipped last time"
        else:
            reasons[name] = "waits on a step that runs again"
    return reasons
