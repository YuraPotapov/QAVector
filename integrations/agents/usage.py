"""What agent work used and cost, summed from the project's record (QCR ADR-008).

Every agent turn - a chat, a task or a question handed to another agent, a
Jira issue's conversation, a cycle step - is a turn of a conversation in the
agent-sessions record. That record is the one source; this module only adds it
up, the same way for every view.

**Tokens** (ADR-008 D2): ``input`` is what went in uncached, ``cache_read`` and
``cache_write`` the cached parts, ``output`` what came out - thinking included -
and ``thinking`` the part of output spent thinking, never added again. The total
is input + cache_read + cache_write + output.

**Cost** (D3) says what it is. ``estimate`` is the agent program's own figure,
priced as its API would be: on a subscription it is not what is charged.
``priced`` would be QAVector's own, from a price list - none is kept yet.
``unknown`` is a turn that reported none: it is counted as such, never as 0.

**Woken turns** (ADR-009 W.3): turns QAVector started by itself when tasks a
conversation handed on had ended are counted apart too (``woken_turns``).

**Roles** (ADR-010 R.3): a task handed to a role is a conversation that keeps
the role (``source.role``); ``by=("role",)`` sums by it.

**Own and inclusive** (D6): a conversation's own turns, and those with the
conversations it handed work to (``source.parent``). The two are never added
to each other.
"""

from . import sessions

ESTIMATE, PRICED, UNKNOWN = "estimate", "priced", "unknown"
#: What a conversation is, from where it came (ADR-008 D5).
KINDS = ("chat", "delegated", "message", "jira", "cycle", "record")
#: A cycle step's conversation: ``cycle:<cycle>:<run>:<step>``.
CYCLE_REF = "cycle:"


def cost_of(turn):
    """``(value, kind)`` of one turn. A turn recorded before turns said which
    kind theirs was is the program's estimate when it has a figure and its
    program reports one; otherwise unknown."""
    value = turn.get("cost_usd")
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    kind = turn.get("cost_kind")
    if kind in (ESTIMATE, PRICED):
        return (value, kind) if number else (None, UNKNOWN)
    if kind is None and number and turn.get("cost_scope") != UNKNOWN:
        return value, ESTIMATE
    return None, UNKNOWN


def kind_of(session):
    """What a conversation is, from its source - also for one recorded before
    sources said so."""
    source = sessions.normal_source(session.get("source"))
    ref, label = source.get("ref") or "", source.get("label") or ""
    if label == "Delegated":
        return "delegated"
    if label == "Message":
        return "message"
    if ref.startswith("jira:"):
        return "jira"
    if ref.startswith(CYCLE_REF) or label.startswith("Cycle "):
        return "cycle"
    return "record" if sessions.is_record(session) else "chat"


def cycle_of(session):
    """``(cycle, run, step)`` of a cycle step's conversation, from its ref; ``None``."""
    ref = sessions.normal_source(session.get("source")).get("ref") or ""
    if not ref.startswith(CYCLE_REF):
        return None
    parts = ref[len(CYCLE_REF):].split(":")
    return tuple(parts) if len(parts) == 3 else None


def cycle_ref(cycle, run, step):
    return "%s%s:%s:%s" % (CYCLE_REF, cycle, run, step)


def tokens_total(tokens):
    tokens = tokens or {}
    return sum(tokens.get(key) or 0 for key in ("input", "cache_read", "cache_write", "output"))


def _empty():
    return {"conversations": 0, "turns": 0, "failed_turns": 0, "woken_turns": 0, "seconds": 0.0,
            "tokens": {key: 0 for key in sessions.TOKEN_KEYS}, "tokens_total": 0,
            "estimate_usd": 0.0, "priced_usd": 0.0, "unknown_cost_turns": 0}


