"""Putting the last cycle run back, after the application has been restarted.

A run is assembled in ``RunState`` from events arriving on a pipe, and a pipe
ends with the process that held it. So closing the window used to throw away
everything a cycle had reported - the colours on the graph, the output, the
stages - while the run's own directory sat on disk with the answer in it. Next
morning the page opened blank and the only account of what had happened
overnight was gone.

The core now writes every event it sends into the run's own directory as well
(``cycle/bus.py``'s ``JsonlObserver``). That file is the same JSONL the GUI
already reads from the launcher's stdout, which is the whole point of writing
it: replaying the run is feeding those lines to the same model, and there is no
second format for a restored run to be slightly wrong in.

Deliberately not a call into the core. The GUI mirrors the core's layout rather
than importing it (see ``core.py``), and it does not have to mirror anything
here: which directory the last run used is in the GUI's own history, and what
is in that directory is a stream it already understands.
"""

import collections
import json
import os

from . import history as history_mod

#: What the file is called inside a run's directory. The core's own
#: ``cycle/workspace.py`` decides this; mirrored rather than imported, like
#: every other path the GUI shares with the core.
LOG_NAME = os.path.join("logs", "cycle.jsonl")

#: How many events are read back at most. The model caps what it keeps anyway
#: (``runner.CYCLE_STAGES`` and the log limits beside it), so reading more only
#: costs the parse - and a run that emitted a hundred thousand events must not
#: be something the window waits for on the way up. The first line is always
#: kept whatever this is: it is ``cycle.run.start``, which carries the graph
#: every other event is about.
MAX_EVENTS = 20000


def log_path(run_dir):
    """Where a run's copy of its own event stream is."""
    return os.path.join(run_dir or "", LOG_NAME)


def last_run_dir(history, cycle_id=""):
    """The newest cycle run's directory, or "" when there is none to read.

    The directory is recorded when the run says where it is writing rather than
    when it ends, so a run the application was killed during - which is the one
    somebody most wants back - is found here too.
    """
    for entry in (history.entries(history_mod.CYCLE) if history else []):
        if cycle_id and (entry.get("config") or {}).get("cycle") != cycle_id:
            continue
        where = entry.get("run_dir") or ""
        if where and os.path.isfile(log_path(where)):
            return where
    return ""


def restore_cycle(history, state, cycle_id):
    """Recover the selected cycle after somebody has run a different one."""
    if state.cycle_running or (state.cycle or {}).get("cycle") == cycle_id:
        return False
    where = last_run_dir(history, cycle_id)
    return bool(where and restore(where, state))


def events_of(run_dir, limit=MAX_EVENTS):
    """The run's events, oldest first. Anything unreadable is simply not there.

    The first line and the last ``limit`` of them, rather than the last
    ``limit``: the first is the run starting, and without it there is no graph,
    no list of steps and nothing for the rest to attach to.
    """
    try:
        with open(log_path(run_dir), encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            # Over the handle, so a long file is walked rather than held.
            rest = collections.deque(fh, maxlen=max(limit - 1, 1))
    except OSError:
        return []
    found = []
    for line in [first] + list(rest):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:            # a line half written when the core died
            continue
        if isinstance(event, dict) and str(event.get("kind", "")).startswith("cycle."):
            found.append(event)
    return found


def restore(run_dir, state, limit=MAX_EVENTS):
    """Replay ``run_dir`` into ``state``. True when there was a run to restore.

    The model's signals are held back while the events go in and sent once at
    the end: a page listening to them would otherwise repaint itself several
    thousand times to arrive at the one view the reader is about to see.
    """
    events = events_of(run_dir, limit)
    if not events:
        return False
    state.reset_cycle()
    state.blockSignals(True)
    try:
        for event in events:
            state.handle(event)
    finally:
        state.blockSignals(False)
    if state.cycle is None:
        return False
    # A run that never said it ended is one the application was killed during.
    # cycle_interrupted keeps the record and withdraws only the claim that it
    # is still going, which is what stops the page offering Stop for it.
    if state.cycle_running:
        state.cycle_interrupted()
    # Where the run wrote its files, so the Artifacts page can open them again
    # as well. Known from the history rather than from the stream: run.dir is
    # the launcher's event, not the cycle's, so it is not in this file.
    state.run_dir = run_dir
    state.run_dir_known.emit(run_dir)
    state.cycle_graph_known.emit((state.cycle or {}).get("graph") or {})
    state.cycle_subject_changed.emit()
    state.changed.emit()
    return True


def restore_last(history, state):
    """The newest run there is. Returns the cycle id restored, or "".

    Never raises: coming back to a page as it was is a courtesy, and a
    half-written file or a directory somebody has deleted must cost that
    courtesy and nothing else.
    """
    try:
        where = last_run_dir(history)
        if where and restore(where, state):
            return (state.cycle or {}).get("cycle", "")
    except Exception:                 # noqa: BLE001 - cosmetic state only
        pass
    return ""
