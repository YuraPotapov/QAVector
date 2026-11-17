"""Everything known about one issue, in one read (QCR ADR-014 D3).

``integrations.jira.<connection>.context {key}`` gives an agent what it needs to
work on an issue, from both sides:

* **from Jira** - the issue's fields and description as text, its newest
  comments as text; the HTML and editor documents the page needs are left
  out, so the answer stays small enough to read at once;
* **from QAVector** - the conversations about the issue (started for it, or
  linked to it because an agent read its details), the tasks they handed to
  other agents, what all of that cost, and the person's private notes on it.
  The notes are private from Jira, not from the agents working here.

Its permission is the issue read's (``integrations.jira.issue``): an agent
that may read an issue may read its context. Reading it is reading the
issue's details, so the reading conversation is linked to the issue (the
host does that, as for ``issue`` and ``comments``).
"""

from integrations import notes as notes_store
from integrations.agents import sessions, usage

from ..contracts import CapabilityDescriptor, ResourceDescriptor, ResourceRef
from ..errors import QCRError
from ..registry import Registration

#: What is kept of long texts, and how many of each list.
DESCRIPTION_CHARS, COMMENT_CHARS, NOTE_CHARS = 6000, 1500, 2000
COMMENTS, CONVERSATIONS, TASKS = 5, 20, 20
#: The issue's fields an agent reads; the page's HTML and editor documents are not among them.
DROPPED = ("description_html", "description_doc", "description_lost", "description_plain")

INPUT = {"type": "object", "additionalProperties": False, "required": ["key"],
         "properties": {"key": {"type": "string", "minLength": 1, "maxLength": 64,
                                "pattern": "^[A-Za-z][A-Za-z0-9_]*-[0-9]+$"}}}
OUTPUT = {"type": "object", "required": ["issue", "comments", "qavector"]}


def _cut(text, limit):
    text = str(text or "")
    return (text[:limit], True) if len(text) > limit else (text, False)


class IssueContext:
    """Registered beside a project's Jira reads, one capability a connection."""

    def __init__(self, registry, project_id, reader, integrations_path, sessions_root):
        self.registry, self.project_id, self.reader = registry, project_id, reader
        self.integrations_path = integrations_path
        self._root = sessions_root            # () -> the agent sessions' root, or None
        self._owner = object()

    def refresh(self, connections):
        registrations = []
        for row in connections:
            if row.get("provider") != "jira":
                continue
            connection_id = row["id"]
            ref = ResourceRef(self.project_id, "integrations.jira.%s~context" % connection_id, "integration_context")
            capability = CapabilityDescriptor(
                "integrations.jira.%s.context" % connection_id, ref, 1,
                "Everything known about one issue: from Jira its fields, description and newest comments; "
                "from QAVector the conversations about it, their tasks, cost and the private notes. Read first.",
                INPUT, OUTPUT, "integrations.jira.issue", read_only=True)
            registrations.append(Registration(
                ResourceDescriptor(ref, "integrations", "%s - issue context" % row["name"], "integrations.jira.read"),
                (capability,), lambda ref, names: {}, read_capability=self._read(connection_id)))
        self.registry.replace(self._owner, registrations)

    def _read(self, connection_id):
        def read(capability_id, version, request):
            key = request["key"].strip().upper()
            try:
                found = self.reader.read(connection_id, "jira", "issue", {"key": key, "comments": COMMENTS})
            except Exception as exc:
                code = getattr(exc, "code", "source_unavailable")
                raise QCRError(code if isinstance(code, str) else "source_unavailable",
                               "The issue could not be read from Jira.") from None
            return dict(self._jira(found.get("issue") or {}), qavector=self._qavector(connection_id, key))
        return read

    @staticmethod
    def _jira(issue):
        shown = {name: value for name, value in issue.items() if name not in DROPPED + ("comments",)}
        shown["description_text"], cut = _cut(issue.get("description_text"), DESCRIPTION_CHARS)
        if cut:
            shown["description_cut"] = True
        shown["attachments"] = [{"filename": one.get("filename"), "size": one.get("size")}
                                for one in issue.get("attachments") or []]
        page = issue.get("comments") or {}
        comments = []
        for one in page.get("items") or []:
            text, cut = _cut(one.get("text"), COMMENT_CHARS)
            comments.append(dict({"author": one.get("author"), "created": one.get("created"), "text": text},
                                 **({"cut": True} if cut else {})))
        more = max(int(page.get("total") or 0) - len(comments), 0)
        return {"issue": shown, "comments": {"newest": comments, "total": int(page.get("total") or len(comments)),
                                             "more": more}}

    def _qavector(self, connection_id, key):
        ref = "jira:%s:%s" % (connection_id, key)
        root = self._root()
        said = {"ref": ref, "conversations": [], "tasks": [], "usage": {}, "notes": []}
        if root:
            try:
                every = sessions.listing(root)["sessions"]
            except sessions.SessionError:
                every = []
            about = [one for one in every if ref in sessions.refs_of(one)]
            ids = {one["id"] for one in about}
            said["conversations"] = [{
                "id": one["id"], "title": one.get("title") or "", "agent": one.get("connection_name") or "",
                "updated": one.get("updated"), "turns": len(one.get("turns") or []), "state": one.get("state") or "",
                "how": "started for it" if (one.get("source") or {}).get("ref") == ref else "linked"}
                for one in about[:CONVERSATIONS]]
            said["tasks"] = [{
                "id": one["id"], "title": one.get("title") or "", "agent": one.get("connection_name") or "",
                "by": (one.get("source") or {}).get("by") or "", "state": one.get("state") or "",
                "updated": one.get("updated")}
                for one in every if (one.get("source") or {}).get("parent") in ids][:TASKS]
            figures = usage.for_ref(root, ref, found=every)
            said["usage"] = {part: {name: figures[part].get(name) for name in (
                "conversations", "turns", "tokens_total", "estimate_usd", "unknown_cost_turns")}
                for part in ("own", "inclusive")}
        try:
            kept = notes_store.notes_for(self.integrations_path, connection_id, key)
        except Exception:
            kept = []
        for one in kept:
            body = one.get("body")
            shown = notes_store.shown(one, "cloud" if isinstance(body, dict) else "server")
            text, cut = _cut(shown.get("text"), NOTE_CHARS)
            said["notes"].append(dict({"created": shown.get("created"), "text": text,
                                       "files": [str(name) for name in one.get("files") or []]},
                                      **({"cut": True} if cut else {})))
        return said
