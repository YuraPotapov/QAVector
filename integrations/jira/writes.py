"""What the Integrations page changes in Jira, each answered with the issue as it is now.

Every write here is one action a person took, and every one answers with the
issue read back afterwards - in the same process - so the page shows what Jira
now holds without asking again, and never what it hoped it would hold.

Nothing here guesses. A transition whose screen asks for fields says which ones
rather than failing with a 400; an edit made on an issue somebody else changed
in the meantime is refused with that said, rather than written over their change;
a file is fetched only from the connection's own site.
"""

import mimetypes
import os

from integrations.jira import adf
from integrations.jira.client import CLOUD, JiraError, api
from integrations.jira.operations import _key, _person, issue

#: The largest file the page uploads. Jira Cloud's own default limit is larger,
#: but a file this big is a decision somebody should make in Jira itself.
UPLOAD_BYTES = 100 * 1024 * 1024


class Refused(Exception):
    """A write that was not made, for a reason the page can show and act on.

    ``kind``: ``fields`` (a transition needs fields - ``needs`` says which),
    ``conflict`` (the issue changed since it was opened), ``invalid``.
    """

    def __init__(self, message, kind="invalid", **details):
        super().__init__(message)
        self.kind = kind
        self.details = details


def _version(connection):
    return api(connection["flavour"])


def _after(get, connection, key):
    """The issue as Jira holds it now, with its newest comments."""
    return issue(get, connection, {"key": key})


# -- the workflow -------------------------------------------------------------
def transition(get, connection, request):
    """Move an issue by one of the transitions it offers.

    ``to`` is the transition's id. When its screen requires fields that have no
    default, the answer is :class:`Refused` ``fields`` with what they are and
    what they may be - unless ``fields`` brings them.
    """
    key = _key(request)
    wanted = str(request.get("to") or "")
    version = _version(connection)
    offered = get("/rest/api/%s/issue/%s/transitions" % (version, key),
                  {"expand": "transitions.fields"}).get("transitions") or []
    chosen = next((one for one in offered if str(one.get("id")) == wanted), None)
    if chosen is None:
        raise Refused("%s cannot be moved that way now. It offers: %s."
                      % (key, ", ".join(str(one.get("name")) for one in offered)
                         or "nothing"))
    given = dict(request.get("fields") or {})
    needs = []
    for field_id, spec in (chosen.get("fields") or {}).items():
        if not spec.get("required") or spec.get("hasDefaultValue") or field_id in given:
            continue
        needs.append({"key": field_id, "name": str(spec.get("name") or field_id),
                      "allowed": [{"id": str(one.get("id") or ""),
                                   "name": str(one.get("name") or one.get("value") or "")}
                                  for one in spec.get("allowedValues") or []
                                  if isinstance(one, dict)]})
    if needs:
        raise Refused("%s asks for %s." % (chosen.get("name"),
                                           ", ".join(one["name"] for one in needs)),
                      "fields", needs=needs, transition=str(chosen.get("name") or ""))
    body = {"transition": {"id": wanted}}
    if given:
        body["fields"] = {name: ({"id": value} if isinstance(value, str) else value)
                          for name, value in given.items()}
    get("/rest/api/%s/issue/%s/transitions" % (version, key), body=body)
    return dict(_after(get, connection, key),
                summary="%s: %s" % (key, chosen.get("name") or wanted))


# -- comments -----------------------------------------------------------------
def comment(get, connection, request):
    key = _key(request)
    text = str(request.get("body") or "").strip()
    if not text:
        raise Refused("A comment needs some words.")
    get("/rest/api/%s/issue/%s/comment" % (_version(connection), key),
        body={"body": adf.from_text(text, connection["flavour"])})
    return dict(_after(get, connection, key), summary="%s: comment" % key)


# -- editing ------------------------------------------------------------------
def edit_meta(get, connection, request):
    """What may be changed on an issue now: priorities offered, and which of
    the editor's fields the issue's screen lets this account edit."""
    key = _key(request)
    fields = get("/rest/api/%s/issue/%s/editmeta" % (_version(connection), key)).get(
        "fields") or {}
    priority = fields.get("priority") or {}
    return {"editable": sorted(name for name in ("summary", "description", "labels",
                                                 "priority", "assignee")
                               if name in fields),
            "priorities": [{"id": str(one.get("id") or ""),
                            "name": str(one.get("name") or "")}
                           for one in priority.get("allowedValues") or []
                           if isinstance(one, dict)]}


