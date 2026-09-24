"""Running a cycle: what may start, when, and what to do when something fails.

**Threads, not asyncio.** There is no event loop anywhere in this application,
Playwright's sync API is bound to the greenlet of its creating thread, and
``engine.services.request`` blocks on a ``threading.Event``. See
``cycle/registry.py`` for the full reasoning; the short version is that async
here would buy nothing and cost a rewrite of the engine.

**A scheduler and a pool, not a wave of layers.** Work becomes ready as results
land, not in tidy generations: in ``a -> b -> d`` and ``a -> c``, ``b`` should
start the moment ``a`` finishes rather than waiting for ``c``. So one loop owns
the state, hands ready steps to a pool, and blocks on a queue of results:

    submit everything ready
    while anything is in flight:
        take a result off the queue (or wake at the nearest deadline)
        record it, resolve what it unblocked, submit whatever is ready now

Only that loop touches the run record, so the record needs no lock and the order
of a run's history is the order things actually finished.

**A step may say "not yet" instead of finishing.** A plugin that returns
``registry.waiting(seconds)`` keeps its place in the graph and gives its worker
back, and the scheduler comes to it again when the time is up. That is the one
thing a plugin can say about scheduling, and it is something only the plugin
knows - *the deploy said ninety seconds*, *the queue is empty, ask again at
nine*. Everything downstream stays blocked, which is what ``needs`` already
means, said about time instead of about a dependency.

The step's ``timeout:`` bounds its **whole life**, waits included, rather than
each turn of it. That is both the honest reading of a deadline written on a
step and the only bound on a plugin that would otherwise ask to wait for ever.

**Stopping is cooperative, and that is a property, not an oversight.** Python
cannot kill a thread. A timeout sets the step's cancel token; a plugin that
waits on its token or passes the remaining time down to whatever it waits on
stops promptly, and every built-in one does. A plugin that ignores it runs to
completion, the run says so rather than hanging in silence, and the pool is
given a grace period at the end rather than being joined forever. That is the
same honesty ``engine/runner.py`` practises, where a stop bites between steps
and is bounded by the current step's own timeout.

**Cleanup is guaranteed.** One ``try/finally`` around the whole loop: stop what
is held, in reverse order; call ``cleanup`` for every step that reached
``prepare``; then report. Every one of those is wrapped the way
``engine/artifacts.py`` wraps diagnostics, so a failure while tidying up is
logged and never masks the result the run actually came to.
"""

import copy
import logging
import os
import queue
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from cycle import ask, checkpoints, conditions, model, registry as default_registry, variables
from cycle.bus import NullObserver
from cycle.context import CancelToken, Cancelled, RunContext
from cycle import revisions, sessions
from domain.cycle import (CANCELLED, CONTINUE, FAILED, MANUAL, PENDING, RUNNING,
                          SKIPPED, SUCCESS, TERMINAL, TIMEOUT, WAITING, CycleRun,
                          StepRun)

log = logging.getLogger("cycle.executor")

#: How many steps may run at once when nobody said. Deliberately not the window
#: governor ``engine/runner.py`` uses for Chrome: that rations windows against
#: free memory, and a cycle step is usually a command or a request to the GUI.
#: The step that does open windows is ``scenario.run``, and its own ``jobs``
#: setting is where that belongs.
DEFAULT_JOBS = 4

#: How long the scheduler is willing to sit on the queue with no deadline
#: pending. Only a ceiling on how stale a cancelled run's view can get; results
#: wake it immediately.
IDLE_TICK = 0.5

#: The answers a step past its deadline is asked for. The first keeps it going
#: for another timeout; anything else, including no answer, stops it.
OVERDUE_OPTIONS = ("Continue", "Cancel")

#: How long the pool is given to drain after the run is over, before the run
#: reports what is still going rather than waiting for it.
DRAIN_GRACE = 10.0

#: Runs that can still be stopped by id, so a control channel can reach one
#: without the launcher having to hold a reference. Mirrors
#: ``engine.runner.request_session_stop``.
_live = {}
_live_lock = threading.Lock()


def request_stop(run_id=None, reason="the run was stopped"):
    """Ask a run to stop. ``None`` stops every run in this process.

    Called from the launcher's control thread when the GUI sends
    ``cycle.stop``, and from the signal handler on Ctrl+C. Returns how many
    runs were asked, so a caller can say "no such run" rather than going quiet.
    """
    with _live_lock:
        tokens = ([token for key, token in _live.items()
                   if run_id in (None, key)])
    for token in tokens:
        token.set(reason)
    return len(tokens)


def run_cycle(cycle, workspace, variables_=None, observer=None, cancel=None,
              jobs=None, registry=None, flows_dir=None, cycles_dir=None,
              trigger=MANUAL, run_id=None, environment=None, secrets=None,
              memory_path="", only=None, reuse=None, resume=False):
    with checkpoints.lock(workspace):
        return _run_cycle(cycle, workspace, variables_, observer, cancel, jobs,
                          registry, flows_dir, cycles_dir, trigger, run_id,
                          environment, secrets, memory_path, only, reuse, resume)


