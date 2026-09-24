"""Writing a run down, and reading it back.

A run's record is **one JSON file in the run's own directory**, plus one index
listing the runs. Not SQLite, and the reason is not the dependency - ``sqlite3``
is in the standard library. It is that the GUI would have to become a second
reader of that database, and the GUI's whole architecture is "PySide6 and
nothing else, ask the core over a pipe". A database would mean either a second
owner of the schema living in the GUI, or a query command per question - which
is a JSON API over a database, so the JSON could have been written directly.

What the design document actually wants from a run record - isolation, cleanup,
reproducibility, debugging, a report - is served by a directory that can be
zipped and attached to a bug report. The one query anyone has is "the last N
runs, newest first", and that is a read of one small file. The GUI's own
``history.py`` is already exactly this shape and has been fine.

Revisit when run counts reach the thousands, or when something wants to
aggregate across runs - analytics over time, say. Until then this is recorded
here so the decision is not re-litigated by whoever reads the code next.

``metadata.json`` is written **as the run goes**, not only at the end: a process
that is killed then leaves a readable partial record saying which steps had
finished, which is honest and is exactly the case somebody is investigating.
"""

import json
import os
import time
from dataclasses import asdict

from cycle import workspace as workspace_mod
from cycle.bus import NullObserver
from domain.cycle import (CANCELLED, FAILED, PENDING, RUNNING, SKIPPED,
                          TIMEOUT, WAITING, Artifact, CycleRun, StepRun)

#: The record's own version. Present from the first release so a reader can
#: tell a format it does not know from a file it cannot parse.
SCHEMA = 1

METADATA = "metadata.json"
INDEX = "index.json"

#: How many runs the index keeps. The directories are left alone - deleting
#: someone's artifacts because a list got long would be a surprise - so this
#: only bounds what is listed quickly.
MAX_INDEXED = 500


def metadata_path(workspace):
    return os.path.join(workspace, METADATA)


def index_path(root=None):
    return os.path.join(workspace_mod.runs_root(root), INDEX)


# -- writing ------------------------------------------------------------------
def to_document(run):
    """The run as plain data, ready for :func:`json.dump`.

    ``workspace`` is deliberately **not** written. It is an absolute path on the
    machine the run happened on, and a record carrying one is wrong twice: the
    directory stops describing itself the moment it is zipped and opened
    somewhere else, and in a source checkout - where the data root is the
    checkout - it would put somebody's home directory into a file sitting in
    the working tree. The directory the record was read from is the workspace,
    which is the one thing every reader already knows; :func:`load` fills it
    back in.
    """
    document = asdict(run)
    document.pop("workspace", None)
    document["schema"] = SCHEMA
    document["tally"] = run.tally()
    document["exit_code"] = run.exit_code
    return document


def save(run, workspace=None):
    """Write ``metadata.json``. Never raises - a run is not lost to a full disk.

    Returns the path it wrote, or "" when it could not. A failure here is worth
    noticing but is never worth failing a run that has already happened, which
    is the same call ``engine/artifacts.py`` makes about diagnostics.
    """
    where = workspace or run.workspace
    if not where:
        return ""
    try:
        return _atomic(metadata_path(where), to_document(run))
    except OSError:
        return ""


