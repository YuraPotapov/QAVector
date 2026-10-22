"""Subjects: one thing being worked on, and everything any cycle did about it.

A session (``cycle/sessions.py``) is one cycle's runs on one subject - the unit
of the Cycles page's sidebar, where the canvas belongs to one cycle. It is not
the unit of the work. Task QA-934 is planned and built by one cycle, released by
another, and somebody asked a single-agent cycle a question about it in between:
three sessions, one task. A **subject** is the thing itself - every run that
named its key, whichever cycle made it.

What somebody coming back to it needs is not a list of runs but what happened in
them: what they were asked and what they answered, what they said to it, what
each agent came back with, where it stopped and what is left to do. That is
:func:`history`, in the order it happened, read from what the runs already wrote
down. **Derived, not stored**, for the reason sessions are: a second account of
the same runs would disagree with them the first time a run directory was
deleted by hand. The one thing kept here is what a person writes about a subject
themselves - :func:`add_note` - because no run could have written it.

**Nothing here knows what a subject is.** A key is whatever a cycle's
``subject:`` resolved to, or what the person pressed Start on: a Jira key, a
release number, a playlist. No repository, branch or tracker is read, because a
cycle may do any of those things or none of them.
"""

import getpass
import json
import os
import time

from cycle import ask, checkpoints, interactions, memory, planner, sessions
from cycle import run as records
from cycle import workspace as workspace_mod
from domain.cycle import FAILED, PENDING, RUNNING, SKIPPED, SUCCESS, TIMEOUT, WAITING

#: The id a run without a subject goes by: it is a subject of its own, the way
#: sessions treat it, so a run nobody named that is waiting for an answer is
#: still on the list.
RUN_PREFIX = "run:"

#: Steps that put a question to a person. A run *at* one of these is waiting
#: for somebody, not working.
PERSON_PLUGINS = ("approval.gate", "user.choice", "user.input")

#: The reason a run was stopped with when its person parked it to come back to
#: later. A run stopped with it is waiting for them, not finished.
PARKED = "parked"

#: Where a run stopped on its way to finishing, which Resume continues. The
#: same set the Cycles page offers Resume for.
RESUMABLE = ("failed", "cancelled", "interrupted", "timeout", "error", WAITING, "paused")

#: The two views of a history: what passed between a person and the cycles, or
#: that and everything the steps did around it.
CONVERSATION = "conversation"
ALL = "all"
SCOPES = (CONVERSATION, ALL)

#: The three views of the list: what is waiting for its person, what is not
#: finished, everything.
NEEDS_YOU = "needs_you"
ACTIVE = "active"
VIEWS = (NEEDS_YOU, ACTIVE, ALL)

#: Where notes are kept, beside the cycles' memory so moving one moves both.
NOTES_FILE = "subjectnotes.json"
#: What a question step keeps of each time it asked (cycle/ask.record), and
#: what an agent call was told (cycle/plugins/agent_run) - the same names.
ASKED_FILE = ask.ASKED_FILE
PROMPT_FILE = "prompt.md"
#: The longest note. The same bound as revision feedback and clarifications:
#: room for what to do next, not for a pasted log.
MAX_NOTE = 8000

#: Stage events in ``logs/cycle.jsonl`` that are an agent's workings rather
#: than anything a history shows. Skipped by substring before parsing: they
#: are most of a long run's log, and parsing them to throw them away was most
#: of the time spent reading one.
_NOISE = ('"phase": "tool"', '"phase": "tool_result"', '"phase": "thinking"',
          '"phase": "text"')


class SubjectError(Exception):
    """A subject that cannot be found, or a note that cannot be kept."""


# -- identity -----------------------------------------------------------------
def subject_id(row):
    """The subject one run belongs to: its key, or the run alone without one."""
    key = str(((row or {}).get("subject") or {}).get("key") or "").strip()
    return key if key else RUN_PREFIX + str((row or {}).get("id", ""))


