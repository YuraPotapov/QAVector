"""The plan: what is waiting for a person to look at later.

Two kinds of thing end up here, both because somebody was asked and did not
answer:

* **a task** a trigger found in a queue it listens to (``cycle/watch.py``),
  kept rather than offered again and again;
* **an approval** a run stopped at - ``approval.gate`` - when nobody answered
  it. Not answered is still not approved: the run stopped, and nothing it
  would have done was done. What the plan adds is that the stop is not lost:
  the entry names the run, and resuming it asks the question again.

**Run later.** An entry may carry ``run_at``: when somebody asked for it to be
started, at most :data:`HORIZON` ahead. The application starts it then, or -
when it was closed at the time - asks whether to start it now. One whose
``run_at`` is further back than the same three days is *stale* and never
started: the application may be opened a month later, and a task planned a
month ago is not today's work. Nothing is deleted for it; the entry stays on
the plan, saying so, until somebody removes or starts it.

Every entry is ``{"id", "kind", "cycle", "title", "at", ...}``. Tasks live in
their trigger's watch record, where "seen" is kept; approvals in a record of
their own under ``plan/approval/<run>/<step>``. :func:`items` reads both, so a
front-end shows one list.
"""

import time

from cycle import memory

APPROVAL = "approval"
TASK = "task"
APPROVAL_PREFIX = "plan/approval/"
#: How far ahead a start may be scheduled - and how long after its time a
#: scheduled start is still worth asking about.
HORIZON = 3 * 24 * 3600


def add_approval(cycle_id, run_id, step_id, question, subject=None,
                 memory_path=None, when=None):
    """Keep an unanswered approval to come back to. Returns the entry."""
    subject = dict(subject or {})
    fields = {"kind": APPROVAL, "cycle": cycle_id, "run": run_id, "step": step_id,
              "question": str(question or ""), "key": str(subject.get("key") or ""),
              "title": str(subject.get("title") or question or ""),
              "at": when or time.time()}
    memory.remember(APPROVAL_PREFIX + "%s/%s" % (run_id, step_id), fields, memory_path)
    return _approval(fields)


def _approval(fields):
    return dict(fields, id="%s:%s:%s" % (APPROVAL, fields.get("run", ""),
                                         fields.get("step", "")))


def items(memory_path=None):
    """Everything on the plan, oldest first."""
    from cycle import watch

    found = [dict(one, kind=TASK,
                  id="%s:%s:%s:%s" % (TASK, one["cycle"], one["trigger"], one["key"]))
             for one in watch.planned(memory_path)]
    for key in memory.keys(APPROVAL_PREFIX, memory_path):
        fields = memory.recall(key, memory_path)
        if fields.get("kind") == APPROVAL:
            found.append(_approval(fields))
    return sorted(found, key=lambda one: one.get("at") or 0)


def remove(entry_id, memory_path=None, cycles_dir=None):
    """Take one entry off the plan. True when there was one.

    A task is marked seen - removing it means "do not offer this again" - and
    that is what takes it off. An approval's record is forgotten; its run is
    untouched and may still be resumed from its session.
    """
    kind, _, rest = str(entry_id).partition(":")
    if kind == APPROVAL:
        run_id, _, step_id = rest.rpartition(":")
        return memory.forget(APPROVAL_PREFIX + "%s/%s" % (run_id, step_id), memory_path)
    if kind == TASK:
        from cycle import loader, watch
        parts = rest.split(":")
        if len(parts) < 3:
            return False
        cycle_id, trigger_id, key = parts[0], parts[1], ":".join(parts[2:])
        watch.mark_seen(loader.load_cycle(cycle_id, cycles_dir), key, trigger_id,
                        memory_path)
        return True
    return False


def stale(item, now=None):
    """True when ``item`` was scheduled further back than :data:`HORIZON`:
    it is never started on its own, nor asked about."""
    run_at = item.get("run_at") if isinstance(item, dict) else None
    if not isinstance(run_at, (int, float)) or isinstance(run_at, bool):
        return False
    return (now if now is not None else time.time()) - run_at > HORIZON


def schedule(entry_id, run_at, memory_path=None, cycles_dir=None, now=None):
    """Start ``entry_id`` at ``run_at`` (seconds since the epoch), or never on
    its own again when ``run_at`` is None. Returns the entry's ``run_at``.

    Raises ValueError for a time in the past, one more than :data:`HORIZON`
    ahead, or an entry that is not on the plan.
    """
    now = now if now is not None else time.time()
    if run_at is not None:
        try:
            run_at = float(run_at)
        except (TypeError, ValueError):
            raise ValueError("run_at must be a time, in seconds since the epoch")
        if run_at <= now:
            raise ValueError("That time has already passed.")
        if run_at > now + HORIZON:
            raise ValueError("A start can be scheduled at most %d days ahead."
                             % (HORIZON // 86400))
    kind, _, rest = str(entry_id).partition(":")
    if kind == APPROVAL:
        run_id, _, step_id = rest.rpartition(":")
        key = APPROVAL_PREFIX + "%s/%s" % (run_id, step_id)
        if not memory.known(key, memory_path):
            raise ValueError("%s is not on the plan" % entry_id)
        memory.remember(key, {"run_at": run_at}, memory_path)
        return run_at
    if kind == TASK:
        from cycle import watch
        parts = rest.split(":")
        if len(parts) < 3:
            raise ValueError("%s is not on the plan" % entry_id)
        cycle_id, trigger_id, task = parts[0], parts[1], ":".join(parts[2:])
        record = watch.record_key(cycle_id, trigger_id)
        planned = list(memory.recall(record, memory_path).get("planned") or [])
        found = False
        for one in planned:
            if one.get("key") == task:
                found = True
                if run_at is None:
                    one.pop("run_at", None)
                else:
                    one["run_at"] = run_at
        if not found:
            raise ValueError("%s is not on the plan" % entry_id)
        memory.remember(record, {"planned": planned}, memory_path)
        return run_at
    raise ValueError("%s is not on the plan" % entry_id)
