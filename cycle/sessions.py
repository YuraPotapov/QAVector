"""What a cycle is working on, and every run that worked on it.

A cycle file may say what it works on - ``subject: {kind: task, key:
${steps.todo.outputs.key}}`` - and a run finds out the answer as it goes: the
task a development cycle takes is only known once the step that takes it has
finished. :func:`resolve` is that finding out.

A **session** is every run of one cycle on one subject: the first attempt at
QA-934, the resume after the plan was sent back, the run a week later that
picked it up again. That is the unit a person thinks in - "where is QA-934
up to" - and until now nothing on disk could answer it without reading every
run's step outputs.

**Derived, not stored.** Sessions are grouped from the run index, which already
carries each run's subject, rather than kept in a file of their own. A second
file would be a second account of the same runs, and the two would disagree
the first time a run directory was deleted by hand. A run whose subject is not
known - a cycle that declares none, or a run that stopped before finding one -
is a session by itself: every such pass stands on its own.

**Deleting one** removes what belongs to it and nothing else: its run
directories, their rows in the index, and the memory record the subject names.
It never touches a git branch or a Jira issue - those are somebody's work, not
this application's record of it - and it refuses while one of its runs is
still being executed.
"""

import os
import shutil

from cycle import checkpoints, memory, run as records, variables
from cycle import workspace as workspace_mod
from cycle.model import SUBJECT_EXPRESSIONS
from domain.cycle import PENDING, RUNNING, SUCCESS

#: How an index row reads when its run is not going and never said it ended:
#: the process was killed, or the machine went down, mid-run.
INTERRUPTED = "interrupted"


class SessionError(Exception):
    """A session that cannot be found or cannot be deleted right now."""


# -- a run finding out what it works on ---------------------------------------
def resolve(cycle, run, scope, step_id=""):
    """``run``'s subject as ``{kind, key, title, memory, pin, step}``, or None.

    None until every step the subject reads has succeeded, and None when the
    key comes out empty - a Jira query that found nothing has no subject, and
    saying "working on ''" would be worse than saying nothing. ``title`` and
    ``memory`` are best effort: a subject whose title cannot be read is still
    the subject.

    ``step_id`` is the step that just finished, recorded as the one that
    settled it; without one the last of the steps it reads stands in.
    """
    subject = getattr(cycle, "subject", None)
    if subject is None or not subject.key:
        return None
    sources = set()
    for name in SUBJECT_EXPRESSIONS:
        for path in variables.references(getattr(subject, name)):
            parts = path.split(".")
            if parts[0] == "steps" and len(parts) > 1:
                sources.add(parts[1])
    for name in sources:
        step = (run.steps or {}).get(name)
        if step is None or step.status != SUCCESS:
            return None
    try:
        key = variables.resolve(subject.key, scope)
    except variables.ResolveError:
        return None
    key = "" if key is None else str(key).strip()
    if not key:
        return None
    found = {"kind": subject.kind, "key": key, "pin": subject.pin}
    for name in ("title", "memory"):
        try:
            value = variables.resolve(getattr(subject, name), scope)
        except variables.ResolveError:
            value = ""
        found[name] = "" if value is None else str(value).strip()
    if not step_id or step_id not in sources:
        ended = [(run.steps[name].ended_at or 0, name) for name in sources]
        step_id = max(ended)[1] if ended else ""
    found["step"] = step_id
    return found


# -- sessions -----------------------------------------------------------------
def session_id(row):
    """The session a run belongs to: its cycle and subject, or just itself."""
    subject = row.get("subject") or {}
    if subject.get("key"):
        return "%s:%s" % (row.get("cycle", ""), subject["key"])
    return "%s:run:%s" % (row.get("cycle", ""), row.get("id", ""))


def backfill(root=None, cycles_dir=None):
    """Give index rows written before subjects existed a subject. Once.

    Such a row has no ``subject`` key at all - not an empty one, which is a
    run that looked and found none. Its run's record still has every step's
    outputs, so the subject its cycle declares *now* can be worked out from
    them the same way a live run does, and written back into the index, which
    is a cache of the run directories and nothing more. Without this every
    old run of a development cycle would be a session of its own, and a list
    of eighty "Run 09:15" rows says nothing about which task each one was.

    Returns how many rows it filled in. Never raises.
    """
    rows = records.index(root)
    stale = [row for row in rows if "subject" not in row]
    if not stale:
        return 0
    from cycle import loader
    declared, filled = {}, {}
    for row in stale:
        cycle_id = row.get("cycle", "")
        if cycle_id not in declared:
            try:
                declared[cycle_id] = loader.load_cycle(cycle_id, cycles_dir)
            except Exception:                # noqa: BLE001 - gone, or broken
                declared[cycle_id] = None
        found, reached, resumes = {}, "", 0
        run = records.load(workspace_mod.path_of(row.get("id", ""), root))
        if run is not None:
            reached, resumes = records.reached(run), run.resume_count
            cycle = declared[cycle_id]
            if cycle is not None and cycle.subject is not None:
                scope = variables.scope(run, cycle, run.steps, env={})
                found = resolve(cycle, run, scope) or {}
        filled[row.get("id")] = {"subject": found, "reached": reached,
                                 "resume_count": resumes}
    # Read again just before writing, and only these fields changed: a run
    # going right now rewrites its own row, and that row must not be lost to
    # a copy of the index taken before it did.
    rows = records.index(root)
    for row in rows:
        if row.get("id") in filled and "subject" not in row:
            row.update(filled[row["id"]])
    try:
        records._atomic(records.index_path(root),
                        {"schema": records.SCHEMA, "runs": rows})
    except OSError:
        return 0
    return len(filled)