# -- where one stands ---------------------------------------------------------
def standing(entry, planned=()):
    """``(state, needs_you, active)`` for the newest run of a subject.

    ``entry`` is a run as :func:`sessions._run_entry` describes it - running is
    asked of the run's lock, not taken from the index. ``planned`` holds the ids
    of runs with an unanswered approval on the plan.

    Needs you is everything that will not move until its person does something:
    a question asked, a node waiting for consent, a pause, a run they parked, an
    approval nobody answered. Active is that and everything still going.
    """
    if entry.get("running"):
        if entry.get("execution_status") == "waiting_for_node_approval":
            return "node_approval", True, True
        if entry.get("paused"):
            return "paused", True, True
        if entry.get("pause_requested"):
            return "pausing", False, True
        if entry.get("reached_plugin") in PERSON_PLUGINS:
            return "asking", True, True
        return "running", False, True
    if entry.get("state") == "paused" or entry.get("paused"):
        return "paused", True, True
    if entry.get("message") == PARKED:
        return PARKED, True, True
    if entry.get("id") in planned:
        return "planned", True, True
    return (entry.get("outcome") or entry.get("state") or entry.get("status") or ""), False, False


# -- the list -----------------------------------------------------------------
def listing(root=None, memory_path=None, cycles_dir=None, view=ALL, limit=0,
            offset=0):
    """Every subject, what needs its person first, then what is going, then
    the rest by when anything last happened.

    ``view`` narrows to :data:`NEEDS_YOU` or :data:`ACTIVE`; ``limit`` (0 for
    all) and ``offset`` cut one page out of it. ``counts`` is worked out over
    every subject, so the list can say how many wait for you without paging to
    the end. Only the page's subjects are described in full: describing one
    reads its newest runs, and doing that for every subject there ever was, to
    show fifty, would make the list slower with every run.

    Returns ``{subjects, total, more, counts}``.
    """
    if view not in VIEWS:
        raise SubjectError("A view is one of %s, not %r." % (", ".join(VIEWS), view))
    sessions.backfill(root, cycles_dir)
    groups, order = {}, []
    for row in records.index(root):
        name = subject_id(row)
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(row)
    planned = _planned_runs(memory_path)
    kept = _notes_store(memory_path)
    summaries = [_summary(name, groups[name], root, planned, kept) for name in order]
    # A subject somebody wrote about but no run is left of is still theirs: the
    # note is the only thing that can say why it matters.
    for name, record in kept.items():
        if name not in groups and (record or {}).get("notes"):
            summaries.append(_orphan(name, record))
    summaries.sort(key=lambda one: (not one["needs_you"], not one["active"],
                                    -(one["updated_at"] or 0)))
    counts = {NEEDS_YOU: sum(one["needs_you"] for one in summaries),
              ACTIVE: sum(one["active"] for one in summaries),
              ALL: len(summaries)}
    if view == NEEDS_YOU:
        summaries = [one for one in summaries if one["needs_you"]]
    elif view == ACTIVE:
        summaries = [one for one in summaries if one["active"]]
    offset = max(0, int(offset or 0))
    chosen = summaries[offset:offset + int(limit)] if limit else summaries[offset:]
    try:
        remembered = memory.load(memory_path)
    except memory.MemoryError_:
        remembered = {}
    cycles = {}
    return {"subjects": [_described(one, root, remembered, cycles_dir, cycles)
                         for one in chosen],
            "total": len(summaries),
            "more": offset + len(chosen) < len(summaries),
            "counts": counts}


def _summary(name, rows, root, planned, kept):
    """Enough about one subject to sort and count it, without opening its runs."""
    newest = max(rows, key=lambda row: row.get("started_at") or 0)
    entry = _light(newest, root)
    state, needs_you, active = standing(entry, planned)
    subject = {}
    for row in sorted(rows, key=lambda row: row.get("started_at") or 0):
        # Oldest first, so the newest run's title wins - but a run that found
        # no title does not erase what an earlier one did find.
        for key, value in (row.get("subject") or {}).items():
            if value:
                subject[key] = value
    cycles, seen = [], set()
    for row in sorted(rows, key=lambda row: row.get("started_at") or 0, reverse=True):
        if row.get("cycle") not in seen:
            seen.add(row.get("cycle"))
            cycles.append({"id": row.get("cycle", ""), "name": row.get("name", "")})
    notes = list(((kept.get(name) or {}).get("notes")) or [])
    note = notes[-1] if notes else {}
    updated = max([(row.get("ended_at") or row.get("started_at") or 0) for row in rows]
                  + [note.get("at") or 0])
    return {"id": name, "key": subject.get("key", ""), "kind": subject.get("kind", ""),
            "title": subject.get("title", ""), "cycles": cycles, "runs": len(rows),
            "started_at": min((row.get("started_at") or 0) for row in rows),
            "updated_at": updated, "state": state, "needs_you": needs_you,
            "active": active, "note": dict(note), "notes": len(notes),
            "_rows": rows, "_newest": newest}


