"""Pausing a scenario between its steps, and moving over the steps it has taken.

**Positions, not steps.** Position ``0`` is the page before the first step and
position ``n`` the page after step ``n`` (counted from 1, as people count).
``last`` is how many steps the live page has been through; the ``cursor`` is the
position somebody is looking at - ``last``, the live page, unless they stepped
back. Every position has a copy of its page while the run lives
(engine.history), which is what stepping back shows.

**What can be asked**, by the GUI over ``--control`` or by the HUD in the page:

* ``pause`` - stop at the next boundary between steps. A step that is running
  is never cut short, just as Stop does not cut one short.
* ``continue`` - go on. From the live page that is the next step; after
  stepping back to ``N`` it is step ``N+1``, run against the live page as it is.
* ``step`` - go on for one step, then stop again.
* ``back`` / ``forward`` / ``cursor`` - move the cursor. ``forward`` on the live
  page is a step. Asked while the scenario runs, they pause it first.
* ``breakpoints`` - the steps of a scenario to stop before, by index.
* ``keys`` - the keys the HUD answers to, so they are the GUI's keys.

**Going back undoes nothing.** Continuing from ``N`` runs ``N+1`` onwards on a
page that has already been through those steps once. That was the person's
decision (2026-11-09), knowing it can repeat what the steps did, and it is why
the runner writes every such return into the result as an intervention: a pass
that needed one is not the same as a pass.

**Threads.** Commands arrive on the launcher's control thread and are only
queued there. Everything else happens on the session's own thread, the one
Playwright is bound to - the wait too, which goes through the adapter so the
page keeps receiving its events while nobody drives it.

**Nobody to ask, nothing to wait for.** Without a control channel
(:func:`configure`) a gate never pauses and keeps no history: a run in the
background, from a cycle or from a terminal goes exactly as it always did.
"""

import collections
import logging
import threading

log = logging.getLogger("flowengine.stepping")

#: How often a paused scenario looks for what it has been asked.
POLL_S = 0.15

COMMANDS = ("pause", "continue", "step", "back", "forward", "cursor", "breakpoints", "keys")

#: The keys the HUD answers to until the GUI says otherwise (``keys``), in the
#: GUI's own spelling - see engine.overlay.hud_keys. F8 and F10 are what a
#: browser's own debugger uses for the same two things.
DEFAULT_KEYS = {"pause": "F8", "step": "F10", "back": "Alt+Left", "forward": "Alt+Right"}
_PREFIX = "scenario."

_lock = threading.Lock()
_enabled = False
_gates = {}           # session name -> the StepGate driving it now


def configure(enabled):
    """Whether this run has somebody to pause for: a control channel."""
    global _enabled
    with _lock:
        _enabled = bool(enabled)
    return _enabled


def enabled():
    with _lock:
        return _enabled


def deliver(message):
    """Hand a ``scenario.*`` command to the session it names.

    Called on the control thread. Answers "" when it was taken, otherwise why
    not - for the launcher to pass back rather than to lose.
    """
    command = str(message.get("command") or "")
    if command.startswith(_PREFIX):
        command = command[len(_PREFIX):]
    if command not in COMMANDS:
        return "unknown command %r" % command
    session = message.get("session")
    with _lock:
        gate = _gates.get(session)
    if gate is None:
        return "no scenario is running in %s" % (session or "that window")
    return gate.put(dict(message, command=command))


class Options:
    """What the run asked of stepping: where the history goes - an
    engine.history.RunHistory, or None for no copies - and whether a failed
    step pauses instead of ending its scenario."""

    def __init__(self, history=None, pause_on_failure=False):
        self.history = history
        self.pause_on_failure = bool(pause_on_failure)


