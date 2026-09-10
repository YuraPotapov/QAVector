"""Execution engine: attach to each launched session and run the scenarios.

Entry point :func:`run_scenarios` is what ``session_launcher.py`` calls when
``--run-tests`` is given. For each launched Chrome it reads the profile's
``DevToolsActivePort``, attaches Playwright over CDP (the auto-login extension
has already signed in by then), compiles each scenario, executes it step by step
with per-step retries, captures artifacts on failure, and writes a report.
Returns a process exit code (0 = all passed).
"""

import concurrent.futures
import contextvars
import logging
import os
import threading
import time

import runtime_paths
import system_load

from adapters.nobrowser import NoBrowserAdapter
from domain.result import FlowResult, RunResult, StepResult, PASS, FAIL, ERROR
from engine import artifacts, assertions, compiler, events, loader, services
from engine.context import RunContext
from engine.overlay import ExecutionOverlay, NullOverlay

log = logging.getLogger("flowengine.runner")


class ServiceError(Exception):
    """A service step could not be carried out (no GUI, unknown service, refused)."""


class _OverlayLogBridge(logging.Handler):
    """Forwards ``flowengine.*`` log records into ONE session's overlay Logs widget.

    Kept at INFO so DEBUG chatter (including the adapter's own overlay-render
    diagnostics) is never forwarded - that also rules out any feedback loop.

    MUST be constructed on the thread that owns its overlay: it records that
    thread's id and ignores records from any other. Handlers live on the shared
    process-wide "flowengine" logger, so with several sessions running at once
    every bridge would otherwise see every session's records - which is not just
    noise in the wrong HUD. `emit` reaches `page.evaluate` through
    overlay.log -> _push -> adapter.overlay_render, so a foreign record would have
    one thread synchronously driving ANOTHER thread's Playwright page: a
    cross-thread greenlet switch, silently eaten by the excepts below.
    """

    def __init__(self, overlay):
        super().__init__(level=logging.INFO)
        self._overlay = overlay
        self._owner_tid = threading.get_ident()

    def filter(self, record):
        # Handlers run inline in the emitting thread, so this identifies the
        # session that produced the record. Filtering here rather than in emit()
        # matters: Handler.handle() calls filter() BEFORE acquire(), so a foreign
        # record never even takes this handler's lock.
        return threading.get_ident() == self._owner_tid and super().filter(record)

    def emit(self, record):
        try:
            self._overlay.log(record.levelname, record.getMessage())
        except Exception:  # logging must never break a run
            pass


# Which session the current thread is driving, for the console log prefix. Set in
# the worker, so the main thread's own lines (Reports ->, the run summary, and
# everything the launcher logs) stay unprefixed.
_session_ctx = contextvars.ContextVar("flowengine_session", default=None)

# Set on CTRL+C during a parallel run. Workers check it between every STEP, not
# only between scenarios: a forty-step flow whose remaining steps each time out
# at 30 s would otherwise take twenty minutes to reach the next scenario
# boundary, and for all that time Stop looks like it did nothing.
_stop_requested = threading.Event()

# Sessions the user has stopped one at a time, by name. _stop_requested is "stop
# everything"; this is "stop that window and leave the others running".
_stopped_sessions = set()
_stopped_lock = threading.Lock()


def request_session_stop(session_name):
    """Stop driving ONE session. Whoever owns its window closes that."""
    with _stopped_lock:
        _stopped_sessions.add(session_name)
    log.warning("--- session %s: stop requested ---", session_name)


def _stopping(session_name=None):
    """Whether to put this session down: everything, or just this one."""
    if _stop_requested.is_set():
        return True
    if session_name is None:
        return False
    with _stopped_lock:
        return session_name in _stopped_sessions


class _SessionPrefixFilter(logging.Filter):
    """Tags each console line with the session that produced it, while parallel.

    Installed on the ROOT handler, not on the "flowengine" logger: a filter on a
    logger only sees records logged through THAT logger, never ones propagating up
    from flowengine.runner/.artifacts/.compiler/.adapter. Handlers are where
    propagated records actually arrive, so this is the one place that catches all
    four with no per-module wiring.
    """

    def filter(self, record):
        prefix = _session_ctx.get()
        if prefix and not getattr(record, "_session_tagged", False):
            record._session_tagged = True   # a root with two handlers must not double-tag
            record.msg = prefix + record.getMessage()
            record.args = ()                # pre-rendered, so a '%' in a name is inert
        return True


def _install_session_prefix():
    """Add the prefix filter to every root handler; returns an undo callable."""
    handlers = list(logging.getLogger().handlers)
    prefix_filter = _SessionPrefixFilter()
    for handler in handlers:
        handler.addFilter(prefix_filter)

    def remove():
        for handler in handlers:
            handler.removeFilter(prefix_filter)
    return remove