def _orphan(name, record):
    notes = list(record.get("notes") or [])
    note = notes[-1] if notes else {}
    key = "" if name.startswith(RUN_PREFIX) else name
    return {"id": name, "key": key, "kind": "", "title": "", "cycles": [], "runs": 0,
            "started_at": record.get("created_at") or note.get("at") or 0,
            "updated_at": note.get("at") or record.get("updated_at") or 0,
            "state": "", "needs_you": False, "active": False, "note": dict(note),
            "notes": len(notes), "_rows": [], "_newest": None}


def _light(row, root):
    """What a run is doing now, asking its lock and graph only when it may be going.

    The index says what a run was doing when it last wrote; only a run it calls
    running can be anything else, and only for those is the lock worth asking or
    the graph worth opening to learn whether the step it is at asks a person.
    """
    entry = dict(row)
    where = workspace_mod.path_of(row.get("id", ""), root)
    entry["run_dir"] = where
    going = row.get("status") in (RUNNING, PENDING, WAITING)
    running = going and sessions._held(where)
    entry["running"] = running
    entry["state"] = (row.get("status") if running or not going else sessions.INTERRUPTED)
    if entry.get("paused"):
        entry["state"] = "paused"
    entry["reached_plugin"] = ""
    if running and row.get("reached"):
        for node in (records.graph_of(where) or {}).get("nodes") or []:
            if node.get("id") == row["reached"]:
                entry["reached_plugin"] = node.get("plugin") or ""
                break
    return entry


def _described(summary, root, remembered, cycles_dir, cycles):
    """One subject of the page in full: its newest session, ready to be read."""
    out = {key: value for key, value in summary.items() if not key.startswith("_")}
    newest = summary["_newest"]
    if newest is None:
        out.update(session={}, latest={}, resume={})
        return out
    name = sessions.session_id(newest)
    rows = [row for row in summary["_rows"] if sessions.session_id(row) == name]
    session = sessions._session(name, rows, root, remembered)
    out["session"] = session
    latest = session["runs"][0] if session.get("runs") else {}
    out["latest"] = {key: latest.get(key) for key in (
        "id", "cycle", "name", "run_dir", "state", "running", "status", "message",
        "reached", "reached_label", "reached_plugin", "started_at", "ended_at",
        "execution_status", "outcome", "current_phase", "paused", "next_action",
        "execution_mode") if key in latest}
    out["resume"] = _resume_offer(latest, cycles_dir, cycles)
    if summary["state"] in (PARKED, "paused", "planned") or out["resume"]:
        out["resume_at"] = resume_at(latest.get("run_dir", ""))
    return out


def resume_at(run_dir):
    """The step a stopped run picks up at, by its label, or "".

    Not where it got to - the step that finished last - but where Resume goes
    on: what was going when it stopped (a question asked, an agent mid-call),
    or else the first step it never started, in the order the cycle lists them.
    """
    run = records.load(run_dir) if run_dir else None
    if run is None:
        return ""
    graph = records.graph_of(run_dir) or {}
    order = [node.get("id") for node in graph.get("nodes") or [] if node.get("id")]
    labels = {node.get("id"): node.get("label") or node.get("id")
              for node in graph.get("nodes") or [] if node.get("id")}
    order += [name for name in run.steps if name not in order]
    unfinished = [name for name in order if name in run.steps
                  and run.steps[name].status not in (SUCCESS, SKIPPED)]
    going = [name for name in unfinished if run.steps[name].started_at is not None]
    chosen = (going or unfinished)[:1]
    return labels.get(chosen[0], chosen[0]) if chosen else ""