class StepGate:
    """One window's way through its scenarios' steps. See the module."""

    def __init__(self, session, adapter, overlay, options=None, stopping=None,
                 locked=False, live=None):
        self.session = session
        self._adapter = adapter
        self._overlay = overlay
        self._options = options or Options()
        self._stopping = stopping or (lambda: False)
        #: Whether the window is held by --lock-windows: let go while paused,
        #: or neither the HUD nor its keys could be used.
        self.locked = bool(locked)
        self.live = enabled() if live is None else bool(live)
        self._queue = collections.deque()
        self._queue_lock = threading.Lock()
        self._breakpoints = {}         # scenario id -> {step index}
        self.keys = dict(DEFAULT_KEYS)
        self.scenario = None
        self._copies = None
        self._settle = None
        self.cursor = self.last = 0
        self.paused = False
        self.reason = ""
        self._wanted = False           # pause at the next boundary
        self._once = False             # one step, then pause
        self._deferred = []            # moves asked for while running

    @classmethod
    def off(cls):
        """A gate that never stops anything - for a scenario run on its own."""
        from engine.overlay import NullOverlay
        return cls(None, None, NullOverlay(), live=False)

    # -- the session's lifetime -------------------------------------------------
    def __enter__(self):
        if self.live:
            with _lock:
                _gates[self.session] = self
        return self

    def __exit__(self, *_exc):
        with _lock:
            if _gates.get(self.session) is self:
                del _gates[self.session]
        self.finish()

    def put(self, message):
        """Queue a command for the session's thread. Control thread only."""
        with self._queue_lock:
            self._queue.append(message)
        return ""

    # -- a scenario ---------------------------------------------------------------
    def start(self, scenario_id, settle=None):
        """A scenario begins: no steps taken, a copy of the page as it starts."""
        self.finish()
        self.scenario = scenario_id
        self._settle = settle
        self.cursor = self.last = 0
        self._once = False
        history = self._options.history
        if self.live and history is not None:
            self._copies = history.scenario(self.session, scenario_id)
        self._setup()
        self._copy(0)

    def finish(self):
        """The scenario is over: nobody can step back into it, so its copies go."""
        if self._copies is not None:
            self._copies.close()
            self._copies = None
        self.scenario = None

    def before_step(self, index):
        """Before step ``index`` (0-based) runs: the index to run now - this
        one, or an earlier one to run again - or None to stop."""
        if not self.live:
            return index
        self._take()
        if index in self._breakpoints.get(self.scenario, ()):
            reason = "breakpoint"
        elif self._once:
            reason = "step"
        elif self._wanted:
            reason = "pause"
        else:
            return index
        return self._hold(reason)

    def after_step(self, index):
        """Step ``index`` has run, whatever came of it: copy the page it left."""
        if not self.live:
            return
        self.last = self.cursor = index + 1
        self._copy(index + 1)

    def after_failure(self, index):
        """Step ``index`` failed. None ends the scenario, as a failure always
        has; an index - with pause on failure, after the person stepped back -
        is where to run again from."""
        if not (self.live and self._options.pause_on_failure):
            return None
        again = self._hold("failure")
        # Going on from the failed step's own page has nothing left to run.
        if again is None or again >= index + 1:
            return None
        return again

    # -- held ---------------------------------------------------------------------
    def _hold(self, reason):
        self.paused, self.reason = True, reason
        self._wanted = self._once = False
        moves, self._deferred = self._deferred, []
        for move in moves:
            self._apply(move)
        unlocked = self.locked and self._adapter.lock_input(False)
        self._announce()
        log.info("Paused %s step %d of %s (%s)", "after" if self.last else "before",
                 self.last or 1, self.scenario, reason)
        go = None
        try:
            while go is None:
                if self._stopping():
                    return None
                go = self._take()
                if go is None:
                    self._adapter.idle(POLL_S)
        finally:
            self.paused = False
            if unlocked:
                self._adapter.lock_input(True)
        if go == "step":
            self._once = True
        resume = self.cursor
        if resume < self.last:
            log.warning("Going on from step %d: steps %d-%d run again on the page as "
                        "it is now", resume, resume + 1, self.last)
            if self._copies is not None:
                self._copies.forget_from(resume + 1)
            self.last = resume
        self._overlay.step_resumed(resume)
        return resume

    def _take(self):
        """Carry out what was asked since the last look. 'continue' or 'step'
        when told to go on; whatever came after that waits for the next look."""
        with self._queue_lock:
            asked = list(self._queue)
            self._queue.clear()
        if self._adapter is not None:
            asked.extend(self._adapter.overlay_commands())
        for position, message in enumerate(asked):
            go = self._apply(message)
            if go:
                rest = asked[position + 1:]
                if rest:
                    with self._queue_lock:
                        self._queue.extendleft(reversed(rest))
                return go
        return None

    def _apply(self, message):
        command = message.get("command")
        if command == "pause":
            self._wanted = True
        elif command == "continue":
            if self.paused:
                return "continue"
            self._wanted = self._once = False
        elif command == "step":
            if self.paused:
                return "step"
            self._wanted = True
        elif command in ("back", "forward", "cursor"):
            if not self.paused:
                # Forward has nowhere to go while the run is at the live page.
                if command != "forward":
                    self._wanted = True
                    self._deferred.append(message)
                return None
            if command == "forward" and self.cursor >= self.last:
                return "step"
            self._move({"back": self.cursor - 1,
                        "forward": self.cursor + 1}.get(command, message.get("index")))
        elif command == "breakpoints":
            indices = set()
            for value in message.get("indices") or ():
                try:
                    indices.add(int(value))
                except (TypeError, ValueError):
                    pass
            self._breakpoints[message.get("scenario") or self.scenario] = indices
            self._setup()
        elif command == "keys":
            self.keys = dict(DEFAULT_KEYS, **{name: str(keys) for name, keys in
                                              (message.get("keys") or {}).items()
                                              if name in DEFAULT_KEYS})
            self._setup()
        return None

    def _setup(self):
        """Tell the HUD and the GUI what stepping can do here now: the same
        breakpoints on both, whichever of them set one."""
        if self.scenario is None:
            return
        self._overlay.step_setup(self.scenario, self.live,
                                 sorted(self._breakpoints.get(self.scenario, ())),
                                 dict(self.keys))

    def _move(self, target):
        try:
            target = int(target)
        except (TypeError, ValueError):
            return
        target = max(0, min(self.last, target))
        if target != self.cursor:
            self.cursor = target
            self._announce()

    def _announce(self):
        """Say where the cursor is, with the copy to show there - None on the
        live page, or for a step that left no copy."""
        view = None
        if self.cursor < self.last and self._copies is not None:
            view = self._copies.view(self.cursor)
        self._overlay.step_paused(self.cursor, self.last, self.reason, view)

    def _copy(self, position):
        if self._copies is None:
            return
        if self._settle is not None:
            try:
                self._settle()
            except Exception:
                pass
        self._copies.capture(self._adapter, position)