def edit(get, connection, request):
    """Change summary, description, labels or priority - only those given.

    ``updated`` is when the issue was last changed as the editor saw it: if
    Jira's is different, somebody else changed it since, and the edit is
    refused rather than written over their change.
    """
    key = _key(request)
    version = _version(connection)
    seen = str(request.get("updated") or "")
    now = (get("/rest/api/%s/issue/%s" % (version, key), {"fields": "updated"}).get(
        "fields") or {}).get("updated") or ""
    if seen and now and seen != now:
        raise Refused("%s was changed in Jira after you opened it. Reload it, and make "
                      "your change again." % key, "conflict")
    wanted = dict(request.get("fields") or {})
    fields = {}
    if "summary" in wanted:
        summary = str(wanted["summary"] or "").strip()
        if not summary:
            raise Refused("A summary is needed.")
        fields["summary"] = summary
    if "description" in wanted:
        text = str(wanted["description"] or "")
        fields["description"] = adf.from_text(text, connection["flavour"]) if text.strip() \
            else None
    if "labels" in wanted:
        fields["labels"] = [one for one in (str(item).strip().replace(" ", "-")
                                            for item in wanted["labels"] or []) if one]
    if "priority" in wanted and wanted["priority"]:
        fields["priority"] = {"id": str(wanted["priority"])}
    if not fields:
        raise Refused("Nothing to change.")
    get("/rest/api/%s/issue/%s" % (version, key), body={"fields": fields}, method="PUT")
    return dict(_after(get, connection, key),
                summary="%s: %s" % (key, ", ".join(sorted(fields))))


# -- who carries it -----------------------------------------------------------
def assignable(get, connection, request):
    """People the issue may be assigned to, matching ``query`` - names only to show,
    and the id Jira needs back."""
    key = _key(request)
    text = str(request.get("query") or "")
    params = {"issueKey": key, "maxResults": 50}
    params["query" if connection["flavour"] == CLOUD else "username"] = text
    found = get("/rest/api/%s/user/assignable/search" % _version(connection), params)
    return {"people": [{"id": _account(one, connection), "name": _person(one)}
                       for one in found if isinstance(one, dict)]
            if isinstance(found, list) else []}


def assign(get, connection, request):
    """Assign an issue: ``account`` is ``me``, an id from :func:`assignable`, or
    empty for nobody."""
    key = _key(request)
    version = _version(connection)
    account = str(request.get("account") or "")
    if account == "me":
        account = _account(get("/rest/api/%s/myself" % version), connection)
    cloud = connection["flavour"] == CLOUD
    body = {"accountId": account or None} if cloud else {"name": account or None}
    get("/rest/api/%s/issue/%s/assignee" % (version, key), body=body, method="PUT")
    after = _after(get, connection, key)
    return dict(after, summary="%s: assigned to %s"
                % (key, after["issue"].get("assignee") or "nobody"))


def _account(person, connection):
    if connection["flavour"] == CLOUD:
        return str((person or {}).get("accountId") or "")
    return str((person or {}).get("name") or (person or {}).get("key") or "")


# -- creating -----------------------------------------------------------------
def create_meta(get, connection, request):
    """What an issue of a project may be: its issue types, or - with ``type`` -
    the fields that type's create screen has, required ones first."""
    project = str(request.get("project") or "").strip()
    if not project:
        raise Refused("Choose a project.")
    version = _version(connection)
    base = "/rest/api/%s/issue/createmeta/%s/issuetypes" % (version, project)
    kind = str(request.get("type") or "")
    if not kind:
        answer = get(base, {"maxResults": 100})
        types = answer.get("issueTypes") or answer.get("values") or []
        return {"types": [{"id": str(one.get("id") or ""), "name": str(one.get("name") or ""),
                           "subtask": bool(one.get("subtask"))}
                          for one in types if isinstance(one, dict)]}
    answer = get("%s/%s" % (base, kind), {"maxResults": 200})
    fields = answer.get("fields") or answer.get("values") or []
    shown = []
    for one in fields:
        if not isinstance(one, dict):
            continue
        field_id = str(one.get("fieldId") or one.get("key") or "")
        if field_id in ("project", "issuetype", "reporter"):
            continue
        schema = one.get("schema") or {}
        shown.append({"key": field_id, "name": str(one.get("name") or field_id),
                      "required": bool(one.get("required")) and not one.get("hasDefaultValue"),
                      "type": str(schema.get("type") or ""),
                      "items": str(schema.get("items") or ""),
                      "allowed": [{"id": str(value.get("id") or ""),
                                   "name": str(value.get("name") or value.get("value") or "")}
                                  for value in one.get("allowedValues") or []
                                  if isinstance(value, dict)]})
    shown.sort(key=lambda one: (not one["required"], one["key"] != "summary"))
    return {"fields": shown}


