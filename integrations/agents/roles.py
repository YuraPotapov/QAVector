"""Roles an agent may hand work to (QCR ADR-010): dev, tester, reviewer...

A role is the person's own: what it does, the agent connection (program) it
runs as, its model and effort, the most it may do (mode), instructions for
each of its tasks, and whether it ends with a verdict. Roles are kept for every
project (``agent-roles.json`` beside ``agents.json``, owner-readable), and each
is a delegation target of its own for every conversation that works with other
agents.

Nothing here starts an agent or asks a model anything.
"""

import hashlib
import json
import os
import re

from integrations import store as shared

from . import adapters, store

FILE_NAME = "agent-roles.json"
VERSION = 1
MODES = ("read", "edit", "full")
ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
DOES_CHARS, INSTRUCTIONS_CHARS = 500, 4000
#: What a role with a verdict is told to end its answer with (ADR-010 D4).
VERDICT_ASK = ('End your answer with one line of JSON and nothing after it: '
               '{"verdict": "approve" or "rework", "issues": ["what must change", ...]}. '
               'Approve only what you would let through as it is.')


class RoleError(ValueError):
    pass


def path_for(agents_path=None):
    return os.path.join(os.path.dirname(os.path.abspath(store.resolve_path(agents_path))), FILE_NAME)


def normalize(role):
    role = dict(role or {})
    known = {
        "id": str(role.get("id") or "").strip().lower(),
        "name": str(role.get("name") or "").strip(),
        "does": str(role.get("does") or "").strip()[:DOES_CHARS],
        "connection": str(role.get("connection") or "").strip(),
        "model": str(role.get("model") or "").strip(),
        "effort": str(role.get("effort") or "").strip(),
        "mode": role.get("mode") if role.get("mode") in MODES else "read",
        "instructions": str(role.get("instructions") or "").strip()[:INSTRUCTIONS_CHARS],
        "verdict": role.get("verdict") is True,
    }
    return dict(known, **{key: value for key, value in role.items() if key not in known})


def problems(role, connections):
    found = []
    if not ID.match(role["id"]):
        found.append("An id is lowercase letters, digits, - and _, starting with a letter (dev, tester).")
    if not role["name"]:
        found.append("Name is required.")
    if not role["does"]:
        found.append("Say what the role does: the agents choose by it.")
    connection = next((one for one in connections if one["id"] == role["connection"]), None)
    if connection is None:
        found.append("Choose the agent program the role runs as.")
    elif role["effort"] and connection["kind"] in adapters.KINDS \
            and role["effort"] not in adapters.get(connection["kind"]).EFFORTS:
        found.append("Effort must be one of %s." % ", ".join(adapters.get(connection["kind"]).EFFORTS))
    return found


def load(agents_path=None):
    path = path_for(agents_path)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RoleError("cannot read %s: %s" % (path, exc))
    return [normalize(one) for one in (document.get("roles") if isinstance(document, dict) else None) or []
            if isinstance(one, dict) and one.get("id")]


def _save(roles, agents_path=None):
    return shared.write_private(path_for(agents_path), {"version": VERSION,
                                                        "roles": [normalize(one) for one in roles]})


def get(role_id, agents_path=None):
    return next((one for one in load(agents_path) if one["id"] == role_id), None)


def upsert(role, agents_path=None, new=False):
    """Add a role (``new``: its id must be free) or change one. ``(role, problems)``."""
    role = normalize(role)
    found = problems(role, store.load(agents_path))
    roles = load(agents_path)
    held = next((index for index, one in enumerate(roles) if one["id"] == role["id"]), None)
    if new and held is not None:
        found.append("There is a role %r already." % role["id"])
    if found:
        return role, found
    if held is None:
        roles.append(role)
    else:
        roles[held] = role
    _save(roles, agents_path)
    return role, []


def delete(role_id, agents_path=None):
    roles = load(agents_path)
    kept = [one for one in roles if one["id"] != role_id]
    if len(kept) == len(roles):
        return False
    _save(kept, agents_path)
    return True


def usable(agents_path=None):
    """``[(role, connection)]`` whose program is still here: the ones offered to agents."""
    connections = {one["id"]: one for one in store.load(agents_path)}
    return [(role, connections[role["connection"]]) for role in load(agents_path)
            if role["connection"] in connections and connections[role["connection"]]["kind"] in adapters.KINDS]


def templates(connections):
    """The usual three, for the person to change (ADR-010 D5). Models are left to
    the connection: nothing is guessed about which models exist."""
    def first(kind):
        return next((one["id"] for one in connections if one["kind"] == kind), None)
    any_one = connections[0]["id"] if connections else ""
    return [normalize(one) for one in (
        {"id": "dev", "name": "Developer", "connection": first("codex") or any_one, "mode": "edit",
         "does": "Changes code to do the task, and runs what it changed.",
         "instructions": "Make the smallest change that does the task. Run the tests that cover what you "
                         "changed and say what they showed. Leave unrelated code alone."},
        {"id": "tester", "name": "Tester", "connection": first("claude") or any_one, "mode": "read",
         "does": "Runs the tests and the relevant scenarios; reports failures and likely problems with "
                 "evidence; changes nothing.",
         "instructions": "Run the tests and scenarios that cover the change. Report each failure with how to "
                         "reproduce it, and problems you see coming, with evidence. Do not change files."},
        {"id": "reviewer", "name": "Reviewer", "connection": first("claude") or any_one, "mode": "read",
         "effort": "high", "verdict": True,
         "does": "Reviews the change and its evidence; approves it or sends it back with concrete issues.",
         "instructions": "Review the diff, the tests and their results against the task. Send it back for "
                         "anything you would not let through, each issue concrete and actionable."})]


def describe(role, connection):
    """One role as an agent is told it: one line."""
    with_ = " ".join(part for part in (role["model"] or "its default model", role["effort"]) if part)
    return "%s (%s) - %s on %s, %s, %s%s: %s" % (
        role["id"], role["name"], connection["name"], with_, role["mode"],
        "may change files" if role["mode"] != "read" else "read-only",
        ", gives a verdict" if role["verdict"] else "", role["does"])


def preface(agents_path, connection):
    """What a conversation of ``connection`` is told about the agents it may hand
    work to (ADR-010 D3): ``(text, fingerprint)``; ``("", "")`` when there is
    nothing to tell."""
    found = usable(agents_path)
    targets = [one for one in store.load(agents_path)
               if one["id"] in (connection.get("allowed_agents") or [])]
    if not found and not targets:
        return "", ""
    lines = ["[QAVector: who you may hand work to in this conversation]"]
    if found:
        lines.append("Roles - agents.roles.<id>.delegate; when the person names a role, use it; otherwise "
                     "pick by what it does:")
        lines += ["- " + describe(role, program) for role, program in found]
    if targets:
        lines.append("Agent connections - agents.<id>.delegate: %s." % ", ".join(
            "%s (%s)" % (one["id"], one["name"]) for one in targets))
    lines.append("Your own program, with another model: agents.%s.delegate." % connection.get("id"))
    text = "\n".join(lines)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