def _run_cycle(cycle, workspace, variables_=None, observer=None, cancel=None,
               jobs=None, registry=None, flows_dir=None, cycles_dir=None,
               trigger=MANUAL, run_id=None, environment=None, secrets=None,
               memory_path="", only=None, reuse=None, resume=False):
    """Run every step of ``cycle`` that should run. Returns a :class:`CycleRun`.

    Never raises for anything the run did - a step that fails, a plugin that
    throws, a cancelled run and a cycle whose plugins are missing all come back
    as a record with a status. The only exceptions that escape are the ones that
    mean this process is in trouble.

    ``only`` is the step ids to actually run, for working on one part of a
    cycle without paying for the rest of it. Every other step is **taken from**
    ``reuse`` - an earlier run's record - rather than run again: its outputs go
    into scope, so a step that reads ``${steps.plan.outputs.summary}`` still
    finds one, and it counts as done, so ``needs`` is satisfied. Nothing else
    in the scheduler knows this happened; a step that is already finished is a
    step it never looks at.
    """
    registry = registry or default_registry
    observer = observer or NullObserver()
    cancel = cancel or CancelToken()
    jobs = max(1, int(jobs or DEFAULT_JOBS))

    store = checkpoints.Store(workspace)
    if resume:
        if only or reuse or variables_:
            raise checkpoints.CheckpointError("Resume cannot be combined with selection or variable overrides.")
        run, kept = store.resume(cycle, registry)
        borrowed = [run.steps[name] for name in kept]
    else:
        if os.path.exists(os.path.join(workspace, "metadata.json")):
            raise checkpoints.CheckpointError(
                "This execution already exists. Resume it or use a new run directory.")
        run = CycleRun(id=run_id or os.path.basename(workspace),
                   cycle_id=cycle.id, cycle_name=cycle.title, trigger=trigger,
                   status=RUNNING, started_at=time.time(), workspace=workspace,
                   variables=dict(variables_ or {}),
                   steps={step.id: StepRun(step.id, plugin=step.plugin)
                          for step in cycle.steps})
        borrowed = _borrow(run, cycle, only, reuse, registry)

    store.begin(run, cycle, registry)
    prior_duration = run.duration_ms if resume else 0.0

    context = RunContext(run, cycle, workspace, cancel=cancel,
                         observer=observer, flows_dir=flows_dir,
                         cycles_dir=cycles_dir, environment=environment,
                         memory_path=memory_path)

    with _live_lock:
        _live[run.id] = cancel
    started = time.monotonic()
    # Read once, at the start, rather than per reference: decrypting on every
    # ${vars.x} would open the file dozens of times in a run, and a secret that
    # changed halfway through a run would make its steps disagree.
    context.secrets = dict(secrets or {})
    context.checkpoints = store
    context.restored = {one.step_id for one in borrowed} if resume else set()
    if resume:
        context.earlier = dict(store.earlier)
    elif reuse is not None:
        lent = {one.step_id for one in borrowed}
        context.earlier = {name: (reuse.id, step_run)
                           for name, step_run in (reuse.steps or {}).items()
                           if name not in lent}
    if resume and set(model.subject_sources(cycle)) - context.restored:
        # The step that found the subject runs again, and may find another -
        # a queue read twice need not give the same task. Until it has, this
        # run is not about what the last attempt was about.
        run.subject = {}
    scheduler = _Scheduler(cycle, run, context, observer, registry, jobs)
    try:
        observer.run_start(run, model.to_graph(cycle), jobs)
        for step_run in borrowed:
            # Announced, so the canvas and the record show them as what they
            # are: already answered, and by which run. A borrowed step that
            # said nothing would look like one this run had not reached.
            observer.step_end(step_run)
        observer.run_mode(run, _mode(cycle, run, store, resume, borrowed, only,
                                     reuse))
        if run.subject:
            observer.subject(run)
        else:
            scheduler.learn_subject()
        scheduler.drive()
    except checkpoints.CheckpointError as exc:
        cancel.set(str(exc))
        run.message = str(exc)
    finally:
        _cleanup(context, cycle, observer)
        with _live_lock:
            _live.pop(run.id, None)
        run.ended_at = time.time()
        run.duration_ms = prior_duration + (time.monotonic() - started) * 1000.0
        run.status = _verdict(run)
        if run.message.startswith("Cannot save execution state"):
            run.status = FAILED
        if not run.message and run.status != SUCCESS:
            run.message = scheduler.why()
        # An execution receipt is mandatory; observers remain best effort.
        try:
            store.save(run)
        except checkpoints.CheckpointError as exc:
            run.status, run.message = FAILED, str(exc)
        observer.run_end(run)
    return run


def _mode(cycle, run, store, resume, borrowed, only, reuse):
    """How this run begins, for :meth:`NullObserver.run_mode`.

    Everything here was already decided - by :meth:`Store.resume`, or by
    :func:`_borrow` - and this only says it, step by step and in file order,
    so a reader can see why a run starts where it does rather than infer it
    from which nodes turned green first.
    """
    in_order = [step.id for step in
                sorted(cycle.steps, key=lambda one: one.source_index)]
    if resume:
        kept = [name for name in in_order if name in {one.step_id for one in borrowed}]
        return {"mode": "resume", "resume_count": run.resume_count,
                "source_run": run.id, "kept": kept,
                "rerun": [{"step": name, "reason": store.reasons.get(name, "")}
                          for name in in_order if name in store.reasons]}
    if only:
        lent = {one.step_id for one in borrowed if one.source_run}
        left = {one.step_id for one in borrowed} - lent
        wanted = set(only)
        return {"mode": "partial", "resume_count": 0,
                "source_run": getattr(reuse, "id", "") or "",
                "kept": [name for name in in_order if name in lent],
                "rerun": [{"step": name,
                           "reason": ("chosen to run" if name in wanted
                                      else "cannot be taken from an earlier run")}
                          for name in in_order
                          if name not in lent and name not in left]}
    return {"mode": "fresh", "resume_count": 0, "source_run": "", "kept": [],
            "rerun": []}


#: The statuses a step has something to lend. A step that *ran* produced
#: outputs, whatever it then came to; one that was skipped or cancelled never
#: got as far as producing anything, and its record is a note that it did not
#: happen rather than a result.
#:
#: Borrowing those too is how ``approve`` came to read ``success (reused from
#: <run> (skipped))`` with empty outputs - a step that had never run, recorded
#: as done, in run after run, each one inheriting the last one's phantom.
LENDABLE = (SUCCESS, FAILED, TIMEOUT)