def _make_overlay(overlay_components, adapter, session_name=None):
    """The observer the runner notifies at every execution transition.

    Two independent consumers can want the same transitions: the in-page HUD
    (``--execution-overlay``) and the JSONL event stream (``--events``). When
    both are on they are wrapped in a :class:`events.Tee` so the runner keeps
    its single ``overlay.*`` call site; when neither is, this is a no-op object
    and nothing downstream has to test for it.
    """
    observers = []
    if overlay_components:
        observers.append(ExecutionOverlay(overlay_components, adapter))
    if events.enabled():
        observers.append(events.EventObserver(session_name))
    if not observers:
        return NullOverlay()
    if len(observers) == 1:
        return observers[0]
    return events.Tee(observers)

DEFAULT_TIMEOUT_MS = 30000        # generous: covers the post-login readiness gate
DEVTOOLS_WAIT_S = 30              # how long to wait for Chrome to open its debug port
DEFAULT_REPORTS_DIR = runtime_paths.reports_dir()


# --- how many windows the machine can carry ----------------------------------
# The governor samples every SAMPLE_S but acts on ten-second averages, and will
# not step down twice inside BACKOFF_S. That dead time is not caution for its own
# sake: closing a window is a graceful Chrome shutdown worth up to 15 s (see
# close_all), so a faster loop would be reading a machine that has not yet felt
# its own last decision and would step 8 -> 1 before the first close landed.
GOVERNOR_SAMPLE_S = 5.0
GOVERNOR_BACKOFF_S = 30.0
GOVERNOR_RECOVER_S = 60.0     # calm this long before a slot is handed back
MEM_STALL_TRIP = 10.0         # % of wall time some task waited on memory
MEM_STALL_CALM = 1.0
# Measured on an 8-core box: 4 busy tasks read 1%, 8 (saturated, healthy) read
# 20%, 16 read 66%, 32 read 78%. So the trip is set above what a fully used
# machine reads and the calm mark well clear of it - at 20 a healthy saturated
# machine would sit exactly on the line and never be judged calm enough to get a
# slot back, which is the one-way ratchet this design exists to avoid.
CPU_STALL_TRIP = 50.0         # tasks genuinely queueing, ~2x oversubscribed
CPU_STALL_CALM = 35.0
CPU_BUSY_TRIP = 90.0          # plain utilisation - the number on the launch page
CPU_BUSY_CALM = 70.0
# A step down taken for CPU has to earn its keep. If dropping one worker does not
# move utilisation by at least this much, the load is the WORK rather than the
# parallelism - seven windows rendering the same app keep eight cores busy however
# few of them are being driven - and taking more away would only make the run
# longer. The step is undone and CPU stops being a trigger for a while. This is
# what stops a busy-but-healthy machine ratcheting itself down to one.
CPU_STEP_MARGIN = 5.0
CPU_FUTILE_COOLDOWN_S = 300.0
MEM_FLOOR_KB = 1536 * 1024    # never open a window on less headroom than this
WINDOW_HEADROOM = 1.5         # x the measured cost of a window, before opening one


class WindowSlots:
    """How many browser windows may be open at once.

    A permit is a WINDOW, not a worker. The holder opens Chrome only after
    acquiring one and does not release until that Chrome has exited, which is
    what makes lowering the ceiling give memory back - throttling the step loop
    in front of a browser that stays resident cannot.

    Lowering the ceiling never evicts a window that is already open: it finishes
    its scenarios and the new ceiling takes effect as it closes. Anything else
    would abandon a half-run scenario to save memory that the abandoned window is
    still holding until it shuts down anyway.
    """

    def __init__(self, limit):
        self._limit = max(1, int(limit))
        self._held = 0
        self._closed = False
        self._cond = threading.Condition()

    @property
    def limit(self):
        with self._cond:
            return self._limit

    @property
    def held(self):
        with self._cond:
            return self._held

    def acquire(self):
        """Wait for a permit. False means the run is stopping: open nothing."""
        with self._cond:
            while self._held >= self._limit and not self._closed:
                self._cond.wait()
            if self._closed:
                return False
            self._held += 1
            return True

    def release(self):
        with self._cond:
            self._held -= 1
            self._cond.notify_all()

    def set_limit(self, limit):
        """Move the ceiling; returns what it actually became (never below 1)."""
        with self._cond:
            self._limit = max(1, int(limit))
            self._cond.notify_all()
            return self._limit

    def yield_if_over(self):
        """Hand the permit back and re-queue for one, if the ceiling has dropped.

        For permits that govern DRIVING rather than a window's lifetime. There it
        is the only way a lowered ceiling takes effect before the session ends,
        and the browser stays open and resident while its driver waits - so this
        buys CPU back, never memory. Where a permit owns a window, closing it is
        what returns the memory and this must not be used: a parked driver would
        hold an idle browser open and give nothing back.

        Called at scenario boundaries, never between steps: parking halfway
        through a flow leaves a form half filled in for as long as the machine
        stays busy, which is how a throttle turns into a failure.
        """
        with self._cond:
            if self._held <= self._limit or self._closed:
                return
            self._held -= 1
            self._cond.notify_all()
            while self._held >= self._limit and not self._closed:
                self._cond.wait()
            self._held += 1

    def close(self):
        """Release everyone waiting, for good. Held permits are unaffected.

        Without this, CTRL+C would leave queued sessions parked on the condition
        until enough windows closed to let them through - each one then opening a
        browser for a run that is already over.
        """
        with self._cond:
            self._closed = True
            self._cond.notify_all()


