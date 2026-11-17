"""The Execution Engine (ADR-002 D4-D8): one way to invoke, watch and cancel.

Whoever asks - the GUI user, a cycle run, later an agent - goes through
:meth:`Engine.invoke`, and the same things happen in the same order:

1. the capability is resolved with the caller's grants and the request checked
   against its input schema (:meth:`Registry.invocable`);
2. everything else the action would touch is checked too - the invoker's
   ``requirements`` (a service's dependency closure, D5) - before any effect;
3. the execution is **written** (``queued``), atomically with "nothing else is
   working on this resource" (D6) - and when it cannot be written nothing runs;
4. only then is the trusted invoker asked to ``start`` it.

The invoker reports through the :class:`Job` it was handed, from whatever
thread it likes: ``dispatched`` (the owner took it - not success), ``running``,
``progress``, and one of ``succeed`` / ``fail``. The engine owns the deadline
(``timed_out``) and ``cancel`` - which ends the execution and tells the invoker
to stop *waiting*; it is never a stop of the resource (D7). A report after the
end is ignored, so a late owner cannot rewrite an outcome.

Who may look: the actor that asked, or a caller granted ``executions.read``
(``executions.cancel`` to cancel) in the project. Anything else is
``not_found``, so an execution's existence is not disclosed.

Blocking calls (``wait``, ``subscribe`` with a timeout) are for worker threads
of the host, never a UI thread. Nothing here is retried.
"""

import copy
import threading
import time

from .contracts import ResourceRef, text
from .errors import QCRError
from .executions import TERMINAL, ExecutionError

#: The ``reason`` a cancel may carry, at most.
REASON_CHARS = 500


def _timer(seconds, work):
    timer = threading.Timer(seconds, work)
    timer.daemon = True
    timer.start()
    return timer


class Job:
    """One execution as its invoker sees it. Every report returns False once the
    execution has ended (finished, timed out or canceled) and changes nothing."""

    def __init__(self, engine, execution, capability, request):
        self._engine = engine
        self.execution_id = execution["execution_id"]
        self.project_id = execution["project_id"]
        self.actor_id = execution["actor_id"]
        self.capability = capability
        self.resource = capability.resource
        self.request = request

    @property
    def finished(self):
        return not self._engine._active(self.execution_id)

    def dispatched(self, payload=None):
        return self._engine._step(self, "dispatched", payload=payload)

    def running(self, payload=None):
        return self._engine._step(self, "running", payload=payload)

    def progress(self, kind, payload=None):
        return self._engine._progress(self, kind, payload)

    def succeed(self, output):
        return self._engine._succeed(self, output)

    def fail(self, code, message=None, output=None):
        return self._engine._step(self, "failed", output=output,
                                  error={"code": code, "message": message or code})