def create(get, connection, request):
    """A new issue. ``fields`` as the create screen named them; text becomes the
    flavour's document format, a choice an ``{"id"}``."""
    project = str(request.get("project") or "").strip()
    kind = str(request.get("type") or "").strip()
    wanted = dict(request.get("fields") or {})
    if not project or not kind:
        raise Refused("Choose a project and an issue type.")
    if not str(wanted.get("summary") or "").strip():
        raise Refused("A summary is needed.")
    fields = {"project": {"key": project}, "issuetype": {"id": kind}}
    for name, value in wanted.items():
        if value in (None, "", []):
            continue
        if name == "description":
            fields[name] = adf.from_text(str(value), connection["flavour"])
        elif name == "labels":
            fields[name] = [str(one).strip().replace(" ", "-") for one in value if str(one).strip()]
        elif isinstance(value, dict):
            fields[name] = value
        else:
            fields[name] = value
    made = get("/rest/api/%s/issue" % _version(connection), body={"fields": fields})
    key = str(made.get("key") or "")
    if not key:
        raise JiraError("Jira did not say which issue it made.")
    return dict(_after(get, connection, key), summary="%s: created" % key)


# -- files --------------------------------------------------------------------
def attach(get, connection, request):
    key = _key(request)
    path = os.path.abspath(os.path.expanduser(str(request.get("path") or "")))
    if not os.path.isfile(path):
        raise Refused("There is no file at %s." % path)
    size = os.path.getsize(path)
    if size > UPLOAD_BYTES:
        raise Refused("%s is %d MB; the page uploads up to %d MB. Attach it in Jira."
                      % (os.path.basename(path), size // 2 ** 20, UPLOAD_BYTES // 2 ** 20))
    with open(path, "rb") as handle:
        data = handle.read()
    name = os.path.basename(path)
    get.upload("/rest/api/%s/issue/%s/attachments" % (_version(connection), key), name, data,
               mimetypes.guess_type(name)[0] or "application/octet-stream")
    return dict(_after(get, connection, key), summary="%s: attached %s" % (key, name))


def download(get, connection, request):
    """One attachment's bytes, written to ``to``. Only from the connection's own
    site (or its gateway): an address out of a payload is never followed."""
    key = _key(request)
    attachment = str(request.get("id") or "")
    target = os.path.abspath(os.path.expanduser(str(request.get("to") or "")))
    if not attachment.isdigit():
        raise Refused("An attachment id is needed.")
    if not os.path.isdir(os.path.dirname(target)):
        raise Refused("There is no folder %s." % os.path.dirname(target))
    # The address the issue gave for the file - checked by fetch() against the
    # site and its gateway, so a payload cannot send the token elsewhere - or
    # the REST one when there is none.
    data = get.fetch(str(request.get("url") or "") or "%s/rest/api/%s/attachment/content/%s"
                     % (get.api, _version(connection), attachment))
    temp = target + ".part"
    with open(temp, "wb") as handle:
        handle.write(data)
    os.replace(temp, target)
    return {"path": target, "bytes": len(data), "summary": "%s: saved %s"
            % (key, os.path.basename(target))}


#: Operation name -> function. Every one of these changes something in Jira,
#: except the two that only say what a change may be.
OPERATIONS = {"transition": transition, "comment": comment, "edit_meta": edit_meta,
              "edit": edit, "assignable": assignable, "assign": assign,
              "create_meta": create_meta, "create": create, "attach": attach,
              "download": download}

#: The ones that change Jira - what the activity log records.
CHANGES = ("transition", "comment", "edit", "assign", "create", "attach")