def _headroom_short(load, window_cost_kb):
    """Too little memory free to open another window, or None.

    Acted on from a single sample, unlike stall: this is a prediction, and
    waiting for a second reading to confirm it is precisely the delay it exists
    to avoid. ``window_cost_kb`` is what a window has actually cost during this
    run, so the floor is what this workload needs rather than a guess.
    """
    if load.available_kb is None:
        return None
    floor = MEM_FLOOR_KB
    if window_cost_kb:
        floor = max(floor, int(WINDOW_HEADROOM * window_cost_kb))
    if load.available_kb >= floor:
        return None
    return ("%.1f GB free, under the %.1f GB the next window needs"
            % (load.available_kb / system_load.KB_PER_GB, floor / system_load.KB_PER_GB))


def _stalling(load):
    """The kernel reporting tasks actually blocked, or None.

    Deliberately not a function of CPU utilisation: a rig driving windows on
    every core SHOULD read 100%, and tripping on that is what ratchets a ceiling
    to one and leaves it there. Stall is the machine failing to keep up; use is
    the machine being used.
    """
    if load.mem_stall is not None and load.mem_stall > MEM_STALL_TRIP:
        return "memory stalled %.0f%% of the last 10 s" % load.mem_stall
    if load.cpu_stall is not None and load.cpu_stall > CPU_STALL_TRIP:
        return "cpu stalled %.0f%% of the last 10 s" % load.cpu_stall
    return None


def _strain(load, window_cost_kb):
    """Why the machine cannot carry another window, or None if it can."""
    return _headroom_short(load, window_cost_kb) or _stalling(load)


def _calm(load):
    """True when there is room to hand a slot back.

    Stricter than "not strained" on purpose, so the two thresholds cannot chatter
    a slot open and shut. Absent PSI this asks only for headroom, which is the
    honest limit of what a machine without it can tell us.
    """
    if load.available_kb is not None and load.available_kb < 2 * MEM_FLOOR_KB:
        return False
    if load.mem_stall is not None and load.mem_stall > MEM_STALL_CALM:
        return False
    if load.cpu_stall is not None and load.cpu_stall > CPU_STALL_CALM:
        return False
    return load.available_kb is not None or load.mem_stall is not None


def _busy(load):
    """Plain CPU utilisation over the trip line, or None.

    Utilisation is a weaker signal than stall - a saturated machine is usually
    just a machine being used - so this one is checked and then CHECKED AGAIN
    against its own effect (see CPU_STEP_MARGIN). It is here because it is the
    number a person watching the launch page can see, and a governor that never
    responds to it is indistinguishable from a governor that is broken.
    """
    if load.cpu_percent is not None and load.cpu_percent > CPU_BUSY_TRIP:
        return "cpu at %.0f%%" % load.cpu_percent
    return None


def _governor(slots, ceiling, stop_event, unit="windows"):
    """Hold the number of parallel sessions to what this machine can carry.

    Runs only under ``--jobs=auto``. Three things lower the ceiling, in order of
    how much they are trusted: memory headroom too thin to open the next session
    (acted on at once - it is a measurement), the kernel reporting real stall
    (two samples - one can be another program), and plain CPU utilisation (which
    then has to prove it was worth doing).

    Every move is logged. A run that quietly got slower with no line saying why
    is worse than one that never throttled.
    """
    sampler = system_load.Sampler()
    # The priming read happens before the pool has opened anything, which makes it
    # the one honest measure of free memory with none of our sessions up - the
    # mark a session's cost is measured against below.
    baseline_kb = sampler.read().available_kb
    strain_streak = busy_streak = 0
    calm_since = None
    stepped_at = 0.0
    probe = None                # (cpu_before, limit_before) awaiting its verdict
    cpu_muted_until = 0.0

    def step(to, why, level=log.warning):
        current = slots.limit
        new = slots.set_limit(to)
        if new != current:
            level("Load governor: %d -> %d %s (%s)", current, new, unit, why)
            events.emit("governor.limit", limit=new, ceiling=ceiling, unit=unit,
                        why=why)
        return new

    # Say where it starts, so a watcher shows the real number from the first
    # moment rather than only once something has moved.
    events.emit("governor.limit", limit=slots.limit, ceiling=ceiling, unit=unit,
                why="starting at one per core")

    while not stop_event.wait(GOVERNOR_SAMPLE_S):
        load = sampler.read()
        now = time.monotonic()
        held = slots.held
        if load.available_kb is not None and (held == 0 or baseline_kb is None):
            baseline_kb = max(baseline_kb or 0, load.available_kb)   # re-mark when idle
        cost_kb = None
        if baseline_kb and held and load.available_kb is not None:
            cost_kb = max(0, baseline_kb - load.available_kb) // held

        # Did the last CPU step down actually make the machine less busy?
        if probe is not None and now - stepped_at >= GOVERNOR_BACKOFF_S:
            cpu_before, limit_before = probe
            probe = None
            gained = cpu_before - (load.cpu_percent if load.cpu_percent is not None
                                   else cpu_before)
            if gained < CPU_STEP_MARGIN:
                cpu_muted_until = now + CPU_FUTILE_COOLDOWN_S
                step(limit_before,
                     "one fewer moved the CPU by %.0f points, so this load is the "
                     "work and not the parallelism" % gained, level=log.info)
                stepped_at = now
                continue

        short = _headroom_short(load, cost_kb)
        stalling = _stalling(load)
        if short or stalling:
            calm_since = None
            busy_streak = 0
            strain_streak += 1
            if not short and strain_streak < 2:
                continue
            if now - stepped_at < GOVERNOR_BACKOFF_S or slots.limit <= 1:
                continue
            step(slots.limit - 1, short or stalling)
            stepped_at = now
            probe = None            # memory is not judged by its effect on CPU
            continue

        strain_streak = 0
        busy = None if now < cpu_muted_until else _busy(load)
        if busy:
            calm_since = None
            busy_streak += 1
            if busy_streak < 2:
                continue
            if now - stepped_at < GOVERNOR_BACKOFF_S or slots.limit <= 1:
                continue
            probe = (load.cpu_percent, slots.limit)
            step(slots.limit - 1, busy)
            stepped_at = now
            busy_streak = 0
            continue

        busy_streak = 0
        if not _calm(load) or (load.cpu_percent is not None
                               and load.cpu_percent > CPU_BUSY_CALM):
            calm_since = None
            continue
        calm_since = calm_since or now
        if slots.limit < ceiling and now - calm_since >= GOVERNOR_RECOVER_S:
            step(slots.limit + 1,
                 "%.1f GB free, cpu at %.0f%%"
                 % ((load.available_kb or 0) / system_load.KB_PER_GB,
                    load.cpu_percent or 0), level=log.info)
            calm_since = now
            stepped_at = now