def _resume_offer(latest, cycles_dir, cycles):
    """Where Resume would pick up, and why it would be refused when it would be."""
    if not latest or latest.get("running") or latest.get("state") not in RESUMABLE:
        return {}
    return {"run_id": latest.get("id", ""), "cycle": latest.get("cycle", ""),
            "run_dir": latest.get("run_dir", ""),
            "problem": resume_problem(latest.get("run_dir", ""), latest.get("cycle", ""),
                                      cycles_dir, cycles)}


def resume_problem(run_dir, cycle_id, cycles_dir=None, cycles=None):
    """Why Resume would be refused today, in the core's own words, or "".

    Only the checks a list can afford - the cycle is still there, has the same
    steps and the same phases - and the ones that refuse most often: a cycle
    edited since the run. The full check, receipts and all, is the core's when
    Resume is actually asked for; this only keeps the list from offering a
    button that is certain to fail.
    """
    from cycle import loader, phases
    cycles = {} if cycles is None else cycles
    if cycle_id not in cycles:
        try:
            cycles[cycle_id] = loader.load_cycle(cycle_id, cycles_dir)
        except Exception:                    # noqa: BLE001 - gone, or broken
            cycles[cycle_id] = None
    cycle = cycles[cycle_id]
    if cycle is None:
        return "The cycle %s is no longer there, so this run cannot be resumed." % cycle_id
    execution = os.path.join(run_dir, ".execution")
    manifest_path = os.path.join(execution, "manifest.json")
    if not os.path.exists(manifest_path):
        return ("This run predates Resume: its original step settings were not "
                "recorded. Start a new run.")
    graph = records.graph_of(run_dir) or {}
    then = {node.get("id") for node in graph.get("nodes") or [] if node.get("id")}
    if then and then != {step.id for step in cycle.steps}:
        return ("The cycle's steps changed since this run, and Resume needs the "
                "same graph. Start a new run.")
    try:
        manifest = checkpoints._read(manifest_path)
    except checkpoints.CheckpointError:
        return ""
    if (manifest.get("phases", []) or []) != [phases.document(one) for one in cycle.phases]:
        return "The phase contract changed since this run. Start a new run."
    return ""


def _planned_runs(memory_path):
    try:
        return {str(item.get("run") or "") for item in planner.items(memory_path)
                if item.get("kind") == planner.APPROVAL}
    except Exception:                        # noqa: BLE001 - a list, not a gate
        return set()


# -- the history --------------------------------------------------------------
def history(subject, root=None, memory_path=None, scope=ALL, limit=0):
    """Everything that happened about one subject, oldest first.

    Every run of every cycle that named it, with the person's notes woven in:
    runs starting and ending, pauses and continues, questions and their answers,
    plans sent back, messages sent to a step, each agent's answer. ``scope``
    :data:`CONVERSATION` keeps what passed between a person and the cycles;
    :data:`ALL` adds what the steps did around it - checks, failures, and the
    steps that simply passed, folded together per run.

    ``limit`` keeps the newest that many entries (0 for all) and ``earlier``
    says how many came before them, for a "show earlier" that asks again.
    ``files`` lists each run's step files and ``runs`` every run, both newest
    run first and across every cycle that named the subject.

    Returns ``{subject, entries, total, earlier, files, runs}``.
    """
    if scope not in SCOPES:
        raise SubjectError("A scope is one of %s, not %r." % (", ".join(SCOPES), scope))
    rows = [row for row in records.index(root) if subject_id(row) == subject]
    notes_kept = notes(subject, memory_path)
    if not rows and not notes_kept:
        raise SubjectError("There is no subject %r." % subject)
    rows.sort(key=lambda row: row.get("started_at") or 0)
    entries, files = [], []
    for row in rows:
        found, made = _run_history(row, root, scope, memory_path)
        entries.extend(found)
        if made:
            files.append(made)
    for note in notes_kept:
        entries.append({"kind": "note", "at": note.get("at") or 0,
                        "who": note.get("who", ""), "text": note.get("text", "")})
    for order, entry in enumerate(entries):
        entry["_order"] = order
    entries.sort(key=lambda entry: (entry.get("at") or 0, entry["_order"]))
    for entry in entries:
        del entry["_order"]
    total = len(entries)
    shown = entries[-int(limit):] if limit else entries
    files.sort(key=lambda one: one.get("started_at") or 0, reverse=True)
    runs = [{"run": row.get("id", ""), "cycle": row.get("cycle", ""),
             "cycle_name": row.get("name", ""),
             "run_dir": workspace_mod.path_of(row.get("id", ""), root),
             "started_at": row.get("started_at") or 0, "ended_at": row.get("ended_at") or 0,
             "status": row.get("status", ""), "message": row.get("message", ""),
             "outcome": row.get("outcome", ""), "resume_count": row.get("resume_count") or 0}
            for row in reversed(rows)]
    return {"subject": subject, "entries": shown, "total": total,
            "earlier": total - len(shown), "files": files, "runs": runs}


