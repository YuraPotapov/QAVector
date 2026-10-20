"""Readable approval previews from the core's captured, redacted inputs.

This is presentation only. The core still binds consent to the full inputs;
the GUI neither resolves settings again nor predicts unrecorded effects.
"""

import html
import json
from urllib.parse import quote


def binding(receipt):
    return receipt.get("id"), receipt.get("fingerprint")


def _text(value):
    return html.escape(str(value))


def _value(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        return "; ".join("%s: %s" % (key, _value(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(_value(item) for item in value)
    return str(value)


def _jira_rows(inputs):
    rows = [("Jira access", "Read only")]

    def add(label, key):
        if inputs.get(key) not in (None, "", [], {}):
            rows.append((label, inputs[key]))

    add("Site", "site")
    if inputs.get("issue"):
        # The plugin's single-issue path ignores queue filters and ordering.
        add("Issue", "issue")
    else:
        add("Projects", "project")
        add("Statuses", "status")
        add("Search conditions", "jql")
        role = inputs.get("role")
        if role == "everyone":
            rows.append(("Whose issues", "Everyone"))
        else:
            add("Whose issues", "user")
            if role:
                rows.append(("Their part", {"any": "Assignee, reporter or creator"}.get(role, role.capitalize())))
        add("Sort by", "order")
        add("Issue limit", "limit")
    included = []
    if inputs.get("comments") is True:
        included.append("Comments")
    attachments = inputs.get("attachments")
    if attachments in ("images", "all"):
        included.append("Images" if attachments == "images" else "All attachments")
    subtasks = inputs.get("subtasks")
    if subtasks in ("full", "list"):
        included.append("Sub-task details" if subtasks == "full" else "Sub-task list")
    if included:
        rows.append(("Include", ", ".join(included)))
    return rows


def _rows(receipt):
    inputs = receipt.get("resolved_inputs") or {}
    if not isinstance(inputs, dict):
        return []
    if receipt.get("plugin") == "jira.issues":
        return _jira_rows(inputs)
    labels = {"dir": "Working directory", "directory": "Working directory", "env": "Environment",
              "expect_exit": "Expected exit code", "jql": "Search conditions"}
    hidden = ("password", "token", "secret", "authorization", "api_key", "apikey", "credential", "private_key", "_pass")
    return [(labels.get(key, key.replace("_", " ").capitalize()), value)
            for key, value in inputs.items()
            if not any(word in key.lower() for word in hidden)
            and key not in ("auth", "verify_tls", "flavour") and value not in (None, "", [], {})]


def preview(receipt, expanded=False):
    rows = _rows(receipt)
    purpose = "Read Jira issues." if receipt.get("plugin") == "jira.issues" else receipt.get("purpose")
    parts = ['<p>%s</p>' % _text(purpose)] if purpose else []
    if rows:
        limit = 12 if receipt.get("plugin") == "jira.issues" else 8
        lines = []
        for label, value in rows[:limit]:
            text = _value(value)
            text = text[:700] + ("…" if len(text) > 700 else "")
            lines.append('<b>%s:</b> %s' % (_text(label), _text(text)))
        parts.append('<p>' + '<br>'.join(lines) + '</p>')
        if len(rows) > limit:
            parts.append('<p>More inputs in Technical details.</p>')
    elif not purpose:
        parts.append('<p>Input summary unavailable. Review Technical details before approving.</p>')
    effects = receipt.get("declared_effects") or []
    if effects:
        parts.append('<p><b>Declared changes:</b> %s</p>' % _text(", ".join(str(one) for one in effects)))
    runtime = receipt.get("runtime") or {}
    if runtime:
        policy = (runtime.get("effective_policy") or {}).get("policy") or {}
        network = (policy.get("network") or {}).get("mode", "none")
        parts.append('<p><b>Strict isolation:</b> %s, network %s<br>Revision %s</p>' % (
            _text(runtime.get("deployment", "")), _text(network), _text(runtime.get("revision", ""))))
    parts.append('<p><a href="details:%s">%s Technical details</a></p>' % (
        quote(str(receipt.get("id") or ""), safe=""), "Hide" if expanded else "Show"))
    if expanded:
        inputs = json.dumps(receipt.get("resolved_inputs") or {}, ensure_ascii=False, indent=2, default=str)
        if len(inputs) > 6000:
            inputs = inputs[:6000] + "\n… Preview truncated"
        details = ["Mode %s · execution %s · attempt %s" % (
            str(receipt.get("mode") or "unknown").capitalize(), receipt.get("execution_generation", 1), receipt.get("attempt", 1)),
            "Timeout: %ss" % receipt["timeout"] if receipt.get("timeout") is not None else "No timeout configured",
            "Effects (declared): " + ", ".join(str(one) for one in effects) if effects else "Effects not recorded"]
        if receipt.get("permissions"):
            details.append("Declared capabilities: " + ", ".join(str(one) for one in receipt["permissions"]))
        parts.append('<p>%s</p><p><b>Resolved inputs</b></p><pre>%s</pre>' % (
            '<br>'.join(_text(one) for one in details), _text(inputs)))
        if runtime:
            parts.append('<p><b>Resolved runtime policy</b></p><pre>%s</pre>' % _text(
                json.dumps(runtime.get("effective_policy") or {}, ensure_ascii=False, indent=2)))
    return "".join(parts)