def run_scenarios(sessions, which, env=None, flows_dir=None, reports_dir=None,
                  overlay_components=None, report=None, jobs=1, windows=None,
                  server_logs=None, browser=True):
    """Run ``which`` scenarios against every launched ``sessions`` entry.

    ``sessions`` is a list of ``(cls, proc, profile, login, origin[, tests])``
    tuples from the launcher. ``which`` is ``"all"``, a list of scenario ids, or
    ``"config"`` - the last meaning each session runs its OWN ``tests`` (the
    config's per-user field), so one launch can cover several roles without every
    scenario being replayed against every window.
    ``overlay_components`` (from ``--execution-overlay``) enables the in-page HUD.
    ``report`` (from ``--report-*``) is a :class:`artifacts.ReportConfig`; ``None``
    means the legacy default (result.json on success, full bundle on failure).

    ``jobs`` (from ``--jobs``) is how many WINDOWS may be open at once; 1 keeps
    the original one-window-at-a-time behaviour byte for byte. Scenarios inside a
    window always run one after another - a single page cannot be driven twice -
    so this only removes the wait between windows. With more windows than jobs the
    extras queue and start as soon as a slot frees.

    ``jobs="auto"`` is the one value that lets the load governor move that number
    while the run is under way. A number is a number: given 6, this runs 6 at a
    time from the first window to the last, whatever the machine is doing.

    ``windows`` is the launcher's :class:`WindowSource` - ``open(session)`` and
    ``close(proc)`` - and is what makes ``jobs`` mean windows rather than merely
    drivers. Given one, this opens each Chrome inside its own slot and closes it
    when its scenarios are done, so a lowered ceiling actually returns memory,
    and the load governor is started. Without one (the tests, and any caller that
    hands over browsers it opened itself) every window is already resident, a
    ceiling can give nothing back, and no governor runs.

    ``browser`` False (``--no-browser``) drives no page at all. Each session gets
    an adapter that refuses every page operation, and a scenario with a step that
    needs one is an ERROR before its first step. Nothing else changes: sessions,
    ``jobs``, the governor and stopping work exactly as they do with a browser.
    """
    env = env or {}
    # flows_dir stays None when it was not given: the loader turns that into its
    # search path (the user's own tree, then the bundled one), and pinning it to
    # a single directory here would hide everything the user has written.
    reports_dir = reports_dir or DEFAULT_REPORTS_DIR
    report = report or artifacts.ReportConfig()
    selectors = loader.load_selectors(flows_dir)
    per_session = which == "config"
    shared = [] if per_session else _resolve_scenarios(which, flows_dir)
    if not per_session and not shared:
        log.error("No scenarios to run.")
        return 1

    # Both paths, not only the parallel one: a session stopped by hand must not
    # still be stopped for the next run in the same process.
    with _stopped_lock:
        _stopped_sessions.clear()

    run = RunResult()
    run_dir = artifacts.new_run_dir(reports_dir)
    log.info("Reports -> %s", run_dir)
    events.emit("run.dir", dir=run_dir)

    # Results are collected per session POSITION, not appended as they finish, so
    # the summary lists sessions in the order given no matter what order they
    # complete in.
    slots = [[] for _ in sessions]
    auto = str(jobs).lower() == "auto"
    if auto:
        # One window per core to begin with - the governor takes it from there,
        # and only ever downwards from a starting point the machine can plainly
        # carry rather than upwards from a guess.
        workers = max(1, min(len(sessions) or 1, os.cpu_count() or 4))
    else:
        workers = max(1, min(int(jobs), len(sessions) or 1))
    plan = _plan_sessions(sessions, per_session, shared, flows_dir, slots)

    staged = windows is not None
    slot_pool = WindowSlots(workers) if (staged or auto) else None
    # Where the runner owns the window, a lowered ceiling takes effect by closing
    # it - which is what gives memory back. Where the windows were opened for us
    # and stay open, the only thing left to ration is the driving, so sessions
    # yield between scenarios instead. That buys CPU and not memory, and is why
    # --close-after is worth having.
    yielding = slot_pool if (auto and not staged) else None

    def drive(session, session_name, scenarios, log_prefix=None):
        if slot_pool is None:
            return _run_one_session(session, session_name, scenarios, env, flows_dir,
                                    selectors, run_dir, overlay_components, report,
                                    log_prefix=log_prefix, server_logs=server_logs,
                                    browser=browser)
        if not slot_pool.acquire():
            return []            # stopping: this session never started
        proc = None
        try:
            if not staged:
                return _run_one_session(session, session_name, scenarios, env, flows_dir,
                                        selectors, run_dir, overlay_components, report,
                                        log_prefix=log_prefix, slots=yielding,
                                        server_logs=server_logs, browser=browser)
            proc = windows.open(session)
            opened = (session[0], proc) + tuple(session[2:])
            return _run_one_session(opened, session_name, scenarios, env, flows_dir,
                                    selectors, run_dir, overlay_components, report,
                                    log_prefix=log_prefix, server_logs=server_logs,
                                    browser=browser)
        finally:
            # Close BEFORE releasing: the next window must not start while this
            # one is still flushing cookies, or the two overlap for the seconds
            # the slot exists to prevent.
            if proc is not None:
                windows.close(proc)
            slot_pool.release()

    if workers == 1:
        # Lazy consumption keeps the planner's warnings interleaved with the runs,
        # exactly as before there was any notion of workers. No prefix filter is
        # installed either, so single-session output is byte-identical to before.
        ran_any = False
        for index, session, session_name, scenarios in plan:
            ran_any = True
            slots[index] = drive(session, session_name, scenarios)
    else:
        planned = list(plan)
        ran_any = bool(planned)
        log.info("Running %d session(s), %d at a time.", len(planned), workers)
        # Lines from N windows interleave, so tag each with its session. Padding
        # sits OUTSIDE the brackets so a fixed-string grep still matches.
        width = max((len(name) for _i, _s, name, _sc in planned), default=0)
        prefixes = {i: ("[%s]" % name).ljust(width + 3) for i, _s, name, _sc in planned}
        undo_prefix = _install_session_prefix()
        _stop_requested.clear()

        governor_stop = threading.Event()
        if auto:
            # Only with --jobs=auto. A number the user typed is a promise, and a
            # run that quietly does 3 at a time when it was told 8 is a worse
            # failure than one that runs out of memory where the user can see it.
            log.info("Auto: starting at %d %s, adjusted as the machine allows.",
                     workers, "windows" if staged else "parallel sessions")
            if not staged and browser:
                # With no browser there are no windows to have opened, and no
                # memory of theirs to wish back.
                log.info("Windows were all opened up front, so this can free CPU "
                         "but not memory; --close-after lets it free both.")
            threading.Thread(target=_governor,
                             args=(slot_pool, workers, governor_stop),
                             kwargs={"unit": "windows" if staged else "sessions"},
                             name="load-governor", daemon=True).start()
        else:
            # Fixed runs report their number too, once. "How many are running"
            # is a fair question whether or not the answer can change, and a
            # readout that is blank for most runs is one nobody learns to read.
            events.emit("governor.limit", limit=workers, ceiling=workers,
                        unit="windows" if staged else "sessions",
                        why="fixed by --jobs")

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers,
                                                     thread_name_prefix="session")
        try:
            futures = [(index, session_name,
                        pool.submit(drive, session, session_name, scenarios, prefixes[index]))
                       for index, session, session_name, scenarios in planned]
            # Joined in submit order so the log of completions is deterministic;
            # slots keep session order regardless of who finishes first.
            for index, session_name, future in futures:
                try:
                    slots[index] = future.result()
                except concurrent.futures.CancelledError:
                    slots[index] = []          # never started: CTRL+C dropped it
                except KeyboardInterrupt:
                    raise
                except BaseException as exc:      # noqa: BLE001 - one window must not sink the run
                    log.error("--- session %s crashed: %s: %s ---",
                              session_name, type(exc).__name__, exc)
                    slots[index] = [FlowResult(scenario=sid, session=session_name, status=ERROR,
                                               error="session crashed: %s: %s"
                                                     % (type(exc).__name__, exc))
                                    for sid in _scenarios_of(planned, index)]
        except KeyboardInterrupt:
            # Queued sessions are dropped outright; running ones stop after the
            # step they are in (bounded by that step's timeout, 30 s by default).
            _stop_requested.set()
            if slot_pool is not None:
                slot_pool.close()   # queued sessions give up instead of opening a browser
            pool.shutdown(wait=False, cancel_futures=True)
            running = sum(1 for _i, _n, f in futures if f.running())
            log.warning("Interrupted - waiting for %d running session(s) to finish the "
                        "current step...", running)
            raise
        finally:
            # Always drain here: an undrained pool would otherwise be joined by
            # concurrent.futures' atexit hook, hanging the process AFTER the
            # traceback with nothing on screen to explain it.
            governor_stop.set()
            pool.shutdown(wait=True)
            undo_prefix()

    if per_session and not ran_any:
        log.error("No session had any 'tests' configured (--run-tests=config).")
        return 1
    for results in slots:
        run.flows.extend(results)
    _print_summary(run)
    return run.exit_code


