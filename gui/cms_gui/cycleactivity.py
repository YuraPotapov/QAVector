"""Operational projections shared by the Cockpit, header and global strip.

No core imports. Timers use receipts, bounded monotonic interpolation for live
views, and freeze on replay. Logs supply activity, never elapsed duration.
"""

import time

ACTIVE = ("running", "waiting")
FAILED = ("failed", "timeout", "interrupted")


def duration(milliseconds):
    if milliseconds is None:
        return "time unavailable"
    seconds = max(0, int(milliseconds / 1000))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return "%dd %02d:%02d" % (days, hours, minutes)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)
    return "%02d:%02d" % (minutes, seconds)


def interpolation(record, live, now=None, wall=None):
    if not live or record.get("_timing_paused") or record.get("status") not in ACTIVE:
        return 0
    received = record.get("_timing_received")
    if received is None:
        return 0
    delta = (time.monotonic() if now is None else now) - received
    wall_delta = (time.time() if wall is None else wall) - record.get("_timing_wall", 0)
    ttl = record.get("_timing_ttl_ms", 5000) / 1000
    # Never bridge a sleep, stale pipe or wall-clock correction.
    return max(0, delta * 1000) if 0 <= delta <= ttl and abs(wall_delta - delta) <= ttl else 0


def elapsed(step, live=False, **clocks):
    value = step.get("active_elapsed_ms")
    return None if value is None else value + interpolation(step, live, **clocks)


def label(cycle, step_id, plugins=None):
    node = next((node for node in (cycle.get("graph") or {}).get("nodes", [])
                 if node.get("id") == step_id), {})
    title = node.get("label")
    plugin = node.get("plugin") or (cycle.get("steps", {}).get(step_id) or {}).get("plugin")
    return (title if title and title != step_id else
            (plugins or {}).get(plugin) or (cycle.get("plugin_names") or {}).get(plugin) or title or step_id)


def status(step, stopping=False):
    raw = step.get("status", "pending")
    if raw == "cancelled" and step.get("interrupted"):
        return "Interrupted"
    if stopping and raw in ACTIVE:
        return "Stopping"
    state = (step.get("progress") or {}).get("state")
    if raw in ACTIVE and state in ("waiting_for_approval", "waiting_for_input"):
        return {"waiting_for_approval": "Waiting for approval",
                "waiting_for_input": "Waiting for input / choice"}[state]
    return {"running": "Running", "waiting": "Waiting", "success": "Completed",
            "pending": "Not started", "timeout": "Timed out", "cancelled": "Stopped",
            "skipped": "Not run"}.get(raw, raw.replace("_", " ").title())


def active(cycle):
    if cycle.get("paused"):
        return []
    steps = cycle.get("steps") or {}
    return sorted((name for name, step in steps.items() if step.get("status") in ACTIVE),
                  key=lambda name: (steps[name].get("started_at") or 0, name))


def primary(cycle):
    names = active(cycle)
    steps = cycle.get("steps") or {}
    decisions = [name for name in names if (steps[name].get("progress") or {}).get("state")
                 in ("waiting_for_approval", "waiting_for_input")]
    chosen = cycle.get("_activity_selected")
    return (decisions[0] if decisions else chosen if chosen in names else
            names[0] if names else "")


def stopping(cycle):
    if cycle.get("status") in ("success", "failed", "cancelled", "timeout", "interrupted", "error"):
        return False
    if cycle.get("_stopping") or cycle.get("execution_status") == "stopping":
        return True
    graph = cycle.get("graph") or {}
    # The core also drains the run after on_failure: stop (including a
    # declined explicit gate). It does not emit the toolbar's local flag.
    # Execution phases pause instead, except within a bounded run action.
    if cycle.get("paused") or ((cycle.get("phase_definition") or graph.get("phases")) and not cycle.get("action_id")):
        return False
    failed = {name for name, step in (cycle.get("steps") or {}).items()
              if step.get("status") in ("failed", "timeout")}
    return any(node.get("id") in failed and node.get("on_failure", "stop") == "stop"
               for node in graph.get("nodes", []))


def tone(cycle, live=False):
    """The variant of the pill that shows :func:`header`, in the Cockpit's
    colours: in flight, held up, failed, done, or simply stopped."""
    raw = cycle.get("status")
    steps = cycle.get("steps") or {}
    waiting = any(one.get("status") == "pending" and (steps.get(one.get("step")) or {}).get("status")
                  not in ("skipped", "cancelled") for one in (cycle.get("node_approvals") or {}).values())
    if raw == "cancelled":
        return "neutral"
    if stopping(cycle) or cycle.get("paused") or cycle.get("pause_requested") or waiting \
            or raw in ("paused", "interrupted"):
        return "warn"
    if live:
        return "outline"
    if raw in ("failed", "timeout", "error"):
        return "bad"
    return "accent" if raw == "success" else "neutral"


