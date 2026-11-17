"""Opt-in contracts for Jira's explicit reads and confirmed writes.

Legacy GUI/Cycle operations retain their contracts and routing. Entries here
describe operations.call results, not a second HTTP client. Other operations
are deliberately not advertised until their contracts/policies are implemented.

Writes (``comment``, ``transition``, ``assign``, ``edit``, ``comment_edit``,
``create``, ``attach``) are each confirmed by a person before they are made
(QCR ADR-007): :func:`prepare` reads what the person decides on and says what
would change, :func:`write` makes the change with the Integrations page's own
functions. Deleting a comment is for people only, so it is not declared.
"""

import os

from integrations.contracts import OperationContract, Provider, ReadError, WriteError
from . import operations, query, writes
from .client import JiraError, api


def obj(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required,
            "additionalProperties": False}


def array(items, limit=1000):
    return {"type": "array", "items": items, "maxItems": limit}


STRING = {"type": "string", "maxLength": 1_000_000}
SHORT = {"type": "string", "maxLength": 4096}
KEY = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]*-[0-9]+$", "maxLength": 128}
INTEGER = {"type": "integer", "minimum": 0}
CURSOR = {"anyOf": [SHORT, INTEGER, {"type": "null"}]}
# The provider's editor document is an ADF object; retained as data, never code.
DOC = {"type": ["object", "null"]}
COMMENT = obj(dict(id=SHORT, author=SHORT, author_id=SHORT, created=SHORT, updated=SHORT,
                   edited={"type": "boolean"}, html=STRING, text=STRING, doc=DOC, lost=array(STRING)))
COMMENTS = obj(dict(items=array(COMMENT, 100), start=INTEGER, total=INTEGER))
ROW_FIELDS = dict(key=SHORT, summary=STRING, type=SHORT, priority=SHORT, status=SHORT,
                  category=SHORT, assignee=SHORT, updated=SHORT, project=SHORT, project_name=SHORT, url=SHORT)
ROW = obj(ROW_FIELDS)
ATTACHMENT = obj(dict(id=SHORT, url=SHORT, filename=SHORT, mime=SHORT, size=INTEGER, created=SHORT, author=SHORT))
TRANSITION = obj(dict(id=SHORT, name=SHORT, to=SHORT, category=SHORT))
ISSUE = obj(dict(ROW_FIELDS, project=obj(dict(key=SHORT, name=SHORT)), reporter=SHORT, created=SHORT,
                 labels=array(SHORT), parent=SHORT, description_html=STRING, description_text=STRING,
                 description_plain={"type": "boolean"}, description_doc=DOC, description_lost=array(STRING),
                 attachments=array(ATTACHMENT), transitions=array(TRANSITION), comments=COMMENTS))
SEARCH_INPUT = obj(dict(text=SHORT, jql=SHORT, project=SHORT, status=SHORT, mine={"type": "boolean"},
                        order=SHORT, size={"type": "integer", "minimum": 1, "maximum": operations.MOST_ROWS},
                        cursor=CURSOR), required=[])

#: The longest comment an agent may propose.
COMMENT_CHARS = 32000
TEXT = {"type": "string", "minLength": 1, "maxLength": COMMENT_CHARS}
TEXT_OR_EMPTY = {"type": "string", "maxLength": COMMENT_CHARS}
SUMMARY = {"type": "string", "minLength": 1, "maxLength": 255}
LABELS = array({"type": "string", "minLength": 1, "maxLength": 255}, 50)
PROJECT = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$"}
EDIT_FIELDS = {"type": "object", "additionalProperties": False, "minProperties": 1, "properties": {
    "summary": SUMMARY, "description": TEXT_OR_EMPTY, "labels": LABELS, "priority": SHORT}}
#: Other create-screen fields: a value as text, or a choice by its id.
CREATE_FIELDS = {"type": "object", "maxProperties": 50, "additionalProperties": {"anyOf": [
    SHORT, obj(dict(id=SHORT))]}}
WRITE_OUTPUT = obj(dict(changed={"type": "boolean"}, summary=STRING, issue=ISSUE))

