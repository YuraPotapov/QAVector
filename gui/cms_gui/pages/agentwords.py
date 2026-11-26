"""How Integrations -> Agents says things: a status, a folder, a time, a cost.

Words only - no widget is made here - so the tabs that show a connection, a
turn or a conversation say the same thing the same way, and a test can ask
what a row would read without building one.
"""

import datetime
import os

#: The agents offered before the core has said which it knows: enough for the
#: dialog to open at once. (name, kind)
KINDS = (("Claude Code", "claude"),)

#: What the Status column says for a check that failed, by its kind.
FAILED = {"signin": "Not signed in", "not_installed": "Not installed"}


def kind_name(kind, kinds=()):
    for one in kinds or ():
        if one.get("kind") == kind:
            return one.get("name") or kind
    return dict((value, name) for name, value in KINDS).get(kind, kind or "")


def program_text(connection):
    """Where the CLI is: the path somebody gave, or found when it is used."""
    return connection.get("binary") or "Found automatically"


def model_text(connection):
    """The model and effort a connection asks for; blank is the CLI's own."""
    parts = [connection.get("model") or "CLI default"]
    if connection.get("effort"):
        parts.append("%s effort" % connection["effort"])
    return " · ".join(parts)


def status_text(connection):
    """What the Status column says: how the last Test went, and when."""
    check = connection.get("last_check") or {}
    if not check:
        return "Not checked"
    when = str(check.get("at") or "")[:10]
    if check.get("ok"):
        return "Signed in as %s · %s" % (check.get("account") or "?", when)
    return "%s · %s" % (FAILED.get(check.get("kind"), "Problem"), when)


def folder_text(path):
    """A folder as a row has room for it: from ``~`` when it is under home.

    The end of a path is what tells two projects apart, and a column cuts a
    line at its end; the whole path is the cell's tooltip.
    """
    path = str(path or "")
    home = os.path.expanduser("~")
    if home not in ("", "/") and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def when_text(stamp):
    """When a turn began, as the log says it."""
    try:
        return datetime.datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def took_text(started, ended):
    """How long a turn took, the way a person says it."""
    try:
        seconds = max(0, int(round(float(ended) - float(started))))
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return "%d s" % seconds
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return "%d min %02d s" % (minutes, seconds)
    return "%d h %02d min" % divmod(minutes, 60)


def cost_text(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    return "$%.2f" % value if value >= 0.01 or value == 0 else "< $0.01"


def result_text(turn):
    """How a turn ended, with what went wrong when something did."""
    words = {"completed": "Done", "failed": "Failed", "stopped": "Stopped",
             "interrupted": "Interrupted", "running": "Running"}
    said = words.get(turn.get("state"), str(turn.get("state") or ""))
    error = " ".join(str(turn.get("error") or "").split())
    return "%s - %s" % (said, error) if error and turn.get("state") == "failed" else said


#: The tools a narrower access mode leaves out, and the mode that has them.
SHELL_TOOLS = ("Bash",)
WRITING_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def refused_text(mode, tool, reason=""):
    """Why a call the agent made was not run, in the terms of the access mode.

    The program's own reason reads as a request to approve, and nothing in a
    conversation can approve a call - so the mode's words come first, and the
    program's reason is kept only when the mode does not explain it.
    """
    if tool in SHELL_TOOLS and mode in ("read", "edit"):
        return "Not allowed in %s: commands need Full access." % mode.capitalize()
    if tool in WRITING_TOOLS and mode == "read":
        return "Not allowed in Read: changing files needs Edit or Full access."
    reason = " ".join(str(reason or "").split())
    said = "Not run: the agent program would not run this without approval, " \
           "which a conversation cannot give."
    return "%s %s" % (said, reason) if reason else said


def ago_text(stamp, now=None):
    """When a conversation was last used, the way a person says it."""
    try:
        stamp = float(stamp)
    except (TypeError, ValueError):
        return ""
    passed = (float(now) if now is not None else datetime.datetime.now().timestamp()) - stamp
    if passed < 60:
        return "just now"
    if passed < 3600:
        return "%d min ago" % (passed // 60)
    if passed < 86400:
        return "%d h ago" % (passed // 3600)
    try:
        return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def clock_text(stamp):
    """The time of day something was said."""
    try:
        return datetime.datetime.fromtimestamp(float(stamp)).strftime("%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def end_text(event, started=None):
    """What the last line of a turn adds to "Finished": steps, cost, how long,
    and how full the agent's context was when it ended."""
    parts = []
    steps = event.get("steps")
    if isinstance(steps, int) and not isinstance(steps, bool):
        parts.append("%d step%s" % (steps, "" if steps == 1 else "s"))
    cost = cost_text(event.get("cost_usd"))
    if cost:
        parts.append("est. " + cost)
    took = took_text(started, event.get("at")) if started is not None else ""
    if took:
        parts.append(took)
    context = context_text(event.get("context"))
    if context:
        parts.append("context " + context)
    return " · ".join(parts)


def count_text(number):
    """A token count as it is read at a glance: 933, 12.3k, 1.2M."""
    if not isinstance(number, int) or isinstance(number, bool) or number < 0:
        return ""
    if number < 1000:
        return str(number)
    if number < 1000000:
        return ("%.1fk" % (number / 1000.0)).replace(".0k", "k") if number < 10000 \
            else "%dk" % round(number / 1000.0)
    return ("%.1fM" % (number / 1000000.0)).replace(".0M", "M")


#: How full a context is when it is worth saying so, and when it is nearly gone
#: - shares of the model's window.
CONTEXT_WARN, CONTEXT_FULL = 0.80, 0.95


def context_share(context):
    """How much of its window the context fills, 0-1 and more; None when the
    window is not known."""
    context = context or {}
    used, window = context.get("used"), context.get("window")
    if not isinstance(used, int) or not isinstance(window, int) or isinstance(used, bool) \
            or isinstance(window, bool) or window <= 0 or used <= 0:
        return None
    return used / window


def context_level(context):
    """``"full"`` from 95%, ``"warn"`` from 80%, otherwise ``""``."""
    share = context_share(context)
    if share is None:
        return ""
    return "full" if share >= CONTEXT_FULL else "warn" if share >= CONTEXT_WARN else ""


def context_text(context):
    """How full the agent's context is: ``25k of 200k``, or the size alone."""
    context = context or {}
    used = count_text(context.get("used"))
    if not used or not context.get("used"):
        return ""
    window = count_text(context.get("window")) if context.get("window") else ""
    return "%s of %s" % (used, window) if window else used


def tokens_total(tokens):
    """Every token that went in and came out. Thinking is part of what came
    out, so it is not counted again."""
    tokens = tokens or {}
    return sum(tokens.get(key) or 0 for key in ("input", "cache_read", "cache_write", "output"))


def tokens_text(tokens):
    """A turn's or a conversation's tokens: what went in - and how much of it
    was read from the cache - and what came out."""
    tokens = tokens or {}
    taken = sum(tokens.get(key) or 0 for key in ("input", "cache_read", "cache_write"))
    if not taken and not tokens.get("output"):
        return ""
    said = "%s in" % count_text(taken)
    if tokens.get("cache_read"):
        said += " (%s cached)" % count_text(tokens["cache_read"])
    return "%s · %s out" % (said, count_text(tokens.get("output") or 0))
