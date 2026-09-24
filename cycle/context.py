"""What a plugin is handed: the run, its workspace, and how it is told to stop.

A plugin never reaches for global state. Everything it is allowed to know about
the run it is part of arrives as a :class:`RunContext` - which run this is,
where to put files, what the steps before it produced, how to say something, and
whether it should still be going. That is what makes a plugin testable without
a run and replaceable without the engine knowing.

:class:`CancelToken` is the only way anything gets stopped. Python cannot kill a
thread, so stopping is cooperative by construction, not by choice: a token is
set, and whatever is blocking either waits on the token or was given a deadline
short enough to notice. Every built-in plugin does one or the other. A plugin
that ignores its token runs to completion and the run reports that it did,
rather than pretending to have stopped it.
"""

import os
import threading


class CancelToken(object):
    """A thread-safe "stop what you are doing", with parents.

    A run has one; each step gets a child of it. Setting the run's token fires
    every step's; setting one step's leaves the others alone. That is exactly
    the two things that have to be possible - stop everything, and stop this one
    because its deadline passed - and nothing else has to be modelled.

    ``reason`` travels with it so a step that stops can say *why* it stopped,
    which is the difference between a useful run record and a page of
    "cancelled".
    """

    def __init__(self, parent=None, reason=""):
        self._event = threading.Event()
        self._parent = parent
        self._reason = reason

    def set(self, reason=""):
        """Ask whatever is holding this to stop. Idempotent."""
        if reason and not self._reason:
            self._reason = reason
        self._event.set()

    def is_set(self):
        return self._event.is_set() or (self._parent is not None
                                        and self._parent.is_set())

    @property
    def reason(self):
        if self._reason:
            return self._reason
        return self._parent.reason if self._parent is not None else ""

    def child(self, reason=""):
        """A token that fires when it is set, or when this one is."""
        return CancelToken(parent=self, reason=reason)

    def wait(self, timeout=None):
        """Block until this is set or ``timeout`` passes. True when it was set.

        What a plugin waits on instead of ``time.sleep``, so a cancelled run
        does not have to sit through the rest of a retry delay. A parent being
        set has to be noticed too, and an Event cannot wait on two things at
        once, so a token with a parent polls on a short tick. The tick is small
        enough to feel immediate and large enough not to matter.
        """
        if self._parent is None:
            return self._event.wait(timeout)
        deadline = None if timeout is None else _now() + timeout
        while True:
            if self.is_set():
                return True
            remaining = None if deadline is None else deadline - _now()
            if remaining is not None and remaining <= 0:
                return self.is_set()
            slice_ = _POLL if remaining is None else min(_POLL, remaining)
            if self._event.wait(slice_):
                return True

    def raise_if_set(self):
        """For a plugin that checks between units of work rather than waiting."""
        if self.is_set():
            raise Cancelled(self.reason or "the run was cancelled")


#: How often a token with a parent looks up. Small enough that stopping feels
#: immediate, large enough that a waiting step is not a busy loop.
_POLL = 0.05


def _now():
    import time
    return time.monotonic()


class Cancelled(Exception):
    """Raised by :meth:`CancelToken.raise_if_set` when a run is stopping."""


