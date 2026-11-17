"""What agent work may cost before the person is asked (QCR ADR-013).

A budget is an amount of est. cost (USD), of tokens, or both - tokens because
some programs (Codex) report no cost, and a budget in dollars alone cannot see
their work. Whichever amount is reached first counts.

* A **task**: everything its child conversation used, all its turns.
* A **conversation**: its own turns and everything it handed to other agents.

Nothing is cut off. At 80% the person is warned; at 100% the next turn that
would spend more waits for the person: Continue raises the budget by the
amount it was first set to, Stop ends it. A running turn is not stopped
midway - this is **best effort**, checked before a turn.

A conversation's budget is kept beside its record (``budget.json``), not in it:
a turn holds and rewrites its record while it runs, and a budget raised during
that turn must not be lost when it saves. Without one, the connection's
default applies; by default there is none.
"""

import json
import os

from integrations.agents import sessions

#: Where the person is warned, as a share of the budget.
WARN_SHARE = 0.8
#: Said wherever a budget's limit is: it is checked before a turn, not during one.
ENFORCEMENT = "best_effort"
_FILE = "budget.json"
_MAX_USD, _MAX_TOKENS = 100000.0, 10 ** 12


def normal(value):
    """``{"usd": float, "tokens": int, "step_usd", "step_tokens"}`` with only what
    is set - or ``{}``: no budget. The steps are what Continue adds."""
    if not isinstance(value, dict):
        return {}
    made = {}
    try:
        usd = float(value.get("usd") or 0)
    except (TypeError, ValueError):
        usd = 0
    try:
        tokens = int(value.get("tokens") or 0)
    except (TypeError, ValueError):
        tokens = 0
    if 0 < usd <= _MAX_USD:
        made["usd"] = round(usd, 4)
        made["step_usd"] = round(float(value.get("step_usd") or usd), 4)
    if 0 < tokens <= _MAX_TOKENS:
        made["tokens"] = tokens
        made["step_tokens"] = int(value.get("step_tokens") or tokens)
    return made


def smaller(*budgets):
    """Per amount, the smallest of those that set it: a task never gets more
    than its conversation has left, nor more than it was given."""
    made = {}
    for key in ("usd", "tokens"):
        values = [one[key] for one in budgets if one and one.get(key) is not None]
        if values:
            made[key] = min(values)
    return normal(made)


def spent_of(totals):
    """What figures from :mod:`usage` come to: est./priced dollars, tokens, and
    how many turns reported no cost (their dollars are unknown, never 0)."""
    totals = totals or {}
    return {"usd": round((totals.get("estimate_usd") or 0) + (totals.get("priced_usd") or 0), 6),
            "tokens": int(totals.get("tokens_total") or 0),
            "unknown_cost_turns": int(totals.get("unknown_cost_turns") or 0)}


def state(spent, budget):
    """How far ``spent`` is into ``budget``: the larger share of the two amounts."""
    budget = normal(budget)
    shares = []
    if budget.get("usd"):
        shares.append(spent.get("usd", 0) / budget["usd"])
    if budget.get("tokens"):
        shares.append(spent.get("tokens", 0) / budget["tokens"])
    share = max(shares) if shares else None
    return {"budget": budget, "spent": dict(spent), "share": share,
            "warn": share is not None and share >= WARN_SHARE, "reached": share is not None and share >= 1,
            "enforcement": ENFORCEMENT}


def left(spent, budget):
    """What is left of ``budget`` after ``spent`` - never below 0."""
    budget = normal(budget)
    made = {}
    if budget.get("usd"):
        made["usd"] = max(budget["usd"] - spent.get("usd", 0), 0.0001)
    if budget.get("tokens"):
        made["tokens"] = max(budget["tokens"] - spent.get("tokens", 0), 1)
    return made


def raised(budget):
    """Continue: the budget plus what it was first set to."""
    budget = normal(budget)
    if budget.get("usd"):
        budget["usd"] = round(budget["usd"] + budget["step_usd"], 4)
    if budget.get("tokens"):
        budget["tokens"] = budget["tokens"] + budget["step_tokens"]
    return budget


# -- kept beside a conversation's record ---------------------------------------------
def load(root, session_id, connection=None, kind="conversation"):
    """A conversation's budget: its own, else its connection's default for a
    conversation (or a task), else none."""
    try:
        path = os.path.join(sessions.directory(root, session_id), _FILE)
        with open(path, encoding="utf-8") as handle:
            return normal(json.load(handle))
    except (OSError, ValueError, sessions.SessionError):
        pass                              # none of its own: its connection's default
    return normal((connection or {}).get("budget_task" if kind == "task" else "budget_conversation"))


def save(root, session_id, budget):
    """Set a conversation's own budget; an empty one is "none", not the default."""
    folder = sessions.directory(root, session_id)
    if not os.path.isdir(folder):
        raise sessions.SessionError("There is no session %s." % session_id, "not_found")
    path = os.path.join(folder, _FILE)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(normal(budget) or {"none": True}, handle)
    os.replace(temporary, path)
    return load(root, session_id)


def of_task(root, session_id, connection=None):
    """A task's child conversation: what it spent, against its budget."""
    from integrations.agents import usage
    totals = usage.conversation(root, session_id, found=[sessions.load(root, session_id)])["own"]
    return state(spent_of(totals), load(root, session_id, connection, kind="task"))


def of_conversation(root, session_id, connection=None, found=None):
    """A conversation, inclusive of what it handed on, against its budget."""
    from integrations.agents import usage
    totals = usage.conversation(root, session_id, found=found)["inclusive"]
    return state(spent_of(totals), load(root, session_id, connection))


def text(spent):
    """``$1.20 / 12k`` - or ``- / 12k`` when nothing reported a cost."""
    usd, tokens = spent.get("usd") or 0, spent.get("tokens") or 0
    return "%s / %s" % (("$%.2f" % usd) if usd else "-", count(tokens))


def count(tokens):
    """``950``, ``12k``, ``1.4M``."""
    tokens = int(tokens or 0)
    if tokens >= 1000000:
        return ("%.1fM" % (tokens / 1000000)).replace(".0M", "M")
    if tokens >= 1000:
        return "%dk" % round(tokens / 1000)
    return str(tokens)