def _add(total, turn):
    total["turns"] += 1
    if turn.get("trigger"):
        total["woken_turns"] += 1
    if turn.get("state") not in (sessions.COMPLETED, sessions.RUNNING):
        total["failed_turns"] += 1
    started, ended = turn.get("started"), turn.get("ended")
    if isinstance(started, (int, float)) and isinstance(ended, (int, float)) and ended >= started:
        total["seconds"] += ended - started
    for key, value in (turn.get("tokens") or {}).items():
        if key in total["tokens"] and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            total["tokens"][key] += value
    value, kind = cost_of(turn)
    if kind == ESTIMATE:
        total["estimate_usd"] += value
    elif kind == PRICED:
        total["priced_usd"] += value
    else:
        total["unknown_cost_turns"] += 1


def _finish(total):
    total["tokens_total"] = tokens_total(total["tokens"])
    total["estimate_usd"] = round(total["estimate_usd"], 6)
    total["priced_usd"] = round(total["priced_usd"], 6)
    total["seconds"] = round(total["seconds"], 3)
    return total


def summarize(found, since=None, until=None, connection="", model="", kind="", cycle="", run="", role="",
              by=("model", "connection", "kind")):
    """Totals over conversations ``found`` (as :func:`sessions.described` gives
    them), with the turns inside a period, and breakdowns ``by`` model,
    connection, kind or cycle. A turn without a model is under ``(default)``:
    the program's own choice."""
    total = _empty()
    breakdowns = {name: {} for name in by}
    for session in found:
        what = kind_of(session)
        if kind and what != kind:
            continue
        if connection and session.get("connection") != connection:
            continue
        if role and (session.get("source") or {}).get("role") != role:
            continue
        at = cycle_of(session)
        if (cycle and (at is None or at[0] != cycle)) or (run and (at is None or at[1] != run)):
            continue
        counted = False
        for turn in session.get("turns") or []:
            started = turn.get("started") or 0
            if (since is not None and started < since) or (until is not None and started >= until):
                continue
            used = turn.get("model") or session.get("model") or "(default)"
            if model and used != model:
                continue
            _add(total, turn)
            counted = True
            given = (session.get("source") or {}).get("role") or ""      # not the role filter
            keys = {"model": used, "connection": session.get("connection_name") or session.get("connection") or "",
                    "kind": what, "cycle": at[0] if at else "", "role": given}
            for name in by:
                if (name == "cycle" and not at) or (name == "role" and not given):
                    continue
                part = breakdowns[name].setdefault(keys[name], _empty())
                _add(part, turn)
                part.setdefault("_sessions", set()).add(session.get("id"))
        if counted:
            total["conversations"] += 1
    for name, parts in breakdowns.items():
        for part in parts.values():
            part["conversations"] = len(part.pop("_sessions", ()))
            _finish(part)
    return dict(_finish(total), by={name: dict(sorted(parts.items())) for name, parts in breakdowns.items()})


def _all(root):
    return sessions.listing(root)["sessions"]


def children_of(found, session_id):
    """The conversations ``session_id`` handed work to, and theirs in turn."""
    made, waiting = [], [session_id]
    while waiting:
        parent = waiting.pop()
        for one in found:
            if (one.get("source") or {}).get("parent") == parent and one not in made:
                made.append(one)
                waiting.append(one["id"])
    return made


def conversation(root, session_id, found=None):
    """One conversation's own figures, and inclusive of what it handed on."""
    found = _all(root) if found is None else found
    own = next((one for one in found if one.get("id") == session_id), None)
    if own is None:
        raise sessions.SessionError("There is no session %s." % session_id, "not_found")
    children = children_of(found, session_id)
    return {"session": session_id, "own": summarize([own], by=()),
            "inclusive": summarize([own] + children, by=()),
            "children": [one["id"] for one in children]}


def for_ref(root, ref, found=None):
    """The conversations with exactly ``ref`` - an issue's, say - own and
    inclusive of what they handed on."""
    found = _all(root) if found is None else found
    owned = [one for one in found if ref in sessions.refs_of(one)]
    children = []
    for one in owned:
        children += [child for child in children_of(found, one["id"]) if child not in children]
    return {"ref": ref, "own": summarize(owned, by=("model",)),
            "inclusive": summarize(owned + children, by=("model",)),
            "children": [one["id"] for one in children]}


def summary(root, **filters):
    return summarize(_all(root), **filters)