def _must_run(cycle, registry):
    """Steps a partial run has to do itself, however little of the cycle it wants.

    A plugin may say its result belongs to the run that produced it - see
    ``registry.PluginMetadata.reusable``, which ``approval.gate`` sets. That
    alone is not enough: **everything downstream of such a step is stale too**.
    A gate that reads ``${steps.approve.outputs.approved}`` and passed an hour
    ago passed on an hour-old answer, so borrowing *it* puts the old approval
    back into the run through the side door - which is exactly what a gate
    asked again in this run is there to prevent.

    So the answer is the closure: the steps that cannot be inherited, and
    everything that could only have run after them.

    An unknown plugin is reusable. The run fails on the missing plugin a moment
    later, and refusing to borrow first would only change which error is said.
    """
    fresh = [step.id for step in cycle.steps
             if not getattr(getattr(registry.get(step.plugin), "metadata", None),
                            "reusable", True)]
    return model.downstream(cycle, fresh) if fresh else set()


def _borrow(run, cycle, only, reuse, registry=None):
    """Fill in the steps this run is not going to do. Returns what was filled.

    A step taken from an earlier run keeps that run's outputs and is marked
    finished, which is all the scheduler needs: it only ever looks at steps
    that are still pending, and it reads outputs out of this same record.

    The original status is preserved. A saved failure is useful to recovery
    conditions, but is never relabelled as success to get past a dependency.
    The message names its source run.

    Two things are never borrowed. A step that **did not run** back then has
    nothing to lend - see :data:`LENDABLE`. And a step this run must do itself
    is left **pending**, so the scheduler does it here: that is how an approval
    gate comes to be asked in every run rather than inherited from one somebody
    answered an hour ago, and why nothing decided on the strength of that old
    answer is inherited either - see :func:`_must_run`.
    """
    if not only:
        return []
    registry = registry or default_registry
    wanted = {one for one in only if run.steps.get(one) is not None}
    earlier = (reuse.steps if reuse is not None else {}) or {}
    fresh = _must_run(cycle, registry)
    current_definitions = checkpoints.signatures(cycle, run.variables, registry)
    # What the selection actually reads. Anything else is borrowed only so the
    # record shows the last known state of the whole cycle.
    feeding = model.upstream(cycle, wanted)
    # What the selection feeds. An earlier result there was made from inputs
    # this run is about to replace - the last run's task, say, where this one
    # may take another - so lending it would pair new answers with old ones.
    fed = model.downstream(cycle, wanted) - set(wanted)
    if reuse is not None:
        run.revisions = copy.deepcopy(reuse.revisions)
        run.revision_inputs = copy.deepcopy(reuse.revision_inputs)

    filled = []
    for step in cycle.steps:
        if step.id in wanted:
            continue
        if step.id in fresh and step.id not in fed:
            # Left pending on purpose, so the scheduler runs it in order with
            # everything else that has to be decided again this time. Not when
            # it comes after the selection, though: "run this step" is not
            # "and everything after it", and such a step would start on the
            # last run's answers before the selection had produced new ones.
            continue
        was = earlier.get(step.id)
        if was is not None and (was.status not in LENDABLE or step.id in fed):
            was = None
        step_run = run.steps[step.id]
        if was is None:
            # Not asked for, and nothing to take. Skipped rather than left
            # pending: pending means "not yet", and the scheduler would run it
            # - which is the opposite of what "only these" asked for. Nothing
            # the selection needs can land here; `missing_for` refuses the run
            # before it starts if it would.
            step_run.status = SKIPPED
            step_run.message = "not part of this run"
            step_run.ended_at = time.time()
            filled.append(step_run)
            continue
        if was.definition_digest and was.definition_digest != current_definitions[step.id]:
            if step.id in feeding:
                raise checkpoints.CheckpointError(
                    "Saved settings for %s changed. Select that node explicitly to rerun "
                    "it; its paid result was preserved." % step.id)
            # Not read by anything this run does, so a result made under other
            # settings is simply not shown as this run's - rather than refusing
            # to run a step that never looks at it.
            step_run.status = SKIPPED
            step_run.message = ("not part of this run; its last result was made "
                                "with settings that have since changed")
            step_run.ended_at = time.time()
            filled.append(step_run)
            continue
        step_run.status = was.status
        step_run.source_run = was.source_run or reuse.id
        step_run.definition_digest = was.definition_digest
        step_run.input_digest = was.input_digest
        step_run.artifact_digests = dict(was.artifact_digests)
        step_run.outputs = dict(was.outputs or {})
        step_run.artifacts = list(was.artifacts or [])
        step_run.metrics = dict(was.metrics or {})
        step_run.attempts = was.attempts
        step_run.started_at = was.started_at
        step_run.ended_at = was.ended_at
        step_run.duration_ms = was.duration_ms
        step_run.message = "reused from %s (%s)" % (
            reuse.id, was.status if was.status else "no status")
        if reuse.workspace and os.path.isdir(reuse.workspace):
            source = os.path.join(reuse.workspace, "steps", step.id)
            target = os.path.join(run.workspace, "steps", step.id)
            if os.path.isdir(source) and os.path.realpath(source) != os.path.realpath(target):
                try:
                    shutil.copytree(source, target, dirs_exist_ok=True)
                except OSError as exc:
                    raise checkpoints.CheckpointError(
                        "Cannot copy saved artifacts for %s: %s" % (step.id, exc)) from exc
            for artifact in step_run.artifacts:
                if os.path.isabs(artifact.path):
                    continue
                source = os.path.realpath(os.path.join(reuse.workspace, artifact.path))
                target = os.path.realpath(os.path.join(run.workspace, artifact.path))
                if (os.path.commonpath([source, os.path.realpath(reuse.workspace)])
                        != os.path.realpath(reuse.workspace)
                        or os.path.commonpath([target, os.path.realpath(run.workspace)])
                        != os.path.realpath(run.workspace)):
                    raise checkpoints.CheckpointError("Artifact escapes its run: %s" % artifact.path)
                if os.path.isfile(source) and source != target:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(source, target)
        filled.append(step_run)
    return filled