def _run_history(row, root, scope, memory_path):
    """One run's entries, and the files its steps wrote."""
    where = workspace_mod.path_of(row.get("id", ""), root)
    base = {"run": row.get("id", ""), "cycle": row.get("cycle", ""),
            "cycle_name": row.get("name", ""), "run_dir": where}
    run = records.load(where)
    if run is None:
        # The index remembers a run whose directory is gone: say so once,
        # rather than leave a hole where it was.
        return [dict(base, kind="run.gone", at=row.get("started_at") or 0,
                     status=row.get("status", ""))], None
    labels = {node.get("id"): node.get("label") or node.get("id")
              for node in (records.graph_of(where) or {}).get("nodes") or []
              if node.get("id")}
    events = _events(where)
    entries = []

    def add(kind, at, **fields):
        entries.append(dict(base, kind=kind, at=at or 0, **fields))

    starts = [one for one in events if one.get("kind") == "cycle.run.start"]
    modes = [one for one in events if one.get("kind") == "cycle.run.mode"]
    ends = [one for one in events if one.get("kind") == "cycle.run.end"]
    if starts:
        for index, start in enumerate(starts):
            mode = modes[index] if index < len(modes) else {}
            add("run.start", start.get("ts"), mode=mode.get("mode") or "",
                resume_count=mode.get("resume_count") or 0,
                source_run=mode.get("source_run") or "",
                trigger=row.get("trigger", ""),
                execution_mode=(run.initial_execution_mode if index == 0
                                else run.execution_mode) or "")
    else:
        add("run.start", run.started_at, mode="", resume_count=0, source_run="",
            trigger=row.get("trigger", ""), execution_mode=run.execution_mode or "")
    for end in ends:
        add("run.end", end.get("ts"), status=end.get("status", ""),
            message=end.get("message", ""))
    if not ends and run.ended_at and run.status not in (RUNNING, PENDING, WAITING):
        add("run.end", run.ended_at, status=run.status, message=run.message)

    for change in run.control_history or []:
        if change.get("action") in ("pause", "continue", "run_phase", "retry_phase"):
            add("control", change.get("at"), action=change.get("action"),
                reason=change.get("reason", ""), phase=change.get("phase", ""))
    for revision in run.revisions or []:
        add("revision", revision.get("created_at"), number=revision.get("number", 0),
            gate=revision.get("gate", ""), label=labels.get(revision.get("gate"), revision.get("gate", "")),
            target=revision.get("target", ""),
            target_label=labels.get(revision.get("target"), revision.get("target", "")),
            feedback=revision.get("feedback", ""), who=revision.get("who", ""))
    approved = []
    for receipt in _node_approvals(where, run):
        if receipt.get("status") == "approved":
            approved.append(receipt)
            continue
        add("node_approval", receipt.get("decided_at") or receipt.get("created_at"),
            step=receipt.get("step", ""),
            label=receipt.get("label") or labels.get(receipt.get("step"), receipt.get("step", "")),
            status=receipt.get("status", ""), purpose=receipt.get("purpose", ""))
    if approved and scope == ALL:
        add("node_approvals", max((one.get("decided_at") or 0) for one in approved),
            count=len(approved),
            labels=[one.get("label") or one.get("step", "") for one in approved])
    for message in _messages(where):
        payload = message.get("payload") or {}
        add("message", message.get("accepted_at"), step=message.get("step", ""),
            label=labels.get(message.get("step"), message.get("step", "")),
            scope=message.get("scope", ""), text=str(payload.get("text") or ""),
            who=message.get("who", ""), state=message.get("state", ""),
            reason=message.get("reason", ""),
            delivered=sum(1 for one in message.get("deliveries") or []
                          if one.get("state") == "delivered"))

    planned = _planned_questions(row.get("id", ""), memory_path)
    gates = _gate_verdicts(events)
    passed, files = [], []
    for step_id, step in run.steps.items():
        label = labels.get(step_id, step_id)
        written = [{"name": one.name or os.path.basename(one.path), "path": one.path,
                    "type": one.type, "bytes": one.bytes} for one in step.artifacts or []]
        if written:
            files.append({"step": step_id, "label": label, "status": step.status,
                          "plugin": step.plugin, "files": written})
        borrowed = bool(step.source_run) or (step.message or "").startswith("reused from ")
        if borrowed or step.started_at is None:
            continue
        at = step.ended_at or step.started_at
        common = {"step": step_id, "label": label, "plugin": step.plugin,
                  "status": step.status, "message": step.message, "files": written}
        if step.plugin in PERSON_PLUGINS:
            for asking in _askings(where, step, planned.get(step_id, "")):
                entries.append(dict(base, kind="ask", step=step_id, label=label,
                                    plugin=step.plugin, files=written, **asking))
        elif (step.plugin or "").startswith("agent."):
            outputs = step.outputs or {}
            add("agent", at, summary=str(outputs.get("summary") or ""),
                risk=str(outputs.get("risk") or ""), issues=_list(outputs.get("issues")),
                recommendations=_list(outputs.get("recommendations")),
                out_of_scope=_list(outputs.get("out_of_scope")),
                verified=outputs.get("verified"), attempts=outputs.get("attempts"),
                cost_usd=outputs.get("cost_usd"), effort=str(outputs.get("effort") or ""),
                prompts=_prompts(where, step), **common)
        elif scope == CONVERSATION:
            continue
        elif step.plugin == "check.gate":
            add("check", at, checks=gates.get(step_id, []), **common)
        elif step.status in (FAILED, TIMEOUT):
            add("failed", at, **common)
        elif step.status == SUCCESS:
            passed.append((at, label, step_id, written))
        elif step.status not in (SKIPPED, PENDING):
            add("step", at, **common)
    if passed and scope == ALL:
        add("steps", max(one[0] for one in passed), count=len(passed),
            steps=[{"step": one[2], "label": one[1], "files": one[3]} for one in passed])
    made = ({"run": row.get("id", ""), "cycle": row.get("cycle", ""),
             "cycle_name": row.get("name", ""), "run_dir": where,
             "started_at": row.get("started_at") or 0, "steps": files} if files else None)
    return entries, made