def _scenarios_of(planned, index):
    """The scenario ids planned for a session position (for crash bookkeeping)."""
    for i, _session, _name, scenarios in planned:
        if i == index:
            return scenarios
    return []


def _plan_sessions(sessions, per_session, shared, flows_dir, slots):
    """Yield ``(index, session, session_name, scenarios)`` for runnable sessions.

    Deliberately a GENERATOR consumed on the caller's thread: scenario resolution
    stays on the main thread in session order, and the "no tests configured"
    warning is emitted between runs rather than hoisted ahead of all of them.
    Sessions that resolve to nothing write their own ERROR result into ``slots``
    and are not yielded.
    """
    for index, session in enumerate(sessions):
        profile = session[2]
        session_name = os.path.basename(profile.rstrip("/\\"))
        if per_session:
            # session[5] is that user's configured "tests"; a user without any is
            # skipped rather than silently inheriting another role's scenarios.
            configured = list(session[5]) if len(session) > 5 else []
            if not configured:
                log.warning("--- session %s: no 'tests' configured, skipping ---", session_name)
                continue
            # Pass the LIST: _resolve_scenarios treats a str as one whole id (only
            # the CLI splits on commas), so joining these would look for a single
            # scenario literally named "access_manager,multicompany_manager".
            scenarios = _resolve_scenarios(configured, flows_dir)
            if not scenarios:
                log.error("--- session %s: 'tests' %s matched no scenario ---",
                          session_name, ", ".join(configured))
                slots[index] = [FlowResult(scenario=",".join(configured), session=session_name,
                                           status=ERROR, error="no scenario matched 'tests'")]
                continue
        else:
            scenarios = shared
        yield index, session, session_name, scenarios