def missing_for(cycle, only, reuse, registry=None):
    """The steps a partial run needs an answer for and has not got one for.

    Asked *before* the run starts, because the alternative is a step failing
    several minutes in on ``${steps.implement.outputs.tree} does not exist`` -
    which is true, unhelpful, and arrives after the expensive part.

    The same two exceptions :func:`_borrow` makes, for the same reasons: a step
    that did not run back then is not an answer, and one this run is going to
    do itself is not missing.
    """
    if not only:
        return []
    registry = registry or default_registry
    earlier = (reuse.steps if reuse is not None else {}) or {}
    fresh = _must_run(cycle, registry)
    relevant = model.upstream(cycle, only) | set(only) | fresh
    missing = []
    for one in model.upstream(cycle, only):
        if one in fresh:
            continue                     # this run does it, so it is not missing
        was = earlier.get(one)
        if was is None or was.status not in LENDABLE:
            # A report/finalizer may wait for both arms of a branch without
            # consuming the skipped arm's outputs. That is not missing work.
            consumers = [s for s in cycle.steps if s.id in relevant and one in s.needs]
            if was is not None and was.status == SKIPPED and consumers and all(
                    s.condition and not any(
                        ref.startswith("steps.%s.outputs" % one)
                        for ref in variables.references(s.settings)) for s in consumers):
                continue
            missing.append(one)
    return sorted(missing)


def _verdict(run):
    """What the run as a whole came to, read from what its steps came to.

    Deliberately not "was the cancel token set": the token is also how the
    scheduler tells stragglers to stop once the last step is done, so a run
    where everything succeeded would read as cancelled. The steps know what
    happened to them, and a run that was genuinely stopped always leaves at
    least one of them saying so.
    """
    statuses = {step.status for step in run.steps.values()}
    if statuses & {FAILED, TIMEOUT}:
        return FAILED
    if CANCELLED in statuses:
        return CANCELLED
    return SUCCESS


def _cleanup(context, cycle, observer):
    """Release everything the run took, newest first, whatever went wrong.

    Reverse order because a service started after the database it needs must be
    stopped before it. Every call is guarded: a plugin that throws while tidying
    up is a line in the log, never the reason a run reports something other
    than what it actually did.
    """
    by_id = {step.id: step for step in cycle.steps}
    for step_id, plugin, handle in context.held():
        step = by_id.get(step_id)
        try:
            plugin.stop(context, step, handle)
        except Exception as exc:              # noqa: BLE001 - cleanup is best effort
            log.warning("%s: could not stop what it started: %s", step_id, exc)
    for step_id in list(getattr(context, "_prepared", ())):
        step = by_id.get(step_id)
        plugin = getattr(context, "_prepared", {}).get(step_id)
        try:
            if plugin is not None and step is not None:
                plugin.cleanup(context, step)
        except Exception as exc:              # noqa: BLE001
            log.warning("%s: cleanup failed: %s", step_id, exc)