def _step_path(where, step_id, *parts):
    """A path in a step's folder, made without making the folder (see _events)."""
    return os.path.join(where, "steps", workspace_mod.safe_id(step_id), *parts)


def _list(value):
    if isinstance(value, list):
        return value
    return [] if value in (None, "") else [value]


def _events(where):
    """The run's own event log, less an agent's workings. [] when it has none."""
    # Joined here, not asked of workspace.log_path, which makes the folder: a
    # history only reads, and must leave a run directory as it found it.
    path = os.path.join(where, "logs", "cycle.jsonl")
    found = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if '"cycle.step.stage"' in line and any(one in line for one in _NOISE):
                    continue
                if not any(kind in line for kind in ('"cycle.run.', '"cycle.step.stage"')):
                    continue
                try:
                    found.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return found


def _gate_verdicts(events):
    """step id -> the checks its gate judged, as the gate itself reported them."""
    found = {}
    for event in events:
        if event.get("kind") == "cycle.step.stage" and event.get("phase") == "gate":
            body = event.get("body") or {}
            found[event.get("step", "")] = list(body.get("checks") or [])
    return found


def _messages(where):
    """Every message a person sent to this run, read only - never settled here."""
    path = interactions.path_of(os.path.join(where, ".execution"))
    if not os.path.exists(path):
        return []
    try:
        document = checkpoints._read(path)
    except checkpoints.CheckpointError:
        return []
    messages = document.get("messages") if isinstance(document, dict) else None
    return [one for one in messages or [] if isinstance(one, dict)]


