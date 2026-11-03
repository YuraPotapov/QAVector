"""Pausing a step where it stands: its processes held, and let go again.

Pause used to wait for every step in flight to finish, so a person who pressed
it during a five-minute agent turn got their pause five minutes later. A step
cannot simply be stopped and done again on Continue either: a call that was
cut off has an uncertain outcome, and Resume refuses to repeat it (see
:mod:`cycle.operations`). So the step is held instead - SIGSTOP to every
process it started, SIGCONT on Continue - and carries on from the same point,
with the same operation, as though nothing had happened.

What is held is what a step started through :func:`cycle.restrict.popen`, and
the scenario launcher: each registers itself with :func:`track`, from the
worker thread the executor runs the step on (:func:`enter`). The whole tree
under each is held, not just its process group: a browser or a sandbox may
start a session of its own.

Installed plugin workers register Runtime-owned pause controllers. These hold
the native namespace and brokered command jobs, and suspend their deadline
clocks. Native process identities and lifecycle stay with Runtime.

Not held, by design:

* work done inside this process - a Jira call, a question to a person. It is
  short, or it is waiting already, and it finishes on its own.
* native adapters that have not opted into a Runtime pause controller,
  including the separate provider bridge; they finish on their own.

Linux only: the tree is read from ``/proc``. Elsewhere :data:`SUPPORTED` is
False and Pause waits for the steps in flight, as it always did.
"""

import atexit
import os
import signal
import threading

SUPPORTED = os.name == "posix" and os.path.isdir("/proc/self/task")

#: How many times the tree is walked while holding it. A process may start a
#: child between the walk and the signal; the next walk finds it.
PASSES = 5

_local = threading.local()


def enter(steps, step_id):
    """Say which step this worker thread runs. Returns what leave() restores."""
    previous = getattr(_local, "current", None)
    _local.current = (steps, step_id)
    return previous


def leave(previous):
    _local.current = previous


def track(process):
    """Register a process the current step started. A no-op outside a step."""
    current = getattr(_local, "current", None)
    if current is not None and process is not None:
        current[0].add(current[1], process.pid)
    return process


def track_controller(controller):
    """Opt in a Runtime-owned group; native process IDs remain with Runtime."""
    current = getattr(_local, 'current', None)
    if current is not None:
        current[0].add_controller(current[1], controller)
    return controller


def _start_time(pid):
    """The process's start time, so a reused pid is never mistaken for it."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    # The command name is in parentheses and may itself hold spaces.
    fields = raw[raw.rfind(")") + 2:].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _children():
    """parent pid -> [child pids], for every process this user can see."""
    found = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as handle:
                raw = handle.read().decode("utf-8", "replace")
            parent = int(raw[raw.rfind(")") + 2:].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        found.setdefault(parent, []).append(int(name))
    return found


def _tree(root, children):
    found, queue = [], [root]
    while queue:
        pid = queue.pop()
        found.append(pid)
        queue.extend(children.get(pid, ()))
    return found


def _signal(pid, number):
    try:
        os.kill(pid, number)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class Steps:
    """The processes each step in flight started, and whether they are held."""

    def __init__(self):
        self._lock = threading.Lock()
        self._roots = {}          # step id -> {pid: start time}
        self._held = {}           # pid -> start time, every process we stopped
        self._controllers = {}
        self.holding = False
        atexit.register(self.release)

    def add(self, step_id, pid):
        started = _start_time(pid) if SUPPORTED else None
        with self._lock:
            self._roots.setdefault(step_id, {})[pid] = started
            if self.holding:
                # Started in the moment the step was being held: it is held too.
                self._hold_tree(pid, started)

    def forget(self, step_id):
        """The step finished. What it started is no longer its to hold."""
        with self._lock:
            self._roots.pop(step_id, None)
            self._controllers.pop(step_id, None)

    def add_controller(self, step_id, controller):
        with self._lock:
            self._controllers.setdefault(step_id, []).append(controller)
            if self.holding:
                controller.pause()

    def hold(self):
        """Stop every process the steps in flight started. Returns those steps."""
        if not SUPPORTED:
            return []
        with self._lock:
            self.holding = True
            held = []
            for step_id, controllers in self._controllers.items():
                for controller in controllers:
                    controller.pause()
                if any(controller.paused for controller in controllers):
                    held.append(step_id)
            for step_id, roots in self._roots.items():
                # Every root must be visited: any(generator) would stop after
                # the first success and leave the rest of this step running.
                stopped = [self._hold_tree(pid, started) for pid, started in roots.items()]
                if any(stopped):
                    held.append(step_id)
            return sorted(set(held))

    def release(self):
        """Let everything held go on. Safe to call when nothing is held."""
        with self._lock:
            self.holding = False
            held, self._held = self._held, {}
            for controllers in self._controllers.values():
                for controller in controllers:
                    controller.release()
        for pid, started in held.items():
            if started is not None and _start_time(pid) == started:
                _signal(pid, signal.SIGCONT)

    def held_steps(self):
        """Include processes registered after Pause first took its snapshot."""
        with self._lock:
            native = {step_id for step_id, controllers in self._controllers.items()
                      if any(controller.paused for controller in controllers)}
            return sorted(native | {step_id for step_id, roots in self._roots.items()
                          if any(started is not None and self._held.get(pid) == started
                                 for pid, started in roots.items())})

    def _hold_tree(self, root, started):
        """Stop ``root`` and everything under it. False when it is gone."""
        if started is None or _start_time(root) != started:
            return False
        for _ in range(PASSES):
            fresh = [pid for pid in _tree(root, _children()) if pid not in self._held]
            if not fresh:
                break
            for pid in fresh:
                when = _start_time(pid)
                if when is not None and _signal(pid, signal.SIGSTOP):
                    self._held[pid] = when
        return root in self._held