CONTRACTS = {
    "issue": OperationContract("issue", "Read an issue and its newest comments within the connection scope.",
        obj(dict(key=KEY, comments={"type": "integer", "minimum": 1, "maximum": 100}), ["key"]),
        obj(dict(issue=ISSUE))),
    "comments": OperationContract("comments", "Read one page of issue comments within the connection scope.",
        obj(dict(key=KEY, start={"type": "integer", "minimum": 0, "maximum": 100000},
                 size={"type": "integer", "minimum": 1, "maximum": 100}), ["key"]), obj(dict(comments=COMMENTS))),
    "search": OperationContract("search", "Read a bounded search page, intersected with the connection query.",
        SEARCH_INPUT, obj(dict(rows=array(ROW, operations.MOST_ROWS), cursor=CURSOR, jql=STRING, scope=STRING))),
    "statuses": OperationContract("statuses", "Read site-wide status names and categories, without issue content.",
        obj({}), obj(dict(statuses=array(obj(dict(name=SHORT, category=SHORT)), 10000)))),
    # -- writes: each one confirmed by a person (QCR ADR-007) ---------------------
    "comment": OperationContract("comment", "Propose a plain-text comment on an issue. A person sees it, may edit "
                                 "it, and approves or denies it; the result is the issue after the change.",
        obj(dict(key=KEY, body=TEXT)), WRITE_OUTPUT, access="write", risk="high", idempotent=False),
    "transition": OperationContract("transition", "Propose moving an issue: to is a transition id from the "
                                    "issue's transitions, or its target status name. A person approves it; "
                                    "already there is no change.",
        obj(dict(key=KEY, to=SHORT, fields={"type": "object", "maxProperties": 50,
                                            "additionalProperties": SHORT}), ["key", "to"]),
        WRITE_OUTPUT, access="write", risk="high", idempotent=True),
    "assign": OperationContract("assign", "Propose assigning an issue: account is an account id, a name or email "
                                "to look up among people it may be assigned to, me, or empty for nobody.",
        obj(dict(key=KEY, account=SHORT)), WRITE_OUTPUT, access="write", risk="high", idempotent=True),
    "edit": OperationContract("edit", "Propose changing an issue's summary, description (plain text), labels or "
                              "priority (name or id) - only those given. A person may edit the text first.",
        obj(dict(key=KEY, fields=EDIT_FIELDS, updated=SHORT), ["key", "fields"]),
        WRITE_OUTPUT, access="write", risk="high", idempotent=True),
    "comment_edit": OperationContract("comment_edit", "Propose new text for one comment (id from the issue's "
                                      "comments). A person may edit it first; a comment changed meanwhile is "
                                      "not overwritten.",
        obj(dict(key=KEY, id={"type": "string", "pattern": "^[0-9]{1,30}$"}, body=TEXT, updated=SHORT),
            ["key", "id", "body"]), WRITE_OUTPUT, access="write", risk="high", idempotent=True),
    "create": OperationContract("create", "Propose a new issue: project key, type (name or id), summary, and "
                                "optionally description, labels, priority and other create-screen fields by id.",
        obj(dict(project=PROJECT, type=SHORT, summary=SUMMARY, description=TEXT_OR_EMPTY, labels=LABELS,
                 priority=SHORT, fields=CREATE_FIELDS), ["project", "type", "summary"]),
        WRITE_OUTPUT, access="write", risk="high", idempotent=False),
    "attach": OperationContract("attach", "Propose attaching one file from your working folder to an issue "
                                "(up to 100 MB).",
        obj(dict(key=KEY, path={"type": "string", "minLength": 1, "maxLength": 4096})),
        WRITE_OUTPUT, access="write", risk="high", idempotent=False, local_paths=("path",)),
}



def _in_scope(get, connection, key):
    """Refuse a key outside the connection's predefined query."""
    if connection.get("scope"):
        scoped_query = operations.jql({"text": key}, connection)
        rows, _ = query.page(get, connection["flavour"], scoped_query, 1, ("key",))
        if not any(isinstance(row, dict) and row.get("key", "").upper() == key for row in rows):
            raise ReadError("out_of_scope")


def read(name, connection, token, request, get=None):
    """Use the existing executor, adding scope membership for direct key reads.

    Search already intersects the scope. Issue/comments do not do so in the
    legacy GUI path; this explicit-read facade checks membership before fetching
    their content. A status list is site metadata, not a list of scoped issues.
    """
    if name not in CONTRACTS or CONTRACTS[name].access != "read":
        raise ReadError("unsupported")
    get = get or operations.session(connection, token)
    if name in ("issue", "comments"):
        _in_scope(get, connection, request["key"].upper())
    return operations.call(name, connection, token, request, get=get)


def _is(a, b):
    return str(a or "").strip().casefold() == str(b or "").strip().casefold()


