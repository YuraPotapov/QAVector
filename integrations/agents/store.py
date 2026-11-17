"""The agent connections, kept once for the whole of QAVector.

One file, beside the rest of the user's data::

    <user data>/agents.json     name, which CLI, where it is, model, effort

It holds no secret and needs no second file: an agent CLI signs in by itself
and keeps what it was given, so there is no token here to put anywhere. It is
written owner-only all the same - it names an account once a check has run.

A connection's ``id`` is made once, from its first name, and never changes:
whatever uses a connection names it by its id, and renaming "Claude" must not
break any of them.
"""

import json
import os

import runtime_paths
from integrations import store as shared
from integrations.agents import adapters

FILE_NAME = "agents.json"
VERSION = 1

#: What a connection's turns may do in QAVector itself, through QCR (ADR-003 D6):
#: each off until somebody turns it on - approvals above all.
QAVECTOR_ACCESS = ("services", "scenarios", "cycles", "approvals", "jira", "jira_write", "usage")

#: The QCR permissions each "QAVector access" switch gives a turn. The GUI
#: keeps the same table (gui/cms_gui/qcrhost.py AGENT_GRANTS) for the turns it
#: grants; this one is what a delegated turn may have at most (ADR-006 D2).
ACCESS_GRANTS = {
    "services": ("services.read", "services.start", "services.stop", "services.restart"),
    "scenarios": ("scenarios.read", "scenarios.run"),
    "cycles": ("cycles.read", "cycles.run"),
    "approvals": ("cycles.read", "cycles.approve"),
    "jira": ("integrations.jira.read", "integrations.jira.issue", "integrations.jira.comments",
             "integrations.jira.search", "integrations.jira.statuses"),
    # Each change waits for the person's Approve (ADR-007).
    "jira_write": ("integrations.jira.write", "integrations.jira.comment", "integrations.jira.transition",
                   "integrations.jira.assign", "integrations.jira.edit", "integrations.jira.comment_edit",
                   "integrations.jira.create", "integrations.jira.attach"),
}


def access_permissions(connection):
    """The permissions a connection's switches allow its turns."""
    access = (connection or {}).get("qavector_access") or {}
    return {permission for key, on in access.items() if on is True for permission in ACCESS_GRANTS.get(key, ())}


#: What a conversation may do with other agents (ADR-006 D3): delegate and
#: message them without asking, only once the person approves, or not at all.
OTHERS = ("auto", "approve", "deny")

#: What a connection made before ADR-006 says: nothing changes for it.
OTHERS_BEFORE = "deny"

#: What a new connection starts with: the person approves each one.
OTHERS_NEW = "approve"

#: The fields that say which program runs; changing one makes a past check moot.
PROGRAM = ("kind", "binary")

StoreError = shared.StoreError


def default_path():
    """Where the file goes when nobody has said otherwise: beside the user's data."""
    return os.path.join(runtime_paths.user_data_root(), FILE_NAME)


def resolve_path(configured=""):
    """The path in force: what the caller was given, or the default."""
    configured = (configured or "").strip()
    return os.path.expanduser(configured) if configured else default_path()


# -- one entry ----------------------------------------------------------------
def normalize(entry):
    """``entry`` with every field present and of the right type.

    Keys this version does not know are kept, so a file written by a newer
    build survives being saved by this one.
    """
    entry = dict(entry or {})
    known = {
        "id": str(entry.get("id") or "").strip(),
        "name": str(entry.get("name") or "").strip(),
        "kind": str(entry.get("kind") or adapters.KINDS[0]).strip(),
        # Blank finds the CLI on PATH each time it is used, so an upgrade that
        # moves it is followed without anybody editing the connection.
        "binary": str(entry.get("binary") or "").strip(),
        # Blank leaves either choice to the CLI's own configuration.
        "model": str(entry.get("model") or "").strip(),
        "effort": str(entry.get("effort") or "").strip(),
        "qavector_access": {key: (entry.get("qavector_access") or {}).get(key) is True
                            for key in QAVECTOR_ACCESS}
        if isinstance(entry.get("qavector_access"), dict) else {key: False for key in QAVECTOR_ACCESS},
        # What its conversations start with for other agents, and which other
        # connections they may reach (ADR-006 D3). Never itself.
        "others_default": entry.get("others_default") if entry.get("others_default") in OTHERS
        else OTHERS_BEFORE,
        "allowed_agents": _allowed(entry.get("allowed_agents"), str(entry.get("id") or "").strip()),
    }
    # The known fields first and in one order, whatever order they came in: the
    # file is plain JSON a person may open.
    return dict(known, **{key: value for key, value in entry.items() if key not in known})


def _allowed(value, own_id=""):
    """The other connections' ids, once each and in order; never ``own_id``."""
    if not isinstance(value, list):
        return []
    return [one for one in dict.fromkeys(str(item).strip() for item in value if isinstance(item, str))
            if one and one != own_id]


def problems(entry):
    """Everything wrong with one connection, as messages."""
    found = []
    if not entry["name"]:
        found.append("Name is required.")
    try:
        adapter = adapters.get(entry["kind"])
    except adapters.UnknownKind as exc:
        found.append(str(exc))
    else:
        if entry["effort"] and entry["effort"] not in adapter.EFFORTS:
            found.append("Effort must be one of %s, or blank for the CLI's own."
                         % ", ".join(adapter.EFFORTS))
    if entry["id"] and not shared.ID_PATTERN.match(entry["id"]):
        found.append("Id %r may hold only letters, digits, - and _." % entry["id"])
    return found