def _run_one_session(session, session_name, scenarios, env, flows_dir, selectors,
                     run_dir, overlay_components, report, log_prefix=None, slots=None,
                     server_logs=None, browser=True):
    """Drive ONE window: attach, run its scenarios, disconnect. Returns its results.

    Everything here - the CDP attach, every step, every screenshot and the
    disconnect - must happen on ONE thread, because Playwright's sync API is
    bound to the thread that created its instance. That is why this whole function
    is the unit of work handed to a worker, and why the overlay and its log bridge
    are constructed inside it: _OverlayLogBridge records the thread it was built
    on and drops records from any other.

    Returns its own list rather than appending to a shared one, so running several
    of these at once needs no lock at all.
    """
    results = []
    cls, _proc, profile, login, origin = session[:5]
    token = _session_ctx.set(log_prefix) if log_prefix else None
    try:
        return _drive_session(session, session_name, scenarios, env, flows_dir,
                              selectors, run_dir, overlay_components, report, results,
                              slots, server_logs, browser)
    finally:
        if token is not None:
            _session_ctx.reset(token)


def _drive_session(session, session_name, scenarios, env, flows_dir, selectors,
                   run_dir, overlay_components, report, results, slots=None,
                   server_logs=None, browser=True):
    cls, _proc, profile, login, origin = session[:5]
    log.info("--- session %s: %s ---", session_name, ", ".join(scenarios))
    # No browser means nothing to attach to. The session is otherwise the same
    # thing - announced, driven, stoppable - so the rest of this runs as is.
    adapter = _attach(profile, session_name) if browser else NoBrowserAdapter()
    if adapter is None:
        events.emit("session.attach_failed", session=session_name, login=login,
                    profile=profile, scenarios=list(scenarios))
        return [FlowResult(scenario=scenario_id, session=session_name,
                           status=ERROR, error="could not attach over CDP")
                for scenario_id in scenarios]
    events.emit("session.attached", session=session_name, login=login,
                cls=cls, profile=profile, origin=origin)
    ctx = RunContext(user={"login": login, "class": cls},
                     env={"origin": origin, "url": env.get("url")})
    overlay = _make_overlay(overlay_components, adapter, session_name)
    bridge = None
    if overlay_components:
        # Feeds the HUD's Logs widget only - the event stream's consumer already
        # receives every log record on stderr.
        bridge = _OverlayLogBridge(overlay)
        logging.getLogger("flowengine").addHandler(bridge)
    # Show the whole plan up front, so a finished or failed scenario stays visible
    # when the next one starts instead of being replaced by it.
    overlay.session_start(scenarios)
    try:
        for scenario_id in scenarios:
            if _stopping(session_name):
                log.warning("Stopped: skipping %s", scenario_id)
                break
            if slots is not None:
                # A scenario boundary is the only safe place to stand aside: the
                # page is between flows, so a wait here costs time and nothing else.
                slots.yield_if_over()
            results.append(
                _run_scenario(adapter, scenario_id, session_name, flows_dir,
                              selectors, ctx, run_dir, overlay, report, server_logs,
                              browser=browser))
    finally:
        if bridge is not None:
            logging.getLogger("flowengine").removeHandler(bridge)
        # Deliberately do NOT tear the overlay down: the HUD stays in the page
        # (with its final tree + completion banner) so it can be inspected
        # after the run. It disappears naturally when the window is closed
        # (manually, CTRL+C, or --close-after).
        adapter.disconnect()
    return results