def _shown(text, most=300):
    text = str(text or "").strip()
    return text if len(text) <= most else text[:most - 3].rstrip() + "..."


def _card(name, key, issue, title, changes=(), notes=(), edit=None):
    return {"operation": name, "key": key, "summary": (issue or {}).get("summary") or "", "title": title,
            "changes": [dict(one, **{"from": str(one.get("from") or ""), "to": str(one.get("to") or "")})
                        for one in changes],
            "notes": [str(one) for one in notes if one], "edit": dict(edit or {})}


def _noop(key, issue, said):
    return {"request": None, "card": {}, "noop": {"changed": False, "summary": "%s %s" % (key, said),
                                                  "issue": issue}}


def _issue(get, connection, key):
    return operations.issue(get, connection, {"key": key, "comments": 1})["issue"]


def _refused(exc):
    """A page refusal as a write error, before or after anything was sent."""
    if exc.kind == "fields":
        needs = [{"key": one["key"], "name": one["name"]} for one in exc.details.get("needs") or []]
        return WriteError("fields_required", str(exc), {"needs": needs})
    return WriteError("conflict" if exc.kind == "conflict" else "refused", str(exc))


def prepare(name, connection, token, request, get=None):
    """What a person decides a write on, read now and within the connection's scope."""
    if name not in CONTRACTS or CONTRACTS[name].access != "write":
        raise WriteError("unsupported")
    get = get or operations.session(connection, token)
    try:
        if name == "create":
            return _prepare_create(get, connection, request)
        key = request["key"].upper()
        _in_scope(get, connection, key)
        return PREPARE[name](get, connection, key, _issue(get, connection, key), request)
    except writes.Refused as exc:
        raise _refused(exc) from None


def _prepare_comment(get, connection, key, issue, request):
    return {"request": {"key": key, "body": request["body"]}, "noop": None, "card": _card(
        "comment", key, issue, "Comment on %s" % key,
        edit={"body": {"label": "Comment", "value": request["body"], "multiline": True, "required": True}})}


def _prepare_transition(get, connection, key, issue, request):
    wanted, status = request["to"], issue.get("status") or ""
    offered = issue.get("transitions") or []
    chosen = (next((one for one in offered if str(one.get("id")) == str(wanted)), None)
              or next((one for one in offered if _is(one.get("to"), wanted)), None)
              or next((one for one in offered if _is(one.get("name"), wanted)), None))
    target = (chosen or {}).get("to") or (chosen or {}).get("name") or wanted
    if (chosen is None and _is(status, wanted)) or (chosen is not None and _is(status, chosen.get("to"))):
        return _noop(key, issue, "is already in %s" % status)
    if chosen is None:
        raise WriteError("not_offered", "%s cannot be moved to %s now. It offers: %s." % (
            key, wanted, ", ".join("%s (id %s, to %s)" % (one.get("name"), one.get("id"), one.get("to"))
                                   for one in offered) or "nothing"))
    made = {"key": key, "to": str(chosen.get("id"))}
    if request.get("fields"):
        made["fields"] = dict(request["fields"])
    by = chosen.get("name") or ""
    return {"request": made, "noop": None, "card": _card(
        "transition", key, issue, "Move %s" % key, [{"label": "Status", "from": status, "to": target}],
        ["By the transition %s." % by] if by and not _is(by, target) else [])}


def _prepare_assign(get, connection, key, issue, request):
    wanted = request["account"].strip()
    now = issue.get("assignee") or ""
    if not wanted:
        account, name = "", ""
    elif wanted.casefold() == "me":
        myself = get("/rest/api/%s/myself" % api(connection["flavour"]))
        account, name = "me", operations._person(myself)
    else:
        people = writes.assignable(get, connection, {"key": key, "query": wanted})["people"]
        found = ([one for one in people if one["id"] == wanted] or [one for one in people if _is(one["name"], wanted)]
                 or (people if len(people) == 1 else []))
        if not found:
            raise WriteError("not_offered", "%s cannot be assigned to %s. %s" % (
                key, wanted, ("Several people match: %s." % ", ".join(one["name"] for one in people[:10]))
                if people else "Nobody it may be assigned to matches."))
        account, name = found[0]["id"], found[0]["name"]
    if _is(now, name):
        return _noop(key, issue, "is already assigned to %s" % (name or "nobody"))
    return {"request": {"key": key, "account": account}, "noop": None, "card": _card(
        "assign", key, issue, "Assign %s" % key,
        [{"label": "Assignee", "from": now or "nobody", "to": name or "nobody"}])}