def header(cycle, live=False, plugins=None):
    if cycle.get("status") == "cancelled":
        return "Run stopped"
    if stopping(cycle):
        return "Stopping cycle…"
    if cycle.get("paused"):
        if cycle.get("pause_reason") == "approval_declined":
            return "Paused · node approval declined · " + (cycle.get("next_action") or "Review the node approval")
        phase_id = cycle.get("last_completed_phase") if cycle.get("pause_reason") == "phase_boundary" else cycle.get("current_phase")
        phase = next((phase for phase in cycle.get("phase_definition", []) if phase.get("id") == phase_id), {})
        preposition = " after " if cycle.get("pause_reason") == "phase_boundary" else " in "
        return "Paused%s · %s" % (preposition + (phase.get("title") or phase_id) if phase_id else "", cycle.get("next_action") or "Continue run")
    steps = cycle.get("steps") or {}
    approvals = [one for one in (cycle.get("node_approvals") or {}).values() if one.get("status") == "pending"
                 and (steps.get(one.get("step")) or {}).get("status") not in ("skipped", "cancelled")]
    if approvals:
        return "%s · %s" % ("Waiting for node approval" if live else "Resume to review node approval",
                            ", ".join(one.get("label") or one.get("step", "") for one in approvals))
    name = primary(cycle) if live else ""
    if name:
        step = cycle["steps"][name]
        extra = len(active(cycle)) - 1
        return "%s %s, %s%s" % (
            label(cycle, name, plugins), duration(elapsed(step, live)),
            "Pausing" if cycle.get("pause_requested") else status(step, cycle.get("_stopping", False)),
            " · +%d parallel" % extra if extra else "")
    raw = cycle.get("status")
    if raw == "success":
        from .cycleresult import completion
        result = completion(cycle)[0]
        elapsed_ms = cycle.get("active_elapsed_ms") or 0
        return (result + " · " + duration(elapsed_ms)
                if elapsed_ms >= 1000 and cycle.get("outcome") not in ("no_work_found", "no_work_performed") else result)
    if raw == "paused":
        return "Paused"
    if raw == "interrupted":
        interrupted = [(name, step) for name, step in (cycle.get("steps") or {}).items() if step.get("interrupted")]
        last = max(interrupted, key=lambda pair: pair[1].get("started_at") or 0) if interrupted else None
        return ("Waiting to resume %s %s, Interrupted" % (label(cycle, last[0], plugins), duration(elapsed(last[1])))
                if last else "Waiting to resume · Interrupted")
    failed = next((name for name, step in (cycle.get("steps") or {}).items()
                   if step.get("status") in FAILED), "")
    if failed:
        return "%s %s, %s" % (label(cycle, failed, plugins),
                               duration(elapsed(cycle["steps"][failed])), status(cycle["steps"][failed]))
    return "Starting cycle…" if live else {"cancelled": "Run stopped", "failed": "Run failed",
                                           "timeout": "Run timed out", "error": "Run could not start"}.get(raw, "No active run")


def activity_text(step, live=False, threshold=60, now=None):
    stamp = step.get("last_activity_at")
    age = max(0, (time.time() if now is None else now) - stamp) if stamp else None
    text = ("Activity unavailable" if age is None else
            ("No output for " if age >= threshold else "Activity ") + duration(age * 1000)
            + (" ago" if age < threshold else ""))
    remaining = step.get("timeout_remaining_ms")
    if remaining is not None:
        remaining = max(0, remaining - interpolation(step, live))
        text += " · timeout in " + duration(remaining) if live else " · timeout remaining " + duration(remaining)
    elif live:
        text += " · no timeout declared"
    return text


def causes(cycle, step_id):
    """A bounded causal path; malformed legacy cycles cannot recurse forever."""
    path, seen = [], set()
    steps = cycle.get("steps") or {}
    while step_id and step_id not in seen and step_id in steps:
        seen.add(step_id)
        step = steps[step_id]
        path.append((step_id, step.get("message") or step.get("reason_code") or status(step)))
        step_id = step.get("blocked_by")
    return path


def next_nodes(cycle):
    """Dependency frontier, not a claim about unevaluated conditions/approvals."""
    if stopping(cycle) or cycle.get("status") == "cancelled":
        return []
    steps = cycle.get("steps") or {}
    selected = cycle.get("selected_steps")
    nodes = (cycle.get("graph") or {}).get("nodes", [])
    pending = [node for node in nodes if (steps.get(node.get("id")) or {}).get("status") == "pending"
               and (selected is None or node.get("id") in selected) and not node.get("disabled")]
    frontier = [node for node in pending if not any(
        (steps.get(name) or {}).get("status") == "pending" for name in node.get("needs") or [])]
    return frontier or pending[:1]
