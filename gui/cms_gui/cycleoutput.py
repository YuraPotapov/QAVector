"""Human-readable implementation results, including runs saved by older cores.

Only presentation changes here. Provider responses, outputs and artifacts stay
available in Technical details and on disk. All provider text is escaped before
it enters Qt's native rich-text view.
"""

import html
import json
import math
import re

from . import theme


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _provider_result(message):
    """Older failures embed a possibly truncated CLI result in their message."""
    match = re.search(r'\{\s*"type"\s*:\s*"result"', message)
    if not match:
        return {}
    text = message[match.start():match.start() + 64000]
    try:
        result, _end = json.JSONDecoder().raw_decode(text)
        return result
    except ValueError:
        # A log tail may stop inside usage. Extract only known scalar fields;
        # missing values remain unknown, rather than becoming zero.
        result = {}
        subtype = re.search(r'"subtype"\s*:\s*"([^"\\]+)"', text)
        if subtype:
            result["subtype"] = subtype.group(1)
        for key in ("num_turns", "duration_ms", "total_cost_usd"):
            found = re.search(r'"%s"\s*:\s*(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?=\s*[,}])'
                              % key, text)
            if found:
                result[key] = float(found.group(1))
        return result


def _text(value):
    return html.escape(str(value)).replace("\n", "<br>")


def _duration(milliseconds):
    seconds = int(milliseconds / 1000)
    if seconds < 60:
        return "%d s" % seconds
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return "%d min %d s" % (minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    return "%d h %d min" % (hours, minutes)


def implementation_html(step):
    """A concise report for an implement node, or empty for other plugins."""
    if step.get("plugin") != "agent.implement":
        return ""
    outputs = step.get("outputs") or {}
    status = step.get("status", "pending")
    failure = str(outputs.get("failed_check") or step.get("message") or "")
    provider = _provider_result(failure)
    limited = (provider.get("subtype") == "error_max_turns"
               or "error_max_turns" in failure)
    next_action = ""
    if status == "success" and outputs.get("verified") is True:
        title, detail, color = ("Change completed and verified",
                                "The configured checks and review passed.", theme.OK)
    elif status in ("pending", "running", "waiting"):
        title = {"pending": "Waiting to start", "running": "Implementation in progress",
                 "waiting": "Implementation is waiting"}[status]
        detail, color = "Follow the agent's progress in Stages.", theme.ACCENT
    elif status == "skipped":
        title, detail, color = ("Implementation was skipped",
                                "This step did not run. Check the preceding steps.", theme.NEUTRAL[700])
    elif status == "cancelled":
        title, detail, color = ("Implementation was stopped",
                                "Stopping a run does not undo changes already made.", theme.WARN)
    elif status == "timeout":
        title, detail, color = ("Implementation ran out of time",
                                "The step stopped before finishing within its time limit.", theme.BAD)
        next_action = "Review the step's timeout and any changes before continuing."
    elif limited:
        title, detail, color = ("Agent reached its step limit",
                                "The agent stopped before completing the change.", theme.BAD)
        next_action = ("Increase Maximum tool iterations in this step's settings, then "
                       "retry the step. Another agent attempt may incur a charge.")
    else:
        title, detail, color = ("Implementation did not finish",
                                "The change has not been verified.", theme.BAD)
        # Useful plain-language reasons stay visible; protocol dumps do not.
        if failure and not re.search(r'\{|Traceback|exited \d|agent\.implement:', failure):
            detail = failure[:600]
        next_action = "Review the reason and any file changes before retrying this step."

    rows = []
    files = outputs.get("files_changed")
    changed = len(files) if isinstance(files, list) else _number(outputs.get("changed"))
    if changed is not None:
        rows.append(("File changes", "%d file(s) reported" % changed if changed
                     else "None reported"))
    if "verified" in outputs:
        rows.append(("Verification", "Passed" if outputs["verified"] is True else "Not verified"))
    attempts = _number(outputs.get("attempts", step.get("attempt")))
    if attempts:
        rows.append(("Attempts", "%d" % attempts))
    turns = _number(provider.get("num_turns"))
    if turns is not None:
        rows.append(("Agent steps", "%d" % turns))
    duration = _number(step.get("duration_ms")) or _number(provider.get("duration_ms"))
    if duration is not None:
        rows.append(("Duration", _duration(duration)))
    cost = _number(outputs.get("cost_usd"))
    cost_label = "Reported cost"
    if cost is None:
        cost = _number(provider.get("total_cost_usd"))
        cost_label = "Last agent call cost"
    if cost is not None:
        rows.append((cost_label, "$%.2f" % cost))

    parts = ['<h3 style="color:%s; margin:0">%s</h3>' % (color, _text(title)),
             "<p>%s</p>" % _text(detail)]
    if rows:
        parts.append('<table cellspacing="0" cellpadding="4">' + "".join(
            '<tr><td style="color:%s">%s</td><td><b>%s</b></td></tr>'
            % (theme.NEUTRAL[700], _text(label), _text(value)) for label, value in rows) + "</table>")
    summary = outputs.get("summary")
    if isinstance(summary, str) and summary.strip():
        parts.append("<p><b>Agent summary</b><br>%s</p>" % _text(summary.strip()))
    if files:
        parts.append("<p><b>Changed files</b></p><ul>%s</ul>" % "".join(
            "<li>%s</li>" % _text(path) for path in files))
    findings = outputs.get("findings") or []
    notes = [one.get("description") for one in findings if isinstance(one, dict)
             and one.get("description")]
    if notes:
        parts.append("<p><b>Review findings</b></p><ul>%s</ul>" % "".join(
            "<li>%s</li>" % _text(note) for note in notes))
    if next_action:
        parts.append("<p><b>Next step</b><br>%s</p>" % _text(next_action))
    return "".join(parts)