_TEXT_FIELDS = (("summary", "Summary", False), ("description", "Description", True))


def _prepare_edit(get, connection, key, issue, request):
    wanted = dict(request["fields"])
    meta = writes.edit_meta(get, connection, {"key": key})
    closed = [one for one in wanted if one not in meta["editable"]]
    if closed:
        raise WriteError("refused", "%s's edit screen does not let this account change %s." % (
            key, ", ".join(sorted(closed))))
    now = {"summary": issue.get("summary") or "", "description": issue.get("description_text") or "",
           "labels": sorted(issue.get("labels") or []), "priority": issue.get("priority") or ""}
    made, changes, notes, edit = {}, [], [], {}
    for field, label, multiline in _TEXT_FIELDS:
        if field in wanted and wanted[field].strip() != now[field].strip():
            made[field] = wanted[field]
            edit["fields." + field] = {"label": label, "value": wanted[field], "was": _shown(now[field]),
                                       "multiline": multiline, "required": field == "summary"}
    if _labels_changed(wanted, now):
        made["labels"] = sorted({one.strip().replace(" ", "-") for one in wanted["labels"]})
        changes.append({"label": "Labels", "from": ", ".join(now["labels"]) or "none",
                        "to": ", ".join(made["labels"]) or "none"})
    if "priority" in wanted and not _is(wanted["priority"], now["priority"]):
        offered = meta["priorities"]
        chosen = next((one for one in offered if one["id"] == wanted["priority"] or _is(one["name"], wanted["priority"])),
                      None)
        if chosen is None:
            raise WriteError("not_offered", "%s cannot have the priority %s. It offers: %s." % (
                key, wanted["priority"], ", ".join(one["name"] for one in offered) or "nothing"))
        if not _is(chosen["name"], now["priority"]):
            made["priority"] = chosen["id"]
            changes.append({"label": "Priority", "from": now["priority"] or "none", "to": chosen["name"]})
    if not made:
        return _noop(key, issue, "already reads so")
    if "description" in made and not issue.get("description_plain", True):
        notes.append("The description has formatting (tables, pictures, lists) that plain text replaces.")
    return {"request": {"key": key, "fields": made, "updated": issue.get("updated") or ""}, "noop": None,
            "card": _card("edit", key, issue, "Edit %s" % key, changes, notes, edit)}


def _labels_changed(wanted, now):
    return "labels" in wanted and sorted({one.strip().replace(" ", "-") for one in wanted["labels"]}) != now["labels"]


def _prepare_comment_edit(get, connection, key, issue, request):
    flavour = connection["flavour"]
    raw = get("/rest/api/%s/issue/%s/comment/%s" % (api(flavour), key, request["id"]))
    found = operations._comment(raw if isinstance(raw, dict) else {}, flavour)
    if not found["id"]:
        raise ReadError("not_found")
    if found["text"].strip() == request["body"].strip():
        return _noop(key, issue, "comment %s already reads so" % found["id"])
    notes = ["A comment by %s." % found["author"]] if found["author"] else []
    if found["lost"]:
        notes.append("The comment has formatting (%s) that plain text replaces." % ", ".join(found["lost"]))
    return {"request": {"key": key, "id": found["id"], "body": request["body"], "updated": found["updated"]},
            "noop": None, "card": _card("comment_edit", key, issue, "Edit a comment on %s" % key, notes=notes,
                                        edit={"body": {"label": "Comment", "value": request["body"],
                                                       "was": _shown(found["text"]), "multiline": True,
                                                       "required": True}})}