# -- the file -----------------------------------------------------------------
def load(path=None):
    """Every connection, normalized, in the order they were added.

    A missing file is no connections, the ordinary state of a new installation.
    """
    path = resolve_path(path)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise StoreError("cannot read %s: %s" % (path, exc))
    if not isinstance(document, dict) or not isinstance(document.get("connections", []), list):
        raise StoreError("%s has to contain an object with a connections list" % path)
    return [normalize(one) for one in document.get("connections") or []
            if isinstance(one, dict) and one.get("id")]


def save(connections, path=None):
    """Write every connection back, atomically and owner-only. Returns the path."""
    return shared.write_private(resolve_path(path), {
        "version": VERSION, "connections": [normalize(one) for one in connections]})


def get(connection_id, path=None):
    """One connection, or None."""
    for one in load(path):
        if one["id"] == connection_id:
            return one
    return None


# -- changing it --------------------------------------------------------------
def upsert(entry, path=None):
    """Add a connection, or change one. Returns ``(entry, problems)``.

    An entry without an ``id`` is new and gets one from its name. One with an
    id replaces the connection of that id and keeps it - renaming changes the
    name, never what its users call it.
    """
    raw = dict(entry or {})
    entry = normalize(entry)
    connections = load(path)
    taken = {one["id"] for one in connections}
    if not entry["id"]:
        # A new connection: the person approves each delegation and message,
        # to every connection already here - unless the form said otherwise.
        if raw.get("others_default") not in OTHERS:
            entry["others_default"] = OTHERS_NEW
        if not isinstance(raw.get("allowed_agents"), list):
            entry["allowed_agents"] = [one["id"] for one in connections]
    if entry["id"] and entry["id"] not in taken:
        return entry, ["No connection %r to change." % entry["id"]]
    found = problems(entry)
    if entry["id"]:
        previous = next(one for one in connections if one["id"] == entry["id"])
        if previous["kind"] != entry["kind"]:
            # Its conversations were held with that agent's program; another one
            # cannot carry them on - and it would run with the wrong command line.
            found.append("A connection's agent cannot change (it is %s)." % previous["kind"])
    others = [one for one in connections if one["id"] != entry["id"]]
    if any(one["name"].lower() == entry["name"].lower() for one in others):
        found.append("A connection called %r already exists." % entry["name"])
    if found:
        return entry, found
    if not entry["id"]:
        entry["id"] = shared.slug(entry["name"], taken)
        entry["allowed_agents"] = _allowed(entry["allowed_agents"], entry["id"])
        entry.pop("last_check", None)
        connections.append(entry)
    else:
        previous = next(one for one in connections if one["id"] == entry["id"])
        if any(previous.get(key) != entry.get(key) for key in PROGRAM):
            # Another program now: what the old one answered says nothing of it.
            entry.pop("last_check", None)
        elif "last_check" not in entry and "last_check" in previous:
            entry["last_check"] = previous["last_check"]
        connections = [entry if one["id"] == entry["id"] else one for one in connections]
    save(connections, path)
    return entry, []


def export(ids=(), path=None):
    """The connections ``ids`` as an export file carries them: what they are,
    not how the last check here went."""
    wanted = set(ids or ())
    return [{key: value for key, value in one.items() if key != "last_check"}
            for one in load(path) if one["id"] in wanted]


def import_entries(entries, mode="merge", path=None):
    """Put the connections of an export file in. Returns what was done, said.

    Each keeps its id, so whatever names it - a cycle step - finds it here too.
    One whose id is here already is left as it is (``merge``) or added beside it
    as "<name> (copy)" (``copy``). A program path that is not a program on this
    computer is dropped, so the connection finds its program on PATH instead.
    """
    said = []
    connections = load(path)
    for raw in entries or []:
        entry = normalize(raw)
        taken = {one["id"] for one in connections}
        names = {one["name"].casefold() for one in connections}
        if entry["id"] in taken:
            if mode != "copy":
                said.append("Agent connection %s is here already; left as it is."
                            % entry["name"])
                continue
            entry["id"] = ""
        if not entry["id"] or not shared.ID_PATTERN.match(entry["id"]):
            name = entry["name"]
            while name.casefold() in names:
                name = "%s (copy)" % name
            entry["name"] = name
            entry["id"] = shared.slug(name, taken)
        elif entry["name"].casefold() in names:
            entry["name"] = "%s (%s)" % (entry["name"], entry["id"])
        note = ""
        binary = entry.get("binary") or ""
        if binary and not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
            entry["binary"] = ""
            note = "; its program %s is not on this computer, so it is found automatically" % binary
        found = problems(entry)
        if found:
            said.append("Agent connection %s was not imported: %s"
                        % (entry["name"] or "?", " ".join(found)))
            continue
        entry.pop("last_check", None)
        connections.append(entry)
        said.append("Agent connection %s added%s." % (entry["name"], note))
    save(connections, path)
    return said


def remove(connection_id, path=None):
    """Forget one connection. Returns whether there was one."""
    connections = load(path)
    kept = [one for one in connections if one["id"] != connection_id]
    if len(kept) == len(connections):
        return False
    save(kept, path)
    return True
