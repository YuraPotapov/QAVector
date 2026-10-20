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
import copy
import json
import os

from . import history as history_mod, cycleresult
from .runner import TIMING_FIELDS

#: What the file is called inside a run's directory. The core's own
#: ``cycle/workspace.py`` decides this; mirrored rather than imported, like
#: every other path the GUI shares with the core.
LOG_NAME = os.path.join("logs", "cycle.jsonl")

#: The event-tail budget. The model caps what it keeps anyway
#: (``runner.CYCLE_STAGES`` and the log limits beside it), so reading more only
#: costs the parse - and a run that emitted a hundred thousand events must not
#: be something the window waits for on the way up. The first line is always
#: kept whatever this is: it is ``cycle.run.start``, which carries the graph
#: every other event is about. At most one extra run.mode is kept too: its
#: retained results and rerun reasons are provenance, not replaceable output.
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

    Keep the first line, latest run mode and bounded tail. The mode's exact
    retained results and rerun reasons are not duplicated in metadata.json.
    """
    try:
        with open(log_path(run_dir), encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            # Over the handle, so a long file is walked rather than held.
            rest = collections.deque(maxlen=max(limit - 1, 1))
            mode = None
            for position, line in enumerate(fh, 1):
                rest.append((position, line))
                # Avoid parsing every output event just to retain one semantic
                # record. Confirm the kind so log text cannot impersonate it.
                if '"cycle.run.mode"' in line:
                    try:
                        candidate = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(candidate, dict) and candidate.get("kind") == "cycle.run.mode":
                        mode = (position, line)
    except OSError:
        return []
    found = []
    lines = dict([(0, first)] + ([mode] if mode is not None else []) + list(rest))
    for _, line in sorted(lines.items()):
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


def saved_interactions(run_dir, run_id):
    """The durable record wins over missing or stale diagnostic events.

    None means an older run or unreadable file: keep the events we did see.
    An empty list is authoritative too, and removes phantom event-only rows.
    """
    path = os.path.join(run_dir, ".execution", "interactions.json")
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        if (not isinstance(document, dict) or document.get("schema") != 1
                or document.get("run_id") != run_id
                or not isinstance(document.get("messages"), list)):
            return None
        messages = document["messages"]
        if any(not isinstance(one, dict) or not one.get("id")
               or one.get("run_id") != run_id for one in messages):
            return None
        return sorted(messages, key=lambda one: one.get("seq") or 0)
    except (OSError, ValueError, TypeError):
        return None


def _phase_current(receipt, steps):
    for name, evidence in (receipt.get("evidence") or {}).items():
        step = steps.get(name)
        if step is None or any(step.get(key, 1 if key == "execution_generation" else "") != evidence[key]
                               for key in ("status", "execution_generation", "definition_digest", "input_digest")
                               if key in evidence):
            return False
    return True


def _saved_policy(run_dir, run_id):
    try:
        with open(os.path.join(run_dir, ".execution", "execution-policy.json"), encoding="utf-8") as handle:
            policy = json.load(handle)
        if isinstance(policy, dict) and policy.get("schema") == 1 and policy.get("run_id") == run_id:
            return {key: policy[key] for key in ("initial_execution_mode", "execution_mode", "execution_policy_version", "node_approvals", "policy_changes") if key in policy}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _restore_receipts(run_dir, state):
    """Restore durable results and semantics omitted from a bounded event tail."""
    try:
        with open(os.path.join(run_dir, "metadata.json"), encoding="utf-8") as handle:
            record = json.load(handle)
        if record.get("id") != state.cycle.get("run_id"):
            return
        record.update(_saved_policy(run_dir, record["id"]))
        for key in ("active_elapsed_ms", "execution_segments", "selected_steps", "plugin_names",
                    "subject", "revisions"):
            if key in record:
                state.cycle[key] = copy.deepcopy(record[key])
        if "resume_count" in record:
            count = record["resume_count"]
            state.cycle["resume_count"] = count
            if isinstance(count, int) and count > 0:
                mode = state.cycle.get("mode") or {}
                if mode.get("mode") != "resume" or mode.get("resume_count") != count:
                    # A crash before the latest mode event must not attribute
                    # a previous attempt's retained work/reasons to this one.
                    state.cycle["mode"] = {"mode": "resume", "resume_count": count,
                                           "source_run": record["id"], "kept": [], "rerun": []}
        for phase in record.get("phase_definition") or []:
            name = phase.get("id", "")
            if not name or os.path.basename(name) != name:
                continue
            try:
                with open(os.path.join(run_dir, ".execution", "phases", name + ".json"), encoding="utf-8") as handle:
                    receipt = json.load(handle)
                if receipt.get("phase") == name:
                    saved = (record.get("phase_receipts") or {}).get(name) or {}
                    generation, previous = receipt.get("generation", 0), saved.get("generation", 0)
                    if generation >= previous:
                        if generation == previous and saved.get("invalidated"):
                            receipt["invalidated"] = True
                        record.setdefault("phase_receipts", {})[name] = receipt
            except (OSError, ValueError, AttributeError):
                pass
        for name, saved in (record.get("steps") or {}).items():
            step = state.cycle.get("steps", {}).get(name)
            if step is None or not isinstance(saved, dict):
                continue
            if os.path.basename(name) == name:
                try:
                    with open(os.path.join(run_dir, ".execution", "steps", name + ".json"), encoding="utf-8") as handle:
                        receipt = json.load(handle)
                    if isinstance(receipt, dict) and receipt.get("step_id") == name:
                        saved = receipt
                except (OSError, ValueError):
                    pass
            for key in TIMING_FIELDS + ("status", "plugin", "message", "outputs", "artifacts", "source_run", "runtime",
                                        "started_at", "ended_at", "reason_code", "blocked_by", "duration_ms",
                                        "definition_digest", "input_digest"):
                if key in saved:
                    step[key] = saved[key]
            if "attempts" in saved:
                step["attempt"] = saved["attempts"]
        for receipt in (record.get("phase_receipts") or {}).values():
            if not _phase_current(receipt, state.cycle["steps"]):
                receipt["invalidated"] = True
        if record.get("status") in ("success", "failed", "cancelled", "timeout", "interrupted"):
            tally = record.get("tally") or {}
            counts = {key: tally.get(status, sum(step.get("status") == status for step in state.cycle["steps"].values()))
                      for key, status in (("passed", "success"), ("failed", "failed"), ("skipped", "skipped"))}
            state.handle(dict(record, kind="cycle.run.end", run_id=record["id"], **counts))
        else:
            # A headless safe pause has already emitted run.end with status
            # waiting. Its receipts can still be newer than that journal event;
            # restoring them must not claim that the executor is live again.
            for key in cycleresult.DETAIL_FIELDS:
                if key in record:
                    state.cycle[key] = record[key]
            cycleresult.update(state.cycle, record if state.cycle_running else None)
    except (OSError, ValueError, AttributeError, TypeError):
        pass


def restore(run_dir, state, limit=MAX_EVENTS, events=None):
    """Replay ``run_dir`` into ``state``. True when there was a run to restore.

    The model's signals are held back while the events go in and sent once at
    the end: a page listening to them would otherwise repaint itself several
    thousand times to arrive at the one view the reader is about to see.

    ``events`` is what :func:`events_of` read, when it was read somewhere else
    - on a thread, so a long stream does not stop the window while it is
    parsed. Feeding the model stays here, on the thread the model lives on.
    """
    if events is None:
        events = events_of(run_dir, limit)
    if not events:
        return False
    state.reset_cycle()
    state.blockSignals(True)
    try:
        for event in events:
            state.handle(event)
        if state.cycle is not None:
            # Receipts may be newer than a truncated diagnostic event stream.
            _restore_receipts(run_dir, state)
            state.cycle.update(_saved_policy(run_dir, state.cycle.get("run_id")))
            messages = saved_interactions(run_dir, state.cycle.get("run_id"))
            if messages is not None:
                state.restore_interactions(messages)
            state.cycle["interaction_offers"] = {}
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
    state.cycle["run_dir"] = run_dir
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