def _node_approvals(where, run):
    """Every node approval asked for, from the policy journal when there is one.

    The journal is the canonical record: the run's metadata is a summary that a
    process killed at the wrong moment may not have written.
    """
    path = os.path.join(where, ".execution", "execution-policy.json")
    receipts = dict(run.node_approvals or {})
    if os.path.exists(path):
        try:
            journal = checkpoints._read(path)
        except checkpoints.CheckpointError:
            journal = {}
        receipts.update((journal or {}).get("node_approvals") or {})
    return sorted((one for one in receipts.values() if isinstance(one, dict)),
                  key=lambda one: one.get("created_at") or 0)


def _planned_questions(run_id, memory_path):
    """step id -> the question an unanswered approval of this run put on the plan."""
    try:
        return {str(item.get("step") or ""): str(item.get("question") or "")
                for item in planner.items(memory_path)
                if item.get("kind") == planner.APPROVAL and item.get("run") == run_id}
    except Exception:                        # noqa: BLE001 - a nicety, not a record
        return {}


def _askings(where, step, planned_question=""):
    """One entry per time a question step asked, oldest first.

    From ``asked.json`` when the run kept it - every asking, the rounds of a
    plan sent back included, each with the words it showed. A run from before
    it was kept has only the step's outputs: one entry, the answer without the
    question (or with the question an unanswered approval put on the plan).
    """
    outputs = step.outputs or {}
    asked = _asked(where, step.step_id)
    if not asked:
        return [{"at": step.ended_at or step.started_at or 0, "kept": False,
                 "question": planned_question, "detail": "", "options": [],
                 "answer": str(outputs.get("answer") or ""),
                 "text": str(outputs.get("text") or ""),
                 "value": outputs.get("value") if "value" in outputs else outputs.get("values"),
                 "who": str(outputs.get("who") or ""), "approved": outputs.get("approved"),
                 "automatic": bool(outputs.get("automatic")),
                 "answered": bool(outputs.get("answer") or outputs.get("text")
                                  or outputs.get("value")),
                 "status": step.status, "message": step.message}]
    found = []
    for number, one in enumerate(asked):
        last = number == len(asked) - 1
        found.append({
            "at": one.get("at") or step.started_at or 0, "kept": True,
            "question": str(one.get("question") or ""), "detail": str(one.get("detail") or ""),
            "options": list(one.get("options") or []), "answer": str(one.get("answer") or ""),
            "text": str(one.get("text") or ""), "value": one.get("chosen"),
            "who": str(one.get("who") or ""), "automatic": bool(one.get("automatic")),
            "answered": bool(one.get("answered")), "revision": bool(one.get("revision")),
            "feedback": str(one.get("feedback") or ""),
            # How this asking ended: the step's own verdict for the last one,
            # and for an earlier one only what came back - it was asked again.
            "status": step.status if last else "", "message": (step.message if last
                                                                else str(one.get("message") or ""))})
    return found


def _asked(where, step_id):
    """What a question step showed, each time it asked - [] for runs before it was kept."""
    path = _step_path(where, step_id, ASKED_FILE)
    if not os.path.exists(path):
        return []
    try:
        document = checkpoints._read(path)
    except checkpoints.CheckpointError:
        return []
    asked = document.get("asked") if isinstance(document, dict) else None
    return [one for one in asked or [] if isinstance(one, dict)]


def _prompts(where, step):
    """The prompt files an agent step's calls were given, relative to the run."""
    found = []
    for artifact in step.artifacts or []:
        if os.path.basename(artifact.path) == PROMPT_FILE:
            found.append(artifact.path)
    if found:
        return found
    base = _step_path(where, step.step_id, "operations")
    if not os.path.isdir(base):
        return []
    for folder, _dirs, names in os.walk(base):
        if PROMPT_FILE in names:
            found.append(workspace_mod.relative(where, os.path.join(folder, PROMPT_FILE)))
    return sorted(found)



# -- an agent's own account ----------------------------------------------------
#: Stage kinds a transcript keeps: what the agent weighed, what it said, which
#: tools it reached for and how each call began and ended. What a tool returned
#: is left out - it is most of the stream and none of the conversation.
TRANSCRIPT_KINDS = ("start", "thinking", "text", "review", "tool", "result")
#: The most stages one transcript carries, newest kept: a long implementation
#: says thousands of things, and the end of it is where it got to.
TRANSCRIPT_LIMIT = 400


