"""The plan: what is waiting for a person to look at later.

Two kinds of thing end up here, both because somebody was asked and did not
answer:

* **a task** a trigger found in a queue it listens to (``cycle/watch.py``),
  kept rather than offered again and again;
* **an approval** a run stopped at - ``approval.gate`` - when nobody answered
  it. Not answered is still not approved: the run stopped, and nothing it
  would have done was done. What the plan adds is that the stop is not lost:
  the entry names the run, and resuming it asks the question again.

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