class Engine:
    def __init__(self, registry, store, *, schedule=_timer, clock=time.monotonic, on_change=None):
        """``on_change(execution_id)`` is told after every change an execution
        goes through (a host pushes it on); it must not call back into the engine
        and its failures are ignored."""
        self._registry, self._store = registry, store
        self._schedule, self._clock = schedule, clock
        self._on_change = on_change
        self._cond = threading.Condition(threading.RLock())
        self._jobs = {}        # execution id -> [job, invoker, timer]

    def recover(self, observe=None):
        """At start-up, before any invoke: unfinished executions of a previous
        life become ``unknown`` (D8). Returns their ids."""
        return self._store.reconcile(observe)

    def close(self):
        """Stop the deadlines. Unfinished executions stay as they are and are
        reconciled the next time the store is opened."""
        with self._cond:
            jobs, self._jobs = list(self._jobs.values()), {}
            self._cond.notify_all()
        for _, _, timer in jobs:
            if timer is not None:
                timer.cancel()

    # -- invoking ---------------------------------------------------------------
    def invoke(self, caller, project_id, capability_id, request, *, version=None, idempotency_key=None,
               parent_id=None, correlation_id=None):
        """Record and start one execution; return it (usually not finished yet).

        The same ``idempotency_key`` with the same request returns the execution
        already made and starts nothing.
        """
        cap, invoker = self._registry.invocable(caller, project_id, capability_id, request, version=version)
        if idempotency_key is not None and not cap.supports_idempotency:
            raise QCRError("invalid_input", "Capability does not take an idempotency key.")
        try:
            needs = list(invoker.requirements(cap, copy.deepcopy(request)) or ())
        except QCRError:
            raise
        except Exception:
            raise QCRError("owner_unavailable", "The resource owner cannot check this action.") from None
        for ref, permission in needs:
            if not isinstance(ref, ResourceRef) or ref.project_id != project_id:
                raise QCRError("authorization_denied", "A resource this action needs is not permitted.")
            if not self._registry.allows(caller, ref, permission):
                raise QCRError("authorization_denied", "A resource this action needs is not permitted.")
        execution, created = self._store.create(
            project_id, cap.capability_id, cap.version, caller.actor_id, request,
            resource_id=cap.resource.resource_id, idempotency_key=idempotency_key, parent_id=parent_id,
            correlation_id=correlation_id, exclusive=cap.exclusive)
        if not created:
            return execution
        job = Job(self, execution, cap, copy.deepcopy(request))
        seconds = cap.timeout_seconds
        asked = getattr(invoker, "timeout", None)
        if callable(asked):
            try:
                wanted = asked(cap, copy.deepcopy(request))
            except Exception:
                wanted = None
            if type(wanted) in (int, float) and wanted > 0:
                seconds = min(seconds, wanted) if seconds else wanted
        with self._cond:
            slot = [job, invoker, None]
            self._jobs[job.execution_id] = slot
            if seconds:
                slot[2] = self._schedule(seconds, lambda: self._deadline(job))
        self._changed(job.execution_id)
        try:
            invoker.start(job)
        except Exception:
            job.fail("dispatch_failed", "The owner could not take the job.")
        return self._store.get(job.execution_id)

    # -- reports from the invoker ------------------------------------------------
    def _active(self, execution_id):
        with self._cond:
            return execution_id in self._jobs

    def _step(self, job, state, *, payload=None, output=None, error=None):
        with self._cond:
            if job.execution_id not in self._jobs:
                return False
            try:
                current = self._store.get(job.execution_id)["state"]
                if current == "queued" and state in ("running", "succeeded"):
                    # An owner that answers at once still went through "took it".
                    self._store.transition(job.execution_id, "dispatched")
                self._store.transition(job.execution_id, state, payload=payload, output=output, error=error)
            except ExecutionError as exc:
                if exc.code in ("too_large", "invalid_input") and state in TERMINAL:
                    self._store.transition(job.execution_id, "failed", error={
                        "code": "invalid_output", "message": "The owner reported an unusable result."})
                    state = "failed"
                elif exc.code in ("invalid_transition", "too_large", "invalid_input"):
                    return False
                else:
                    raise
            if state in TERMINAL:
                self._end(job.execution_id)
            self._cond.notify_all()
            self._changed(job.execution_id)
            return True

    def _succeed(self, job, output):
        try:
            self._registry.validate_output(job.capability, output)
        except QCRError:
            return self._step(job, "failed", error={"code": "invalid_output",
                                                    "message": "The owner reported an unusable result."})
        return self._step(job, "succeeded", output=output)

    def _progress(self, job, kind, payload):
        with self._cond:
            if job.execution_id not in self._jobs:
                return False
            try:
                kept = self._store.progress(job.execution_id, kind, payload)
            except ExecutionError as exc:
                if exc.code in ("too_large", "invalid_input", "terminal"):
                    return False
                raise
            self._cond.notify_all()
            if kept:
                self._changed(job.execution_id)
            return kept

    def _end(self, execution_id):
        slot = self._jobs.pop(execution_id, None)
        if slot is not None and slot[2] is not None:
            slot[2].cancel()
        return slot

    def _deadline(self, job):
        with self._cond:
            if job.execution_id not in self._jobs:
                return
            self._store.transition(job.execution_id, "timed_out", error={
                "code": "timed_out", "message": "The execution did not finish in time."})
            slot = self._end(job.execution_id)
            self._cond.notify_all()
            self._changed(job.execution_id)
        self._stop_waiting(slot)

    def _changed(self, execution_id):
        if self._on_change is not None:
            try:
                self._on_change(execution_id)
            except Exception:
                pass

    @staticmethod
    def _stop_waiting(slot):
        if slot is None:
            return
        try:
            slot[1].cancel(slot[0])
        except Exception:
            pass        # the execution has ended either way; the owner's trouble is its own

    # -- looking and cancelling --------------------------------------------------
    def _visible(self, caller, execution_id, permission):
        try:
            execution = self._store.get(execution_id)
        except ExecutionError as exc:
            if exc.code == "not_found":
                raise QCRError("not_found", "Execution is not available.") from None
            raise
        ref = ResourceRef(execution["project_id"], execution["resource_id"] or "-", "execution")
        if caller.actor_id != execution["actor_id"] and not self._registry.allows(caller, ref, permission):
            raise QCRError("not_found", "Execution is not available.")
        self._registry.allows(caller, ref, permission)       # a revoked caller is refused even as actor
        return execution

    def get(self, caller, execution_id):
        return self._visible(caller, execution_id, "executions.read")

    def wait(self, caller, execution_id, timeout=None):
        """Block until the execution ends or ``timeout`` seconds pass; return it as it is then."""
        execution = self._visible(caller, execution_id, "executions.read")
        end = None if timeout is None else self._clock() + max(0.0, float(timeout))
        with self._cond:
            while execution["state"] not in TERMINAL:
                left = None if end is None else end - self._clock()
                if left is not None and left <= 0:
                    break
                self._cond.wait(left)
                execution = self._store.get(execution_id)
        return execution

    def subscribe(self, caller, execution_id, after=0, *, timeout=0, limit=200):
        """Events after ``after`` (a cursor); with a timeout, wait for some to come.

        Returns ``{"events", "cursor", "state"}`` - the next call passes ``cursor``.
        """
        execution = self._visible(caller, execution_id, "executions.read")
        end = self._clock() + max(0.0, float(timeout))
        with self._cond:
            while True:
                events = self._store.events(execution_id, after, limit)
                state = self._store.get(execution_id)["state"]
                left = end - self._clock()
                if events or state in TERMINAL or left <= 0:
                    break
                self._cond.wait(left)
        return {"events": events, "cursor": events[-1]["cursor"] if events else after, "state": state}

    def cancel(self, caller, execution_id, reason=None):
        """End an unfinished execution as ``canceled`` and tell its invoker to stop
        waiting. The resource is not stopped (D7). An ended one is returned as is."""
        execution = self._visible(caller, execution_id, "executions.cancel")
        if reason is not None:
            text(reason[:REASON_CHARS] if isinstance(reason, str) else reason, "reason")
        with self._cond:
            execution = self._store.get(execution_id)
            if execution["state"] in TERMINAL:
                return execution
            slot = self._jobs.get(execution_id)
            if slot is not None and not slot[0].capability.supports_cancel:
                raise QCRError("not_cancelable", "This execution cannot be canceled.")
            payload = {"by": caller.actor_id}
            if reason:
                payload["reason"] = reason[:REASON_CHARS]
            self._store.transition(execution_id, "canceled", payload=payload,
                                   error={"code": "canceled", "message": "The execution was canceled."})
            slot = self._end(execution_id)
            self._cond.notify_all()
            self._changed(execution_id)
        self._stop_waiting(slot)
        return self._store.get(execution_id)

    def list(self, caller, project_id, **filters):
        """Executions in a project, newest first. Without ``executions.read`` in
        the project a caller sees only its own."""
        text(project_id, "project id")
        if not self._registry.allows(caller, ResourceRef(project_id, "-", "execution"), "executions.read"):
            filters["actor_id"] = caller.actor_id
        return self._store.list(project_id, **filters)