class RunContext(object):
    """One run, as a plugin sees it.

    Deliberately small. A plugin gets where to put things, what came before,
    somewhere to say something, and a way to be told to stop. It does not get
    the executor, the other steps' plugins, or any way to change the graph -
    a step that could rewrite the run it is part of would make a run
    unreadable afterwards.
    """

    def __init__(self, run, cycle, workspace, cancel=None, observer=None,
                 flows_dir=None, cycles_dir=None, environment=None,
                 memory_path=""):
        self.run = run
        self.cycle = cycle
        self.workspace = workspace
        self.cancel = cancel or CancelToken()
        self.observer = observer
        self.flows_dir = flows_dir
        self.cycles_dir = cycles_dir
        #: Where what the project remembers between runs is kept. Carried
        #: rather than looked up, for the reason the secrets path is: the
        #: launcher was told where it is and a plugin should not be guessing
        #: at a second answer. Blank means the default beside the user's data.
        self.memory_path = memory_path or ""
        self.environment = dict(os.environ if environment is None else environment)
        #: What each step this run is doing again did last time: step id ->
        #: (run id, StepRun). Filled from the run being resumed, or from the
        #: run a partial one reuses. Read-only history - a step that spent an
        #: hour and failed on a setting somebody has since fixed can look at
        #: what it left behind instead of paying for all of it again.
        self.earlier = {}
        self._held = []                       # [(step_id, plugin, handle)]
        self._lock = threading.Lock()

    # -- one step's view ------------------------------------------------------
    def for_step(self, token):
        """This context, but with ``cancel`` being one step's own token.

        A plugin reads ``context.cancel``, and the context is shared by every
        step running at once - so without this a step's own deadline would set
        a token nothing was watching, and only a cancellation of the whole run
        would ever reach a plugin. A shallow view rather than a copy, because
        everything else about the run genuinely is shared: the workspace, the
        record of what came before, the observer, and what is being held open.
        """
        return _StepView(self, token)

    def earlier_of(self, step_id):
        """``(run id, StepRun)`` for this step's last attempt, or ``(None, None)``."""
        return self.earlier.get(step_id, (None, None))

    # -- where things go ------------------------------------------------------
    @property
    def run_id(self):
        return getattr(self.run, "id", "")

    def step_dir(self, step_id, *parts):
        """A step's own directory under the run workspace, created on demand."""
        path = os.path.join(self.workspace, "steps", step_id, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def artifacts_dir(self):
        path = os.path.join(self.workspace, "artifacts")
        os.makedirs(path, exist_ok=True)
        return path

    def relative(self, path):
        """``path`` as it should be recorded: relative to the workspace.

        A run directory has to stay meaningful after it is zipped and opened on
        another machine, so nothing stored in the record is absolute. A path
        outside the workspace comes back untouched, because making it relative
        would produce a string of ``..`` that means less than the original.
        """
        try:
            relative = os.path.relpath(os.path.abspath(path), self.workspace)
        except ValueError:                    # different drive on Windows
            return path
        return path if relative.startswith(os.pardir) else relative

    # -- what came before -----------------------------------------------------
    def outputs_of(self, step_id):
        """What a finished step produced, or ``{}``."""
        step_run = (self.run.steps or {}).get(step_id) if self.run else None
        return dict(step_run.outputs) if step_run else {}

    def status_of(self, step_id):
        step_run = (self.run.steps or {}).get(step_id) if self.run else None
        return step_run.status if step_run else ""

    def started_at(self, step_id):
        """When a step first began, in epoch seconds, or ``None``.

        The word that matters is **first**. A step that asked to wait and was
        come back to keeps this from its original start, which is what lets a
        plugin work out how long it has been going without keeping any state
        of its own - there is none to keep, since each turn is a fresh call.
        """
        step_run = (self.run.steps or {}).get(step_id) if self.run else None
        return step_run.started_at if step_run else None

    # -- saying something -----------------------------------------------------
    def log(self, step_id, stream, lines):
        """Hand output to whoever is watching. Never raises into a plugin."""
        if self.observer is None:
            return
        rows = [lines] if isinstance(lines, str) else list(lines)
        if not rows:
            return
        try:
            self.observer.step_log(step_id, stream, rows)
        except Exception:                     # observers are diagnostics
            pass

    def stage(self, step_id, one):
        """Hand one stage of a step's inner work over. Never raises.

        Separate from :meth:`log` because it is not output: a stage is a row
        somebody reads - what the step is doing now - while the log is the text
        the process printed. A plugin whose work has visible phases emits these;
        every other plugin emits none and nothing downstream notices.
        """
        if self.observer is None or not one:
            return
        try:
            self.observer.step_stage(step_id, one)
        except Exception:                     # observers are diagnostics
            pass

    # -- holding something open ----------------------------------------------
    def hold(self, step_id, plugin, handle):
        """Register something that must be stopped when the run ends.

        The run stops these in reverse order, so a service started after the
        database it needs is stopped before it.
        """
        with self._lock:
            self._held.append((step_id, plugin, handle))

    def held(self):
        """What is being held, newest first - the order it should be released."""
        with self._lock:
            return list(reversed(self._held))

    def release(self, step_id):
        """Forget what a step was holding, without stopping it.

        For a step that stopped its own thing - ``service.stop`` after
        ``service.start`` - so the cleanup pass does not try again and report a
        failure for something that already went as asked.
        """
        with self._lock:
            self._held = [entry for entry in self._held if entry[0] != step_id]


class _StepView(object):
    """One step's view of the run: everything shared, but its own cancel token.

    Deliberately a proxy and not a subclass or a copy. A copy would give each
    step its own idea of what is being held open, which is the one thing that
    has to be the run's; a subclass would have to keep its parent's fields in
    step. Attribute lookup falls through, so a plugin cannot tell the
    difference and nothing here has to be updated when RunContext grows.
    """

    def __init__(self, context, cancel):
        # Set through __dict__ so __setattr__ does not send them to the parent.
        self.__dict__["_context"] = context
        self.__dict__["cancel"] = cancel

    def __getattr__(self, name):
        return getattr(self._context, name)

    def __setattr__(self, name, value):
        if name == "cancel":
            self.__dict__["cancel"] = value
        else:
            setattr(self._context, name, value)