def load(workspace):
    """Read a run back from its directory. Returns None when there is nothing.

    The workspace is taken from where the file was found rather than from
    inside it - see :func:`to_document`. That is what lets a run directory be
    moved, copied or attached to a bug report and still read correctly.
    """
    try:
        with open(metadata_path(workspace), encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    run = from_document(document)
    if run is not None:
        run.workspace = os.path.abspath(workspace)
    return run


def from_document(document):
    """A record read from disk, back into a :class:`CycleRun`.

    Tolerant on purpose: a record written by an older version is missing fields
    this one knows about, and the useful answer is the run with gaps rather
    than an exception. Anything unrecognised is dropped rather than carried,
    because a half-understood record is worse than a plainly partial one.
    """
    if not isinstance(document, dict):
        return None
    steps = {}
    for step_id, raw in (document.get("steps") or {}).items():
        if not isinstance(raw, dict):
            continue
        artifacts = [Artifact(**_only(entry, Artifact))
                     for entry in (raw.get("artifacts") or [])
                     if isinstance(entry, dict)]
        step = StepRun(**_only(raw, StepRun, skip=("artifacts",)))
        step.artifacts = artifacts
        steps[step_id] = step
    run = CycleRun(**_only(document, CycleRun, skip=("steps",)))
    run.steps = steps
    return run


def _only(raw, kind, skip=()):
    """The keys of ``raw`` that ``kind`` actually has, minus ``skip``."""
    fields = set(kind.__dataclass_fields__)
    return {key: value for key, value in raw.items()
            if key in fields and key not in skip}


class Persister(NullObserver):
    """Publishes the summary and run index for history and older integrations.

    This observer is best effort. The executor's checkpoint store separately
    requires durable state before starting work and releasing dependencies.
    Indexing at start keeps interrupted runs discoverable as well as finished
    ones; log output belongs to the event observer.
    """

    def __init__(self, run=None, root=None):
        self._run = run
        self._root = root

    def run_start(self, run, graph, jobs=1):
        self._run = run
        save(run)
        _write_graph(run.workspace, graph)
        remember(run, self._root)

    def step_end(self, step_run):
        if self._run is not None:
            save(self._run)

    def step_skipped(self, step_run, reason):
        if self._run is not None:
            save(self._run)

    def step_waiting(self, step_run, seconds):
        """A step that parked itself. Written down, though nothing ended.

        Otherwise a run waiting an hour would spend that hour claiming on disk
        that the step was running - and the record is read by people asking
        exactly that question while it is going on. The index too: "waiting
        for approval" is what a list of sessions should say about this run.
        """
        if self._run is not None:
            save(self._run)
            remember(self._run, self._root)

    def subject(self, run):
        """The run found its subject: both files, so a list shows it now."""
        self._run = run
        save(run)
        remember(run, self._root)

    def run_end(self, run):
        self._run = run
        save(run)
        remember(run, self._root)


def _write_graph(workspace, graph):
    """Keep the graph beside the record, so a finished run can still be drawn.

    Without it, opening an old run would mean reading the cycle file as it is
    *now* - which may have had steps added, renamed or removed since. A run
    should be drawn as it actually was.
    """
    try:
        _atomic(os.path.join(workspace, "graph.json"), graph)
    except OSError:
        pass


def graph_of(workspace):
    """The graph a finished run was drawn from, or None."""
    try:
        with open(os.path.join(workspace, "graph.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# -- the index ----------------------------------------------------------------
def summary(run):
    """One row of the index: enough to list a run without opening it.

    No path here either, for the reason :func:`to_document` gives. A run's
    directory is ``runs_root()/<id>``, which every reader of this index can
    work out and none of them needs told - and an absolute path in a file that
    lives in the checkout is a home directory waiting to be committed.
    """
    tally = run.tally()
    return {"id": run.id, "cycle": run.cycle_id, "name": run.cycle_name,
            "status": run.status, "trigger": run.trigger,
            "started_at": run.started_at, "ended_at": run.ended_at,
            "duration_ms": round(run.duration_ms, 1),
            "steps": len(run.steps), "passed": tally.get("success", 0),
            "failed": tally.get("failed", 0), "skipped": tally.get("skipped", 0),
            "message": run.message, "resume_count": run.resume_count,
            "subject": dict(run.subject or {}), "reached": reached(run)}


def reached(run):
    """The step a run got to, for a reader asking "where is it now?".

    In order: a step waiting (a run parked on an approval is *at* the
    approval), a step running, the **first** step that failed - later ones,
    and the report every run writes whatever happened, are consequences, not
    where it stopped - and otherwise the step that finished last. A skipped or
    cancelled step is where a run did not go.
    """
    steps = list((run.steps or {}).values())
    for wanted in (WAITING, RUNNING):
        found = [step for step in steps if step.status == wanted]
        if found:
            return max(found, key=lambda step: step.started_at or 0).step_id
    failed = [step for step in steps if step.status in (FAILED, TIMEOUT)
              and step.ended_at]
    if failed:
        return min(failed, key=lambda step: step.ended_at).step_id
    ended = [step for step in steps if step.ended_at
             and step.status not in (SKIPPED, CANCELLED, PENDING)]
    if not ended:
        return ""
    return max(ended, key=lambda step: step.ended_at).step_id

def latest_of(cycle_id, root=None):
    """The most recent run of one cycle, read back whole, or ``None``.

    What a partial run borrows from. The index is newest first and carries the
    cycle each row belongs to, so this is a scan of a list rather than a walk
    of the directories - and it falls through to the directories only when the
    index has no row for this cycle, because the index is a convenience and
    the run directories are the truth.
    """
    cycle_id = str(cycle_id or "")
    for row in index(root):
        if row.get("cycle") == cycle_id:
            found = load(workspace_mod.path_of(row["id"], root))
            if found is not None:
                return found
    for run_id in workspace_mod.existing_runs(root):
        found = load(workspace_mod.path_of(run_id, root))
        if found is not None and found.cycle_id == cycle_id:
            return found
    return None


def remember(run, root=None):
    """Put ``run`` at the top of the index, replacing any earlier row for it."""
    rows = [row for row in index(root) if row.get("id") != run.id]
    rows.insert(0, summary(run))
    try:
        _atomic(index_path(root), {"schema": SCHEMA,
                                   "runs": rows[:MAX_INDEXED]})
    except OSError:
        pass
    return rows


def index(root=None):
    """Every remembered run, newest first. ``[]`` when there is no index yet."""
    try:
        with open(index_path(root), encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return []
    rows = document.get("runs") if isinstance(document, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict)]


def rebuild_index(root=None):
    """Build the index from the run directories on disk.

    For when the index was lost or is out of step with what is there - which a
    file that anything may delete eventually will be. The directories are the
    record; the index is only a way of not opening all of them.
    """
    rows = []
    for run_id in workspace_mod.existing_runs(root):
        run = load(workspace_mod.path_of(run_id, root))
        if run is not None:
            rows.append(summary(run))
    rows.sort(key=lambda row: row.get("started_at") or 0, reverse=True)
    try:
        _atomic(index_path(root), {"schema": SCHEMA, "runs": rows[:MAX_INDEXED]})
    except OSError:
        pass
    return rows


def _atomic(path, document):
    """Write JSON through a temp file, so a reader never sees half of one.

    The same write-then-replace shape ``engine/flowfile._atomic_write`` and
    ``servicesfile.save`` use. ``os.replace`` is atomic on both platforms, so a
    process killed mid-write leaves either the old file or the new one.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    if os.name != "nt":
        # fsync the rename too: a completed receipt must survive a power loss,
        # not only an ordinary process exit.
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return path


def prune(keep=50, root=None):
    """Delete the oldest run directories, keeping the newest ``keep``.

    Nothing calls this automatically. Deleting somebody's artifacts is not a
    thing to do on a schedule they did not ask for - but having the operation
    written down once, correctly, is better than three places doing it by hand.
    Returns the ids it removed.
    """
    import shutil

    removed = []
    for run_id in workspace_mod.existing_runs(root)[keep:]:
        try:
            shutil.rmtree(workspace_mod.path_of(run_id, root))
            removed.append(run_id)
        except OSError:
            continue
    if removed:
        rebuild_index(root)
    return removed


def started_now():
    """The wall clock, as a run records it. One place, so tests can find it."""
    return time.time()