def transcript(run_id, step_id, root=None, limit=TRANSCRIPT_LIMIT):
    """What one agent step weighed and said, call by call, read off its stream.

    ``run_id`` rather than a path, so a request cannot name a directory outside
    the runs. Each call (attempt, review) is one group, oldest first, with its
    prompt file when the call kept one. Returns ``{run, step, calls, cut}`` -
    ``cut`` counting the stages left out at the start for ``limit``.
    """
    from cycle.plugins import agent_stream
    base = os.path.realpath(workspace_mod.runs_root(root))
    where = os.path.realpath(workspace_mod.path_of(run_id, root))
    if os.path.dirname(where) != base or not os.path.isdir(where):
        raise SubjectError("There is no run %r." % run_id)
    operations = _step_path(where, step_id, "operations")
    calls = []
    for folder, dirs, names in os.walk(operations):
        dirs.sort()
        if "stdout.log" not in names:
            continue
        found = []
        try:
            with open(os.path.join(folder, "stdout.log"), encoding="utf-8",
                      errors="replace") as handle:
                for line in handle:
                    for stage in agent_stream.stages(line):
                        if stage.get("kind") in TRANSCRIPT_KINDS:
                            found.append({key: stage.get(key, "") for key in
                                          ("kind", "title", "detail", "status")})
        except OSError:
            continue
        prompt = os.path.join(os.path.dirname(folder), PROMPT_FILE)
        if not os.path.exists(prompt):
            prompt = os.path.join(folder, PROMPT_FILE)
        calls.append({"call": workspace_mod.relative(where, folder),
                      "started_at": os.path.getmtime(os.path.join(folder, "stdout.log")),
                      "prompt": (workspace_mod.relative(where, prompt)
                                 if os.path.exists(prompt) else ""),
                      "stages": found})
    calls.sort(key=lambda one: one["started_at"])
    total = sum(len(one["stages"]) for one in calls)
    cut, left = max(0, total - int(limit or 0)) if limit else 0, 0
    if cut:
        left = cut
        for one in calls:
            dropped = min(left, len(one["stages"]))
            one["stages"] = one["stages"][dropped:]
            left -= dropped
        calls = [one for one in calls if one["stages"]]
    return {"run": run_id, "step": step_id, "calls": calls, "cut": cut}


# -- notes --------------------------------------------------------------------
def notes_path(memory_path=None):
    """Beside the cycles' memory, wherever that has been put."""
    return os.path.join(os.path.dirname(os.path.abspath(memory.resolve_path(memory_path))),
                        NOTES_FILE)


def _notes_store(memory_path=None):
    try:
        return memory.load(notes_path(memory_path))
    except memory.MemoryError_:
        return {}


def notes(subject, memory_path=None):
    """One subject's notes, oldest first. [] when it has none."""
    record = _notes_store(memory_path).get(str(subject)) or {}
    return [dict(one) for one in record.get("notes") or [] if isinstance(one, dict)]


def add_note(subject, text, who="", memory_path=None):
    """Keep what a person wrote about a subject. Returns the note as kept.

    Added, never rewritten: a note is somebody's account at the time, and the
    history shows each one where it was written. Locked like the memory store
    it sits beside, because two windows adding a note at once must keep both.
    """
    subject = str(subject or "").strip()
    text = str(text or "").strip()
    if not subject:
        raise SubjectError("A note needs a subject.")
    if not text:
        raise SubjectError("A note needs some text.")
    if len(text) > MAX_NOTE:
        raise SubjectError("A note is at most %d characters, not %d." % (MAX_NOTE, len(text)))
    note = {"at": time.time(), "who": str(who or "").strip() or _who(), "text": text}
    try:
        with memory._change(notes_path(memory_path)) as store:
            record = store.setdefault(subject, {"created_at": note["at"], "notes": []})
            record.setdefault("notes", []).append(note)
            record["updated_at"] = note["at"]
    except memory.MemoryError_ as exc:
        raise SubjectError(str(exc))
    return dict(note)


def _who():
    try:
        return getpass.getuser()
    except Exception:                        # noqa: BLE001 - a name, not a gate
        return ""
