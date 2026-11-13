"""What the launcher said, kept for whoever asks after it was on screen (ADR-004 L.1).

The Run and Log pages show a run as it happens; an agent asks about it later,
through QCR: is the launcher busy and with what, what did it print since the
last look, and how did the run end - which step of which scenario failed, with
what message, and which files it left.

Nothing here changes what the launcher does. :class:`LauncherFeed` only reads
what the window already receives - the launcher's stderr lines and its JSONL
events - and keeps:

* a ring of the last :data:`KEEP` entries, each with a sequence number that only
  grows, so a reader asks for what came *after* the number it last saw and
  learns when older entries were dropped before it could read them;
* a digest of the current (or last) run: who started it, what it is, each
  scenario's status, ``passed/total``, the step that failed and the artifacts.

Entries are trimmed: an event keeps its kind, session and small fields; a step
tree or a page copy is left out (the scenario file says what the steps are).

No Qt here.
"""

import collections
import time

#: Entries kept; older ones are dropped (and a reader is told so).
KEEP = 5000
#: Longest text kept from one log line or event field.
TEXT = 2000
#: Event fields worth passing on, when small. Everything else stays on screen only.
EVENT_FIELDS = ("index", "status", "message", "attempts", "attempt", "scenario", "id", "role", "steps",
                "passed", "total", "exit_code", "dir", "files", "reason", "cursor", "last", "url", "user",
                "op", "service", "project", "error", "step", "run_id", "mode")


def label(argv):
    """A launcher's command line without the interpreter and the launcher's own
    path: what it was asked to do (``--run-tests=login --env=Shop ...``)."""
    args = [str(one) for one in argv or ()]
    for index, arg in enumerate(args):
        if arg.startswith("--"):
            return " ".join(args[index:])[:TEXT]
    return " ".join(args)[:TEXT]


def _small(value):
    if isinstance(value, str):
        return value[:TEXT]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        items = [_small(one) for one in list(value)[:100] if isinstance(one, (str, int, float, bool))]
        return items
    return None


def _leaves(tree, path=()):
    """(step index, label, the blocks it sits in) for each step of a ``flow.start`` tree."""
    if not isinstance(tree, dict):
        return
    if tree.get("kind") == "step":
        yield tree.get("step_index"), str(tree.get("label") or ""), path
        return
    label = str(tree.get("label") or "")
    for child in tree.get("children") or ():
        yield from _leaves(child, path + ((label,) if label else ()))