def sessions(root=None, cycle_id="", memory_path=None, cycles_dir=None):
    """Every session, the most recently active first.

    ``cycle_id`` narrows to one cycle. Each session carries its runs newest
    first, the state of the latest one - with a run the index still calls
    running but which nothing is executing reported as interrupted - where it
    got to, and what the memory record its subject names holds now.
    """
    backfill(root, cycles_dir)
    grouped, order = {}, []
    for row in records.index(root):
        if cycle_id and row.get("cycle") != cycle_id:
            continue
        name = session_id(row)
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(row)

    try:
        remembered = memory.load(memory_path)
    except memory.MemoryError_:
        remembered = {}

    found = []
    for name in order:
        runs = [_run_entry(row, root) for row in grouped[name]]
        runs.sort(key=lambda one: one.get("started_at") or 0, reverse=True)
        latest = runs[0]
        subject = {}
        for one in reversed(runs):
            # Oldest first, so the newest run's title wins - but a run that
            # found no title does not erase what an earlier one did find.
            for key, value in (one.get("subject") or {}).items():
                if value:
                    subject[key] = value
        record = remembered.get(subject.get("memory") or "") or {}
        found.append({
            "id": name,
            "cycle": latest.get("cycle", ""),
            "name": latest.get("name", ""),
            "subject": subject,
            "status": latest["state"],
            "running": latest["running"],
            "reached": latest.get("reached", ""),
            "reached_label": latest.get("reached_label", ""),
            "reached_plugin": latest.get("reached_plugin", ""),
            "message": latest.get("message", ""),
            "latest": latest.get("id", ""),
            "started_at": min((one.get("started_at") or 0) for one in runs),
            "updated_at": max((one.get("ended_at") or one.get("started_at") or 0)
                              for one in runs),
            "memory": ({"key": subject.get("memory", ""),
                        "fields": dict(record.get("fields") or {}),
                        "updated_at": record.get("updated_at"),
                        "claim": dict(record.get("claim") or {})}
                       if record else {}),
            "runs": runs,
        })
    found.sort(key=lambda one: one["updated_at"] or 0, reverse=True)
    return found


def _run_entry(row, root):
    """One index row, with where it lives and what it is doing now."""
    entry = dict(row)
    where = workspace_mod.path_of(row.get("id", ""), root)
    entry["run_dir"] = where
    running = row.get("status") in (RUNNING, PENDING) and _held(where)
    entry["running"] = running
    entry["state"] = (row.get("status") if running or row.get("status")
                      not in (RUNNING, PENDING) else INTERRUPTED)
    reached = row.get("reached") or ""
    entry["reached_label"], entry["reached_plugin"] = reached, ""
    if reached:
        for node in (records.graph_of(where) or {}).get("nodes") or []:
            if node.get("id") == reached:
                entry["reached_label"] = node.get("label") or reached
                entry["reached_plugin"] = node.get("plugin") or ""
                break
    return entry


def _held(workspace):
    """Whether an executor holds this run right now.

    Asked of the same lock the executor takes, so the answer cannot disagree
    with it: a run the index says is running, which nobody holds, was cut off.
    Only asked about runs the index calls running, so a list of finished
    sessions never touches a lock at all.
    """
    if not os.path.isdir(workspace):
        return False
    try:
        with checkpoints.lock(workspace):
            return False
    except checkpoints.CheckpointError:
        return True


def find(name, root=None, memory_path=None):
    """One session by id. Raises :class:`SessionError` when there is none."""
    for one in sessions(root, memory_path=memory_path):
        if one["id"] == name:
            return one
    raise SessionError("There is no session %r." % name)


def delete(name, root=None, memory_path=None):
    """Remove one session: its runs, their index rows, and its memory record.

    Returns what went, so whoever asked can say so. Refuses - before removing
    anything - while any of its runs is being executed; a run directory taken
    out from under its executor is a run that fails for no reason it can give.
    """
    session = find(name, root, memory_path)
    busy = [one["id"] for one in session["runs"] if one["running"]]
    if busy:
        raise SessionError("%s is still running (%s). Stop it before deleting "
                           "the session." % (name, ", ".join(busy)))
    base = os.path.realpath(workspace_mod.runs_root(root))
    removed = []
    for one in session["runs"]:
        where = os.path.realpath(one["run_dir"])
        # Only ever a directory directly under the runs root: an index row is
        # a file anything could have edited, and its id must not be able to
        # name somewhere else.
        if os.path.dirname(where) != base or not os.path.isdir(where):
            continue
        shutil.rmtree(where, ignore_errors=True)
        removed.append(one["id"])
    gone = {one["id"] for one in session["runs"]}
    rows = [row for row in records.index(root) if row.get("id") not in gone]
    try:
        records._atomic(records.index_path(root),
                        {"schema": records.SCHEMA, "runs": rows})
    except OSError as exc:
        raise SessionError("Cannot rewrite the run index: %s" % exc)
    key = (session.get("subject") or {}).get("memory") or ""
    forgotten = memory.forget(key, memory_path) if key else False
    return {"id": name, "runs": removed, "memory": key if forgotten else ""}