def _prepare_attach(get, connection, key, issue, request):
    path, name = writes._upload_file(request["path"])
    size = os.path.getsize(path)
    shown = "%s (%s)" % (name, ("%.1f MB" % (size / 2 ** 20)) if size >= 2 ** 20 else "%d KB" % max(1, size // 1024))
    return {"request": {"key": key, "path": path}, "noop": None, "card": _card(
        "attach", key, issue, "Attach a file to %s" % key, [{"label": "File", "from": "", "to": shown}],
        ["From %s" % os.path.dirname(path)])}


def _prepare_create(get, connection, request):
    project = request["project"].upper()
    if connection.get("scope"):
        # Only into a project the connection's predefined query reaches.
        rows, _ = query.page(get, connection["flavour"], operations.jql({"project": project}, connection), 1,
                             ("key",))
        if not rows:
            raise ReadError("out_of_scope")
    types = writes.create_meta(get, connection, {"project": project})["types"]
    kind = next((one for one in types if one["id"] == request["type"] or _is(one["name"], request["type"])), None)
    if kind is None:
        raise WriteError("not_offered", "%s has no issue type %s. It has: %s." % (
            project, request["type"], ", ".join(one["name"] for one in types) or "none"))
    screen = {one["key"]: one for one in writes.create_meta(
        get, connection, {"project": project, "type": kind["id"]})["fields"]}
    given = {"summary": request["summary"]}
    for field in ("description", "labels", "priority"):
        if request.get(field):
            given[field] = request[field]
    given.update(request.get("fields") or {})
    extra, notes = {}, []
    for field, value in (request.get("fields") or {}).items():
        allowed = (screen.get(field) or {}).get("allowed") or []
        shown = value.get("id") if isinstance(value, dict) else value
        if allowed and isinstance(value, str):
            chosen = next((one for one in allowed if one["id"] == value or _is(one["name"], value)), None)
            if chosen is None:
                raise WriteError("not_offered", "%s cannot be %s. It may be: %s." % (
                    screen[field]["name"], value, ", ".join(one["name"] for one in allowed)))
            value, shown = {"id": chosen["id"]}, chosen["name"]
        extra[field] = value
        notes.append("%s: %s" % ((screen.get(field) or {}).get("name") or field, shown))
    missing = [one for one in screen.values() if one["required"] and one["key"] not in given]
    if missing:
        raise WriteError("fields_required", "A %s in %s asks for %s." % (
            kind["name"], project, ", ".join(one["name"] for one in missing)),
            {"needs": [{"key": one["key"], "name": one["name"]} for one in missing]})
    made = {"project": project, "type": kind["id"], "summary": request["summary"]}
    changes = [{"label": "Type", "from": "", "to": kind["name"]}]
    if request.get("labels"):
        made["labels"] = list(request["labels"])
        changes.append({"label": "Labels", "from": "", "to": ", ".join(made["labels"])})
    if request.get("priority"):
        offered = (screen.get("priority") or {}).get("allowed") or []
        chosen = next((one for one in offered if one["id"] == request["priority"]
                       or _is(one["name"], request["priority"])), None)
        if chosen is None:
            raise WriteError("not_offered", "A %s cannot have the priority %s." % (kind["name"], request["priority"]))
        made["priority"] = chosen["id"]
        changes.append({"label": "Priority", "from": "", "to": chosen["name"]})
    if extra:
        made["fields"] = extra
    made["description"] = request.get("description") or ""
    return {"request": made, "noop": None, "card": _card(
        "create", "", {"summary": ""}, "Create a %s in %s" % (kind["name"], project), changes, notes,
        {"summary": {"label": "Summary", "value": request["summary"], "multiline": False, "required": True},
         "description": {"label": "Description", "value": made["description"], "multiline": True}})}


PREPARE = {"comment": _prepare_comment, "transition": _prepare_transition, "assign": _prepare_assign,
           "edit": _prepare_edit, "comment_edit": _prepare_comment_edit, "attach": _prepare_attach}


def write(name, connection, token, request, get=None):
    """Make the approved change with the page's own write; the issue after it."""
    if name not in CONTRACTS or CONTRACTS[name].access != "write":
        raise WriteError("unsupported")
    get = get or operations.session(connection, token)
    request = dict(request)
    if name == "create":
        fields = dict(request.get("fields") or {}, summary=request["summary"])
        if (request.get("description") or "").strip():
            fields["description"] = request["description"]
        if request.get("labels"):
            fields["labels"] = request["labels"]
        if request.get("priority"):
            fields["priority"] = {"id": request["priority"]}
        request = {"project": request["project"], "type": request["type"], "fields": fields}
    try:
        made = writes.OPERATIONS[name](get, connection, request)
    except writes.Refused as exc:
        raise _refused(exc) from None
    except JiraError as exc:
        if exc.status == 0:
            # Sent, or not: the answer never came, so nobody can say.
            raise WriteError("unknown_outcome") from None
        raise
    return {"changed": True, "summary": made.get("summary") or name, "issue": made["issue"]}


def provider():
    return Provider("jira", CONTRACTS, dict(operations.OPERATIONS, **writes.OPERATIONS), read,
                    prepare=prepare, write=write)