class LauncherFeed:
    def __init__(self, keep=KEEP, clock=time.time):
        self._entries = collections.deque(maxlen=keep)
        self._seq = 0
        self._clock = clock
        self._number = 0
        self.run = None              # the current or last run's digest
        self.running = False
        self._sessions = {}          # session -> (its scenario in the digest, step index -> (label, blocks))
        #: Called with no arguments after each new entry - what a waiting reader wakes on.
        self.listeners = []

    # -- what the window feeds in ---------------------------------------------------
    def begin(self, label, kind, by="person", execution_id="", history_entry=""):
        """A launcher started. ``by``: ``person`` or the QCR actor that asked."""
        self._number += 1
        self.running = True
        self.run = {"number": self._number, "label": str(label)[:TEXT], "kind": str(kind)[:32],
                    "by": str(by)[:256], "started_at": self._clock(), "ended_at": None, "exit_code": None,
                    "execution_id": str(execution_id or "")[:256], "history_entry": str(history_entry or "")[:256],
                    "run_dir": "", "scenarios": []}
        self._sessions = {}
        self._add({"type": "run", "run": self._number, "state": "started", "label": self.run["label"],
                   "by": self.run["by"]})

    def attach(self, execution_id="", by=None, history_entry=None):
        """The run turned out to be QCR's (it was asked for just before it started)."""
        if self.run is None:
            return
        if execution_id:
            self.run["execution_id"] = str(execution_id)[:256]
        if by:
            self.run["by"] = str(by)[:256]
        if history_entry:
            self.run["history_entry"] = str(history_entry)[:256]

    def log(self, record):
        record = record if isinstance(record, dict) else {}
        self._add({"type": "log", "level": str(record.get("level") or "")[:16],
                   "session": str(record.get("session") or "")[:256], "text": str(record.get("text") or "")[:TEXT]})

    def event(self, event):
        if not isinstance(event, dict):
            return
        kind = str(event.get("kind") or "")
        fields = {}
        for name in EVENT_FIELDS:
            if name in event:
                value = _small(event[name])
                if value is not None:
                    fields[name] = value
        self._add({"type": "event", "kind": kind[:64], "session": str(event.get("session") or "")[:256],
                   "fields": fields})
        if self.run is not None:
            self._digest(kind, event)

    def finish(self, exit_code):
        self.running = False
        if self.run is not None:
            self.run["ended_at"] = self._clock()
            self.run["exit_code"] = exit_code if isinstance(exit_code, int) else None
            for scenario in self.run["scenarios"]:
                if scenario["status"] == "running":
                    scenario["status"] = "ended"          # the launcher went before the scenario did
        self._add({"type": "run", "run": self._number, "state": "ended",
                   "exit_code": exit_code if isinstance(exit_code, int) else None})

    # -- the digest -------------------------------------------------------------------
    def _digest(self, kind, event):
        session = str(event.get("session") or "")
        if kind == "run.dir":
            self.run["run_dir"] = str(event.get("dir") or "")[:4000]
        elif kind == "flow.start":
            steps = {index: (label, list(path[1:])) for index, label, path in _leaves(event.get("tree"))}
            scenario = {"id": str(event.get("id") or event.get("scenario") or "?")[:256], "session": session[:256],
                        "status": "running"}
            if event.get("scenario"):
                scenario["name"] = str(event["scenario"])[:256]
            if isinstance(event.get("steps"), int):
                scenario["total"] = event["steps"]
            self.run["scenarios"].append(scenario)
            self._sessions[session] = (scenario, steps)
        elif kind == "step.end":
            scenario, steps = self._sessions.get(session, (None, {}))
            if scenario is None or event.get("status") in ("pass", "passed", "ok", "skip", "skipped"):
                return
            if "failed_step" in scenario:
                return                              # the first failure is the one that explains the rest
            index = event.get("index")
            label, blocks = steps.get(index, ("", []))
            scenario["failed_step"] = {"index": index if isinstance(index, int) else -1,
                                       "label": label[:500], "blocks": [one[:256] for one in blocks][:20],
                                       "status": str(event.get("status") or "")[:32],
                                       "message": str(event.get("message") or "")[:TEXT],
                                       "attempts": event.get("attempts") if isinstance(event.get("attempts"), int)
                                       else 1}
        elif kind == "flow.end":
            scenario, _steps = self._sessions.get(session, (None, {}))
            if scenario is None:
                return
            scenario["status"] = str(event.get("status") or "")[:32]
            for name in ("passed", "total"):
                if isinstance(event.get(name), int):
                    scenario[name] = event[name]
        elif kind == "artifacts.written":
            scenario, _steps = self._sessions.get(session, (None, {}))
            target = scenario if scenario is not None else self.run
            target["artifacts"] = {"dir": str(event.get("dir") or "")[:4000],
                                   "files": [str(one)[:256] for one in (event.get("files") or ())][:200]}

    # -- what a reader gets -------------------------------------------------------------
    def _add(self, entry):
        self._seq += 1
        entry = dict(entry, seq=self._seq, at=round(self._clock(), 3))
        self._entries.append(entry)
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                pass

    @property
    def last_seq(self):
        return self._seq

    def read(self, after=0, limit=200):
        """Entries with a sequence number above ``after``, at most ``limit``.
        ``next`` is what to pass as ``after`` next time; ``dropped`` says older
        entries went before they could be read."""
        limit = max(1, min(int(limit), 500))
        after = max(0, int(after))
        if after > self._seq:
            after = 0               # a number from before QAVector restarted: from the start of what is kept
        first = self._entries[0]["seq"] if self._entries else self._seq + 1
        entries = [entry for entry in self._entries if entry["seq"] > after][:limit]
        return {"entries": entries, "next": entries[-1]["seq"] if entries else after,
                "dropped": bool(self._entries) and after + 1 < first, "running": self.running}

    def has_after(self, after):
        return self._seq > after

    def status(self):
        return {"running": self.running, "run": self.summary()}

    def summary(self):
        """The current or last run, as QCR reports it (copied: the caller may keep it)."""
        if self.run is None:
            return None
        run = dict(self.run, scenarios=[dict(one) for one in self.run["scenarios"]])
        return run