class _Scheduler(object):
    """The loop that decides what runs next. One instance per run.

    Everything mutable lives here and is touched only by :meth:`drive`, which
    runs on the calling thread. Workers communicate by putting finished results
    on a queue; they never write to the run record.
    """

    def __init__(self, cycle, run, context, observer, registry, jobs):
        self.cycle = cycle
        self.run = run
        self.context = context
        self.observer = observer
        self.registry = registry
        self.jobs = jobs

        self.steps = {step.id: step for step in cycle.steps}
        self.done = queue.Queue()
        self.inflight = {}                    # step id -> (token, deadline)
        #: Steps that asked to be come back to: step id -> monotonic seconds.
        #: Not in flight and not finished - the third thing a step can be.
        self.waiting = {}
        #: A step's deadline and its first start, kept here rather than
        #: recomputed per turn: a step that waits four times must not get its
        #: whole timeout back each time, or `timeout:` would stop bounding it.
        self.deadlines = {}                   # step id -> monotonic, or None
        self.begun = {}                       # step id -> monotonic
        self.waited = {}                      # step id -> how many times
        self.groups = {}                      # concurrency group -> semaphore
        #: Steps past their deadline whose person has been asked whether to
        #: keep going: step id -> (the question's token, {"answer": ...}).
        #: The answer is written by the asking thread and read only here.
        self.overdue = {}
        self.extended = {}                    # step id -> times given more
        self.prepared = {}                    # step id -> plugin, for cleanup
        # Shared with _cleanup, which runs after this object is finished with.
        self.context._prepared = self.prepared

        # Three levels of token, and the middle one is the point. The run's own
        # token means "somebody stopped this run" and nothing else touches it,
        # so the verdict can trust it. `stopping` is a child that also fires
        # when the last step is done, which is how stragglers are told to give
        # up without the run reading as cancelled. Each step gets a child of
        # `stopping`, so its deadline reaches only itself.
        self.stopping = context.cancel.child()

    # -- the loop -------------------------------------------------------------
    def drive(self):
        pool = ThreadPoolExecutor(max_workers=self.jobs,
                                  thread_name_prefix="cycle")
        try:
            self._submit(pool)
            # `or self.waiting`: a run whose only remaining work is a step that
            # asked to be come back to still has work. Without this the loop
            # would fall out the moment nothing was in flight and the waiting
            # step would be cancelled by _resolve_remaining, which is the exact
            # opposite of what it asked for.
            while self.inflight or self.waiting:
                finished = self._take()
                if finished is None:
                    self._enforce_deadlines()
                    self._submit(pool)        # anything whose wait is over
                    continue
                self._record(*finished)
                self._submit(pool)
        except KeyboardInterrupt:
            # Ctrl+C is a cancellation, not a crash. Caught here so the run
            # still gets its cleanup pass, its record and its verdict - the
            # alternative is a traceback over a half-torn-down run with
            # services left going and nothing written down about any of it.
            # The run's own token, so the verdict reads "cancelled".
            log.warning("Interrupted; stopping the run.")
            self.context.cancel.set("interrupted")
        except checkpoints.CheckpointError as exc:
            self.stopping.set(str(exc))
            raise
        finally:
            # Not pool.shutdown(wait=True): a plugin that ignores its token
            # would hang the run for as long as it likes. Ask, wait a while,
            # then report what is still going rather than never returning.
            #
            # Set without a reason on purpose. If something already stopped this
            # run - an interrupt, a failed step, the user - that reason is the
            # true one and is inherited from the parent token. "the run ended"
            # is only the fallback for the ordinary case where nothing was
            # wrong and the last step simply finished.
            self.stopping.set()
            pool.shutdown(wait=False)
            self._drain()
        self._resolve_remaining()

    def why(self):
        """Why the run ended the way it did, for the run's own message."""
        if self.context.cancel.is_set():
            return self.context.cancel.reason or "the run was stopped"
        for step in self.cycle.steps:
            step_run = self.run.steps[step.id]
            if step_run.status in (FAILED, TIMEOUT):
                return "%s %s: %s" % (step.id, step_run.status, step_run.message)
        return ""

    def _take(self):
        """One finished step, or None when it is time to check deadlines."""
        timeout = self._until_next_deadline()
        try:
            return self.done.get(timeout=timeout)
        except queue.Empty:
            return None

    def _until_next_deadline(self):
        """How long the loop may sit on the queue before it has to look around.

        Both kinds of clock count: a step in flight that is running out of time,
        and a step whose wait is nearly over. Missing the second would leave a
        step that asked for one second sitting until the next idle tick.
        """
        soonest = [deadline for _token, deadline in self.inflight.values()
                   if deadline is not None]
        soonest.extend(self.waiting.values())
        if not soonest:
            return IDLE_TICK
        return max(0.01, min(min(soonest) - time.monotonic(), IDLE_TICK))

    def _enforce_deadlines(self):
        """Tell anything overdue to stop. It is recorded when it actually does."""
        now = time.monotonic()
        for step_id, (token, deadline) in list(self.inflight.items()):
            if deadline is not None and now >= deadline and not token.is_set():
                if self._ask_overdue(step_id, token):
                    continue
                token.set(self._timed_out(step_id))
                log.warning("%s: timed out; asked it to stop", step_id)
        self._settle_overdue()
        # A waiting step has no thread to tell, so its deadline is settled here
        # rather than by anything noticing a token. Without this a step that
        # asked to wait an hour would outlive the `timeout: 60` written on it.
        for step_id in [one for one in self.waiting
                        if self.deadlines.get(one) is not None
                        and now >= self.deadlines[one]]:
            self.waiting.pop(step_id, None)
            step_run = self.run.steps[step_id]
            self._resolve(self.steps[step_id], TIMEOUT,
                          "timed out after %ss while waiting: %s"
                          % (_clean(self.steps[step_id].timeout),
                             step_run.message or "it asked to be come back to"))

    def _ask_overdue(self, step_id, token):
        """Ask whether a step past its deadline should keep going. True if asked.

        A deadline is somebody's guess at how long the work takes, written
        before it ran, and an agent halfway through a change it has already
        paid for is worth more finished than stopped. So when there is a person
        to ask, they decide; the step keeps running while they do. Not asked
        with nobody there, and not for a plugin whose deadline is the point of
        it - a gate's timeout is how long its own question waits.

        The step's deadline is lifted while the question stands, so the loop
        does not wake a hundred times a second over a clock that already ran
        out. Its token is left alone: Stop still reaches it.
        """
        plugin = self.registry.get(self.steps[step_id].plugin)
        if (not ask.enabled() or plugin is None
                or not plugin.metadata.asks_when_overdue):
            return False
        step = self.steps[step_id]
        question = token.child()
        slot = {"answer": None}
        text = ("%s is taking longer than expected: it has run for %s, past "
                "its timeout. Continue gives it another %s; Cancel stops it now."
                % (step.title, _span(time.monotonic() - self.begun[step_id]),
                   _span(step.timeout)))

        def asking():
            slot["answer"] = ask.ask(text, options=OVERDUE_OPTIONS,
                                     step=step_id, cancel=question)

        self.overdue[step_id] = (question, slot)
        self.inflight[step_id] = (token, None)
        threading.Thread(target=asking, name="cycle-overdue-%s" % step_id,
                         daemon=True).start()
        log.warning("%s: past its timeout; asking whether to continue", step_id)
        return True

    def _settle_overdue(self):
        """Act on whichever overdue questions have been answered."""
        for step_id, (question, slot) in list(self.overdue.items()):
            answer = slot["answer"]
            if answer is None:
                continue
            self.overdue.pop(step_id)
            if step_id not in self.inflight:
                continue                      # it finished while being asked
            token = self.inflight[step_id][0]
            step = self.steps[step_id]
            if answer.get("answered") and answer.get("answer") == OVERDUE_OPTIONS[0]:
                deadline = time.monotonic() + step.timeout
                self.deadlines[step_id] = deadline
                self.inflight[step_id] = (token, deadline)
                self.extended[step_id] = self.extended.get(step_id, 0) + 1
                self.context.stage(step_id, {
                    "kind": "review", "status": "done",
                    "title": "Given another %s" % _span(step.timeout),
                    "detail": "Continued past its timeout%s."
                              % (" by " + answer["who"] if answer.get("who") else "")})
                continue
            # Cancel, a closed window, nobody there to answer: every one of
            # them stops the step, because none of them is somebody saying yes.
            token.set(self._timed_out(step_id, answer.get("message", "")))
            log.warning("%s: timed out; asked it to stop", step_id)

    def _timed_out(self, step_id, why=""):
        """The reason a step stopped by its deadline carries. Says "timed out"."""
        allowed = self.steps[step_id].timeout * (1 + self.extended.get(step_id, 0))
        reason = "timed out after %ss" % _clean(allowed)
        return "%s (%s)" % (reason, why) if why else reason

    def _drain(self):
        """Take whatever the pool manages to finish in the grace period."""
        deadline = time.monotonic() + DRAIN_GRACE
        while self.inflight and time.monotonic() < deadline:
            try:
                finished = self.done.get(timeout=0.1)
            except queue.Empty:
                continue
            self._record(*finished)

    def _resolve_remaining(self):
        """Give every step that never reached a verdict an honest one."""
        for step_id, (token, _deadline) in list(self.inflight.items()):
            step_run = self.run.steps[step_id]
            step_run.status = TIMEOUT if "timed out" in token.reason else CANCELLED
            step_run.message = (step_run.message
                                or "still running when the run ended")
            self.observer.step_end(step_run)
        if self.inflight:
            self.run.message = ("%d step(s) were still running when the run "
                                "ended" % len(self.inflight))
            log.warning(self.run.message)
        self.inflight.clear()
        # A step set aside for later, when there is no later. Its own deadline
        # is the one case where it is a timeout rather than a cancellation:
        # nothing stopped it, it simply never got the time it asked for.
        for step_id, resume_at in list(self.waiting.items()):
            deadline = self.deadlines.get(step_id)
            overdue = deadline is not None and resume_at >= deadline
            self._resolve(self.steps[step_id], TIMEOUT if overdue else CANCELLED,
                          "the run ended while it was waiting")
        self.waiting.clear()
        for step in self.cycle.steps:
            step_run = self.run.steps[step.id]
            if step_run.status == PENDING:
                self._resolve(step, CANCELLED, "the run ended first")

    # -- what may start -------------------------------------------------------
    def _submit(self, pool):
        """Start everything that can start now, and resolve everything that cannot."""
        self._wake()
        while True:
            started = False
            for step in self.cycle.steps:
                if self.run.steps[step.id].status != PENDING:
                    continue
                if step.id in self.inflight:
                    continue
                verdict = self._readiness(step)
                if verdict is _WAIT:
                    continue
                if verdict is _GO:
                    self._start(pool, step)
                    started = True
                else:
                    self._resolve(step, verdict[0], verdict[1])
                    started = True
            if not started:
                return

    def _wake(self):
        """Put back into the running every step whose wait is over.

        Back to PENDING rather than straight into the pool, so a woken step
        goes through :meth:`_readiness` again like any other. That matters: the
        run may have been stopped while it waited, and a step that walked
        around the readiness check would start on a run already on its way
        down.
        """
        now = time.monotonic()
        # A stopping run wakes everything at once, whatever it asked for.
        # Otherwise a run stopped at one minute past would sit out the hour a
        # waiting step had booked before it could say it was cancelled.
        stopping = self.stopping.is_set()
        for step_id in [one for one, resume_at in self.waiting.items()
                        if stopping or now >= resume_at]:
            self.waiting.pop(step_id, None)
            self.run.steps[step_id].status = PENDING

    def _readiness(self, step):
        """``_GO``, ``_WAIT``, or ``(status, reason)`` for a step that will not run."""
        if self.stopping.is_set():
            return CANCELLED, self.stopping.reason or "the run was stopped"
        if step.disabled:
            return SKIPPED, "disabled"

        # A successful intermediate result must not conceal a refreshed
        # approval/lease from unfinished work further down the graph.
        ancestors = model.upstream(self.cycle, {step.id})
        for need in ancestors & self.context.checkpoints.refreshed:
            state = self.run.steps[need].status
            if state not in TERMINAL:
                return _WAIT
            if state != SUCCESS and not step.condition:
                return SKIPPED, "%s %s" % (need, state)

        for need in step.needs:
            need_run = self.run.steps.get(need)
            if need_run is None:
                return SKIPPED, "needs %s, which is not a step in this cycle" % need
            if need_run.status not in TERMINAL:
                return _WAIT
            # A dependency that did not succeed skips its dependents - unless
            # the step has an `if:`, in which case the author has said they
            # will decide. That is what makes the obvious use of a condition
            # possible at all: a debug step that needs the tests and runs
            # precisely because they failed. Without the exception, `needs`
            # would skip it before the condition was ever read, and the only
            # way to write it would be to drop `needs` - which would let it
            # run before the thing it is reacting to had finished.
            if need_run.status != SUCCESS and not step.condition:
                return SKIPPED, "%s %s" % (need, need_run.status)

        if step.condition:
            try:
                if not conditions.evaluate(step.condition, self._scope()):
                    return SKIPPED, "if: %s was not true" % step.condition
            except conditions.ConditionUnavailable as exc:
                return SKIPPED, "if: %s" % exc
            except Exception as exc:          # noqa: BLE001 - a bad reference
                return FAILED, "if: %s" % exc

        for name in ancestors & self.context.restored:
            previous = self.run.steps[name]
            if previous.status != SUCCESS or not previous.input_digest:
                continue
            try:
                settings = revisions.settings(self.steps[name], self.run, self._scope())
            except variables.ResolveError as exc:
                return FAILED, "Saved inputs for %s are unavailable: %s" % (name, exc)
            if checkpoints.digest(settings) != previous.input_digest:
                return FAILED, ("Saved inputs for %s changed after refresh. Its paid result "
                                "was preserved; decide explicitly whether to rerun it." % name)

        plugin = self.registry.get(step.plugin)
        if plugin is None:
            return FAILED, "no plugin called %r is installed" % step.plugin
        return _GO

    def learn_subject(self, step_id=""):
        """Find out what this run works on, if it has become possible.

        Asked after every step that succeeds, and once at the start for
        whatever a partial run borrowed. Said once: a subject does not change
        halfway through a run, and the one that settled it is the answer.
        """
        if self.run.subject or getattr(self.cycle, "subject", None) is None:
            return
        found = sessions.resolve(self.cycle, self.run, self._scope(), step_id)
        if found:
            self.run.subject = found
            self.observer.subject(self.run)

    def _scope(self):
        return variables.scope(self.run, self.cycle, self.run.steps,
                               env=self.context.environment,
                               secrets=getattr(self.context, "secrets", {}))

    # -- running one ----------------------------------------------------------
    def _start(self, pool, step):
        plugin = self.registry.get(step.plugin)
        step_run = self.run.steps[step.id]
        first = step.id not in self.begun
        step_run.status = RUNNING
        if first:
            step_run.started_at = time.time()

        token = self.stopping.child()
        if first:
            # Once, on the first start. A step that waits and comes back keeps
            # the deadline it was given: `timeout: 60` means this step has a
            # minute, not a minute per turn - which is also what stops a plugin
            # asking to wait for ever.
            self.begun[step.id] = time.monotonic()
            self.deadlines[step.id] = ((time.monotonic() + step.timeout)
                                       if step.timeout else None)
        self.inflight[step.id] = (token, self.deadlines[step.id])

        try:
            settings = revisions.settings(step, self.run, self._scope())
        except variables.ResolveError as exc:
            self.inflight.pop(step.id, None)
            self._resolve(step, FAILED, str(exc))
            return

        step_run.input_digest = checkpoints.digest(settings)
        step_run.definition_digest = self.context.checkpoints.definitions[step.id]
        self.context.checkpoints.step(self.run, step_run)
        self.observer.step_start(step_run, attempt=step_run.attempts + 1)
        pool.submit(self._work, step, plugin, settings, token)

    def _work(self, step, plugin, settings, token):
        """One step, on a worker thread. Puts its result on the queue, always.

        Nothing here touches the run record: the scheduler owns that, and a
        worker writing to it would need a lock around every field for the sake
        of something the scheduler can do anyway.
        """
        result, error, attempts = None, None, 0
        started = time.monotonic()
        semaphore = self._semaphore(plugin)
        # The plugin sees its own token as context.cancel, so its deadline
        # reaches it. Without this a step's timeout would set a token nothing
        # was watching, and only stopping the whole run would ever be noticed.
        context = self.context.for_step(token)
        try:
            if semaphore is not None:
                # Held for the whole of execute: the point of a group is that
                # two members are never both inside their plugin at once.
                semaphore.acquire()
            resolved = _Step(step, settings)
            if step.id not in self.prepared:
                # Once per step, not once per turn. `cleanup` is called exactly
                # once for every step that reached `prepare`, so a step that
                # waited four times and prepared four times would take four
                # things and give one back. Safe without a lock: two turns of
                # one step never overlap - the scheduler re-submits only after
                # recording the turn before.
                self.prepared[step.id] = plugin
                plugin.prepare(context, resolved)

            for attempt in range(1, max(1, step.retry_attempts) + 1):
                attempts = attempt
                if token.is_set():
                    break
                if attempt > 1:
                    self.observer.step_retry(self.run.steps[step.id], attempt,
                                             step.retry_delay,
                                             getattr(result, "message", "")
                                             or str(error or ""))
                    self.observer.step_start(self.run.steps[step.id], attempt)
                try:
                    result, error = plugin.execute(context, resolved), None
                except Cancelled as exc:
                    result, error = None, exc
                    break
                except checkpoints.CheckpointError as exc:
                    result, error = None, exc
                    break
                except Exception as exc:      # noqa: BLE001 - a plugin's fault
                    log.debug("%s: attempt %d raised", step.id, attempt,
                              exc_info=True)
                    result, error = None, exc
                if result is not None and (result.ok or result.unfinished):
                    # `unfinished` as well as `ok`: a step that asked to be come
                    # back to has not failed, and retrying it here would call it
                    # again at once - spending the whole retry budget in a
                    # moment and never letting the scheduler see the wait.
                    break
                if attempt < max(1, step.retry_attempts):
                    # Waiting on the token, not sleeping: a cancelled run must
                    # not sit through the rest of a retry delay.
                    if token.wait(step.retry_delay):
                        break
        except Exception as exc:              # noqa: BLE001 - prepare, or the pool
            result, error = None, exc
        finally:
            if semaphore is not None:
                semaphore.release()
            self.done.put((step, result, error, attempts,
                           (time.monotonic() - started) * 1000.0, token))

    def _semaphore(self, plugin):
        group = plugin.metadata.concurrency_group
        if not group:
            return None
        return self.groups.setdefault(group, threading.BoundedSemaphore(1))

    # -- writing down what happened -------------------------------------------
    def _record(self, step, result, error, attempts, duration_ms, token):
        self.inflight.pop(step.id, None)
        asked = self.overdue.pop(step.id, None)
        if asked is not None:
            # Finished while somebody was being asked about it. The question is
            # withdrawn rather than left on screen deciding nothing.
            asked[0].set()
        step_run = self.run.steps[step.id]
        # Accumulated, not assigned: a step that waited and came back has run
        # more than once, and the record should say how many times in total.
        step_run.attempts += attempts

        if result is not None and result.revision and not token.is_set():
            try:
                changed = revisions.apply(self.cycle, self.run, step, result.revision,
                                          self.context.checkpoints, self.inflight)
            except ValueError as exc:
                result = default_registry.failed(str(exc))
            else:
                for name in changed:
                    for mapping in (self.begun, self.deadlines, self.waited, self.waiting):
                        mapping.pop(name, None)
                    self.context.restored.discard(name)
                    self.observer.step_end(self.run.steps[name])
                if self.run.revisions:
                    self.observer.revision(self.run, self.run.revisions[-1])
                self.context.stage(step.id, {"kind": "review", "status": "done",
                    "title": "Plan revision requested", "detail": result.revision["feedback"]})
                return

        if result is not None and result.unfinished and not token.is_set():
            # Not a verdict: the plugin says it is not finished and when it is
            # worth asking again. Nothing here is final - no ended_at, no
            # step_end, no on_failure - because nothing has ended.
            #
            # The token check is what makes a wait refusable, and it covers
            # both ways a wait can be refused: the token is a child of the
            # run's, so it reads as set when the run is stopping as well as
            # when this step's own deadline passed. Either way the result falls
            # through and is finished properly below - otherwise a plugin could
            # keep a stopped run alive by asking nicely.
            return self._park(step, result)

        step_run.ended_at = time.time()
        step_run.duration_ms = duration_ms

        if result is not None:
            step_run.status = result.status
            step_run.message = result.message
            step_run.outputs = dict(result.outputs or {})
            step_run.metrics = dict(result.metrics or {})
            step_run.artifacts = list(result.artifacts or [])
            for artifact in step_run.artifacts:
                self.observer.artifact(step.id, artifact)
        elif error is not None:
            step_run.status = FAILED
            step_run.message = "%s: %s" % (type(error).__name__, error)
        else:
            step_run.status = CANCELLED
            step_run.message = token.reason or "stopped before it ran"

        # A step told to stop is not a step that failed on its own account, and
        # the record should say which of the two happened. The reason leads the
        # message: "timed out after 30s" is what somebody needs to read first,
        # and whatever the plugin said about it follows.
        #
        # WAITING belongs in this list even though it is not a verdict: it only
        # reaches here when the wait was refused, and leaving it would give the
        # step a status the scheduler never finishes and no later pass catches.
        if token.is_set() and step_run.status in (FAILED, CANCELLED, WAITING):
            reason = token.reason or "stopped"
            timed_out = "timed out" in reason
            step_run.status = TIMEOUT if timed_out else CANCELLED
            step_run.message = ("%s: %s" % (reason, step_run.message)
                                if step_run.message else reason)

        if self.waited.get(step.id):
            # It waited, so the worker's own elapsed is only the last turn of
            # it. What a reader wants from a step that took an hour, of which
            # it spent fifty-nine minutes waiting, is the hour. Set after the
            # result, which would otherwise overwrite the metrics with its own.
            step_run.duration_ms = (time.monotonic()
                                    - self.begun[step.id]) * 1000.0
            step_run.metrics = dict(step_run.metrics or {},
                                    waits=self.waited[step.id])

        self.context.checkpoints.step(self.run, step_run)
        self.observer.step_end(step_run)
        if step_run.status == SUCCESS:
            self.learn_subject(step.id)
        if isinstance(error, checkpoints.CheckpointError):
            # An unavailable receipt store is a run-level failure, even for a
            # node configured to continue after ordinary business failures.
            raise error
        if step_run.status in (FAILED, TIMEOUT) and step.on_failure != CONTINUE:
            # `stopping`, not the run's own token: the run did not get
            # cancelled, a step failed under `on_failure: stop`. The verdict
            # reads FAILED from the step, which is the truer account.
            self.stopping.set("%s %s" % (step.id, step_run.status))

    def _park(self, step, result):
        """Set a step aside until the time it asked for. Not a verdict.

        Its outputs are kept: a plugin that looked, found nothing ready and
        said so may well have learned something worth reading while it waits -
        and a later turn simply overwrites them. Its artifacts are published
        now rather than held, because a file that exists exists.
        """
        step_run = self.run.steps[step.id]
        step_run.status = WAITING
        step_run.message = result.message
        step_run.outputs = dict(result.outputs or {})
        step_run.metrics = dict(result.metrics or {})
        if result.artifacts:
            step_run.artifacts = list(step_run.artifacts) + list(result.artifacts)
            for artifact in result.artifacts:
                self.observer.artifact(step.id, artifact)

        resume_at = result.resume_at or (time.monotonic()
                                         + default_registry.MIN_WAIT)
        deadline = self.deadlines.get(step.id)
        if deadline is not None:
            # Never past its own deadline: waking a step a second after it was
            # due to time out would run it once more for nothing. Capped rather
            # than refused, so the step still ends as a timeout at the moment
            # it was always going to.
            resume_at = min(resume_at, deadline)
        self.waiting[step.id] = resume_at
        self.waited[step.id] = self.waited.get(step.id, 0) + 1
        self.context.checkpoints.step(self.run, step_run)
        self.observer.step_waiting(step_run, max(0.0, resume_at - time.monotonic()))

    def _resolve(self, step, status, reason):
        """Give a step that never ran its verdict, and say why."""
        step_run = self.run.steps[step.id]
        step_run.status = status
        step_run.message = reason
        step_run.ended_at = time.time()
        self.context.checkpoints.step(self.run, step_run)
        if status == SKIPPED:
            self.observer.step_skipped(step_run, reason)
        self.observer.step_end(step_run)
        # TIMEOUT as well as FAILED: a step whose deadline passed while it was
        # waiting never goes through _record, so this is the only place that
        # would stop the run on its account.
        if status in (FAILED, TIMEOUT) and step.on_failure != CONTINUE:
            self.stopping.set("%s %s" % (step.id, status))


class _Step(object):
    """A step with its ``${...}`` already resolved, for one execution.

    A shallow stand-in rather than a mutated :class:`~domain.cycle.CycleStep`,
    because the cycle is shared across the whole run and a resolved value is
    true only for the attempt it was resolved for.
    """

    def __init__(self, step, settings):
        self._step = step
        self.settings = settings

    def __getattr__(self, name):
        return getattr(self._step, name)


#: Sentinels for :meth:`_Scheduler._readiness`. Objects rather than strings so
#: they can never be mistaken for a status.
_GO = object()
_WAIT = object()


def _clean(number):
    """A timeout as it was written: 30 rather than 30.0."""
    if number is None:
        return "?"
    return int(number) if float(number).is_integer() else number


def _span(seconds):
    """A length of time the way a person says it: 1h 30m, 45m, 20s."""
    seconds = max(0, int(round(seconds or 0)))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return "%dh %dm" % (hours, minutes) if minutes else "%dh" % hours
    if minutes:
        return "%dm" % minutes
    return "%ds" % seconds