def _resolve_scenarios(which, flows_dir):
    """Resolve the --run-tests value to a de-duplicated list of scenario ids.

    Accepts "all", plain ids, and ``tag:<tag>`` entries (expanded to every scenario carrying
    that tag), which may be mixed in a comma-separated list.
    """
    if which == "all":
        found = loader.discover_scenarios(flows_dir)
        if not found:
            log.warning("--run-tests=all but no scenarios found under %s/scenarios", flows_dir)
        return found

    items = [which] if isinstance(which, str) else list(which)
    resolved = []
    for item in items:
        if item.startswith("tag:"):
            tag = item[len("tag:"):]
            matches = loader.scenarios_with_tag(tag, flows_dir)
            if not matches:
                log.warning("no scenarios tagged %r", tag)
            resolved.extend(matches)
        else:
            resolved.append(item)

    seen, out = set(), []
    for scenario in resolved:
        if scenario not in seen:
            seen.add(scenario)
            out.append(scenario)
    return out


def _attach(profile, session_name):
    try:
        endpoint = wait_for_devtools(profile)
        from adapters.playwright_adapter import PlaywrightAdapter  # lazy: needs playwright
        return PlaywrightAdapter.connect(endpoint, DEFAULT_TIMEOUT_MS)
    except Exception as exc:
        log.error("Cannot attach to %s: %s", session_name, exc)
        return None


def wait_for_devtools(profile, timeout_s=DEVTOOLS_WAIT_S):
    """Poll ``<profile>/DevToolsActivePort`` and return http://127.0.0.1:PORT.

    Chrome writes the file as "PORT\\n/devtools/browser/<uuid>\\n" and NOT atomically,
    so a poll can land between the two writes. Requiring both lines means a partial
    read is retried instead of yielding a truncated port. The launcher deletes any
    previous file before starting Chrome (clear_devtools_port), so whatever we read
    here belongs to the window we just launched.
    """
    path = os.path.join(profile, "DevToolsActivePort")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].startswith("/devtools/"):
                return "http://127.0.0.1:%s" % lines[0].strip()
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("Chrome never wrote %s (no --remote-debugging-port?)" % path)


def _run_scenario(adapter, scenario_id, session_name, flows_dir, selectors, ctx, run_dir,
                  overlay=None, report=None, server_logs=None, browser=True):
    overlay = overlay or NullOverlay()
    out_dir = artifacts.scenario_dir(run_dir, session_name, scenario_id)
    # Before the reporter, so the artifact's window can close over it: the backend
    # lines that belong to THIS scenario are the ones written while it ran, not
    # everything the tail has seen since the window opened.
    started = time.time()
    server = None
    if server_logs is not None:
        server = lambda: server_logs.slice_for_session(session_name, started)  # noqa: E731
    reporter = artifacts.Reporter(report or artifacts.ReportConfig(), out_dir, adapter,
                                  server=server)
    result = FlowResult(scenario=scenario_id, session=session_name, artifacts_dir=out_dir)
    try:
        steps, plan = compiler.compile_plan(scenario_id, flows_dir, selectors, ctx)
    except Exception as exc:
        result.status = ERROR
        result.error = "compile: %s" % exc
        result.duration_ms = (time.time() - started) * 1000
        reporter.finalize_compile_error(result)
        log.error("[%s] compile failed: %s", scenario_id, exc)
        return result
    if not browser:
        # The launcher vetted every scenario before the run began; this is for one
        # edited since. A page step here could only fail against no page, so the
        # scenario is refused whole rather than run up to it.
        needs = sorted({step.action for step in steps} - compiler.NO_BROWSER)
        if needs:
            result.status = ERROR
            result.error = "needs a browser: %s" % ", ".join(needs)
            result.duration_ms = (time.time() - started) * 1000
            reporter.finalize_compile_error(result)
            log.error("[%s] %s", scenario_id, result.error)
            return result

    overlay.flow_start(plan, role=ctx.user.get("class"), scenario_id=scenario_id)
    reporter.capture_start()
    for index, step in enumerate(steps):
        if _stopping(session_name):
            # Stop has to bite BETWEEN STEPS, not merely between scenarios. A
            # forty-step flow whose remaining steps each time out at 30 s takes
            # twenty minutes to reach the next scenario boundary, and for all of
            # that time the GUI's Stop looks like it did nothing at all.
            result.status = ERROR
            result.error = "stopped at step %d of %d" % (index + 1, len(steps))
            log.warning("[%s] stopped at step %d of %d", scenario_id, index + 1, len(steps))
            break
        overlay.step_start(index)
        step_result = _run_step(adapter, index, step, overlay)
        result.steps.append(step_result)
        overlay.step_end(index, step_result.status, step_result.attempts, step_result.message)
        if step_result.status != PASS:
            if _stopping(session_name):
                # The window was closed under this step. Report what happened
                # rather than the CDP error that closing it produced.
                result.status = ERROR
                result.error = "stopped during step %d of %d" % (index + 1, len(steps))
                break
            result.status = step_result.status
            result.error = "%s(%s): %s" % (
                step.action, step.target or step.value or "", step_result.message)
            break
        reporter.capture_step()

    result.duration_ms = (time.time() - started) * 1000
    reporter.finalize(result, failed=result.status != PASS)
    passed = sum(1 for s in result.steps if s.status == PASS)
    overlay.flow_end(result.status, passed, len(steps))
    log.info("[%s] %s  (%d/%d steps, %.0f ms)", scenario_id, result.status.upper(),
             passed, len(result.steps), result.duration_ms)
    return result


