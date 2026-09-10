"""Structured event stream (JSONL) for out-of-process consumers, e.g. the GUI.

``--events=TARGET`` turns this on: ``-`` writes to **stdout**, anything else is
taken as a file path. Stdout is the natural channel because the launcher's own
logging goes to *stderr* (``logging.basicConfig``'s default) and the only prints
are the exit-immediately paths (``--help`` / ``--version`` / ``--extensions=list``),
so a consumer reads events and human log lines as two clean streams - no temp
files, no ports, no parsing of log text.

One event per line::

    {"ts": 1723545600.12, "seq": 7, "kind": "step.end", "session": "dev-agent", ...}

Two things feed it:

* :func:`emit` - called directly from the launcher and the runner for lifecycle
  events (windows launched, CDP attached, run finished).
* :class:`EventObserver` - implements the *same* observer protocol as
  :class:`engine.overlay.NullOverlay`, so the runner notifies it at every
  execution transition without knowing it exists. :class:`Tee` fans one call out
  to the in-page HUD and this observer at once.

Nothing here may raise into a run: a closed pipe (the consumer went away) or an
unserializable payload costs at most a dropped line.
"""

import json
import os
import sys
import threading
import time

# Module-level, like logging's own configuration: the launcher configures the
# sink once in main() and everything downstream - including runner threads -
# emits through it. The lock keeps lines from parallel sessions whole.
_lock = threading.Lock()
_stream = None          # file object to write to; None = disabled
_own_stream = False     # True when we opened it (a path) and so must close it
_seq = 0


def configure(target):
    """Point the stream at ``target``; returns True when events are enabled.

    ``target`` is ``"-"`` (stdout), a file path, or None/"" (disabled). A path
    that cannot be opened disables the stream rather than failing the launch:
    diagnostics must never be the reason a run does not happen.
    """
    global _stream, _own_stream, _seq
    close()
    if not target:
        return False
    _seq = 0
    if target == "-":
        _stream, _own_stream = sys.stdout, False
        return True
    try:
        # Line buffered, so a consumer tailing the file sees each event as it
        # happens rather than in 8 KB bursts.
        _stream = open(target, "a", encoding="utf-8", buffering=1)
        _own_stream = True
        return True
    except OSError as exc:
        print("--events: cannot write %s (%s); events disabled." % (target, exc),
              file=sys.stderr)
        _stream, _own_stream = None, False
        return False


def enabled():
    """True when a sink is configured (cheap enough to guard payload building)."""
    return _stream is not None


def close():
    """Close a file sink; no-op for stdout and when already disabled."""
    global _stream, _own_stream
    with _lock:
        if _stream is not None and _own_stream:
            try:
                _stream.close()
            except OSError:
                pass
        _stream, _own_stream = None, False


def emit(kind, **fields):
    """Write one event line. Silent no-op when events are disabled."""
    global _seq
    if _stream is None:
        return
    with _lock:
        if _stream is None:      # closed between the check and the lock
            return
        _seq += 1
        event = {"ts": round(time.time(), 3), "seq": _seq, "kind": kind}
        event.update(fields)
        try:
            # default=str: one odd value (a Path, an exception) degrades to its
            # string form instead of sinking the whole event.
            _stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            _stream.flush()
        except Exception:        # broken pipe, closed file, encoding error
            pass


def emit_artifacts(out_dir, scenario=None, session=None):
    """Announce the files that now exist in a scenario's report directory."""
    if _stream is None:
        return
    try:
        names = sorted(os.listdir(out_dir))
    except OSError:
        return
    emit("artifacts.written", session=session, scenario=scenario,
         dir=out_dir, files=names)


class EventObserver:
    """Mirrors the overlay hook protocol, emitting each transition as an event.

    One instance per driven window: ``session`` tags every line, because several
    sessions run on parallel threads and their events interleave on one stream.
    """

    enabled = True

    def __init__(self, session=None):
        self._session = session

    def _emit(self, kind, **fields):
        emit(kind, session=self._session, **fields)

    def session_start(self, scenario_ids):
        self._emit("session.start", scenarios=list(scenario_ids))

    def flow_start(self, root, role=None, scenario_id=None):
        # root.to_dict() is the very tree the in-page HUD draws, so a consumer
        # can render an identical step tree with no extra plumbing. ``scenario``
        # is what the flow is called; ``id`` is what session.start listed it as,
        # which is the only thing a consumer can match the two up by.
        self._emit("flow.start", scenario=root.label, id=scenario_id, role=role,
                   tree=root.to_dict(), steps=sum(1 for _ in root.leaves()))

    def step_start(self, index):
        self._emit("step.start", index=index)

    def step_end(self, index, status, attempt=1, message=""):
        self._emit("step.end", index=index, status=status, attempts=attempt,
                   message=message)

    def mark(self, selector, label=None, timeout=None):
        # Purely a visual affordance of the in-page HUD; nothing to report.
        pass

    def retry(self, index, attempt):
        self._emit("step.retry", index=index, attempt=attempt)

    def log(self, level, message):
        # Log records already reach the consumer on stderr; forwarding them here
        # would duplicate every line on the other channel.
        pass

    def flow_end(self, status, passed, total):
        self._emit("flow.end", status=status, passed=passed, total=total)

    def teardown(self):
        pass


class Tee:
    """Fans every observer hook out to several observers, in order.

    Lets the runner notify the in-page HUD and the event stream from its single
    ``overlay.*`` call site. One observer raising must not stop the others: the
    HUD talks to a live browser page, which can vanish mid-run.
    """

    def __init__(self, observers):
        self._observers = [o for o in observers if o is not None]

    @property
    def enabled(self):
        return any(getattr(o, "enabled", False) for o in self._observers)

    def _fan(self, name, *args, **kwargs):
        for observer in self._observers:
            try:
                getattr(observer, name)(*args, **kwargs)
            except Exception:   # observers are diagnostics; never break a run
                pass

    def session_start(self, scenario_ids):
        self._fan("session_start", scenario_ids)

    def flow_start(self, root, role=None, scenario_id=None):
        self._fan("flow_start", root, role=role, scenario_id=scenario_id)

    def step_start(self, index):
        self._fan("step_start", index)

    def step_end(self, index, status, attempt=1, message=""):
        self._fan("step_end", index, status, attempt, message)

    def mark(self, selector, label=None, timeout=None):
        self._fan("mark", selector, label, timeout)

    def retry(self, index, attempt):
        self._fan("retry", index, attempt)

    def log(self, level, message):
        self._fan("log", level, message)

    def flow_end(self, status, passed, total):
        self._fan("flow_end", status, passed, total)

    def teardown(self):
        self._fan("teardown")