# Steps that DO something to an element, as opposed to asserting about one. Only
# these get a marker: outlining every assertion target would strobe the page.
_MARKED_ACTIONS = ("click", "fill", "select", "press")


def _mark_label(step):
    """Short caption for the marker, e.g. `click` or `press Enter`."""
    if step.action == "press":
        return "press %s" % (step.value or "")
    if step.action in ("fill", "select") and step.value:
        value = str(step.value)
        return "%s %s" % (step.action, value if len(value) <= 24 else value[:21] + "...")
    return step.action


def _run_step(adapter, index, step, overlay=None):
    attempts, delay = 1, 0.0
    if step.retry:
        attempts = max(1, int(step.retry.get("attempts", 1)))
        delay = float(step.retry.get("delay", 0) or 0)
    is_assert = assertions.is_assertion(step.action)
    started = time.time()
    message, errored = "", False

    if overlay is not None and step.action in _MARKED_ACTIONS:
        # Mark BEFORE acting: a click can navigate away, and for `press` the
        # focused element is only knowable while it still has focus.
        overlay.mark(step.target if step.action != "press" else None,
                     _mark_label(step), timeout=step.timeout)

    for attempt in range(1, attempts + 1):
        try:
            if is_assert:
                ok, message = assertions.run_assertion(adapter, step)
                errored = False
            else:
                _do_action(adapter, step)
                ok, message, errored = True, "ok", False
        except Exception as exc:
            ok, message, errored = False, "%s: %s" % (type(exc).__name__, exc), True
        if ok:
            return StepResult(index, step.action, PASS, step.target, step.value,
                              message, (time.time() - started) * 1000, attempt)
        if attempt < attempts:
            if overlay is not None:
                overlay.retry(index, attempt + 1)
            if delay:
                time.sleep(delay)

    status = ERROR if errored else FAIL
    return StepResult(index, step.action, status, step.target, step.value,
                      message, (time.time() - started) * 1000, attempts)


def _do_action(adapter, step):
    action = step.action
    if action == "goto":
        adapter.goto(step.target, timeout=step.timeout)
    elif action == "fill":
        adapter.fill(step.target, step.value, timeout=step.timeout)
    elif action == "click":
        adapter.click(step.target, timeout=step.timeout)
    elif action == "select":
        adapter.select(step.target, step.value, timeout=step.timeout)
    elif action == "wait_for":
        adapter.wait_for(step.target, state=step.state or "visible", timeout=step.timeout)
    elif action == "press":
        adapter.press_key(step.value)
    elif action in services.IMPERATIVE:
        # No adapter: the GUI owns the service, so this is a request over the
        # control channel. A refusal raises - being unable to carry out an
        # instruction is an ERROR, where a service that simply never came up is
        # a FAIL through the wait actions in engine.assertions.
        ok, detail = services.request(action, step.target, timeout_ms=step.timeout)
        if not ok:
            # Bare: _run_scenario already prefixes the action and its target.
            raise ServiceError(detail)
    else:
        raise ValueError("no handler for action %r" % action)


def _print_summary(run):
    log.info("================ RUN SUMMARY ================")
    for flow in run.flows:
        detail = ("  (%s)" % flow.error) if flow.error else ""
        log.info("  %-6s %s / %s%s", flow.status.upper(), flow.session, flow.scenario, detail)
    passed = sum(1 for flow in run.flows if flow.ok)
    log.info("  %d/%d passed", passed, len(run.flows))
    log.info("=============================================")
    events.emit("run.summary", passed=passed, total=len(run.flows),
                exit_code=run.exit_code,
                flows=[{"session": f.session, "scenario": f.scenario,
                        "status": f.status, "error": f.error,
                        "duration_ms": round(f.duration_ms), "dir": f.artifacts_dir}
                       for f in run.flows])
