"""What the Integrations page asks Jira, one request of a person at a time.

Each operation is ``(session, connection, request) -> dict`` and makes the
fewest requests that answer it: the list asks for the handful of fields a row
shows and nothing else, an opened issue is read alone, comments are the newest
few, and a file is fetched only when somebody opens it. Nothing here runs on
its own - the page decides when to ask, and asks only for what is on screen.
"""

import re

from integrations.jira import adf, query
from integrations.jira.client import CLOUD, JiraError, Session, api

#: What one row of the issue list shows - and so everything the list asks for.
LIST_FIELDS = ("summary", "status", "issuetype", "priority", "assignee", "updated",
               "project")

#: How many issues Jira's own quick search (the issue picker) may add to a
#: text search - what a person would look through, not a list.
PICKED = 20

#: What an opened issue shows, apart from its comments, which are asked for
#: separately so a thread of hundreds costs the newest few.
DETAIL_FIELDS = ("summary", "description", "status", "issuetype", "priority",
                 "assignee", "reporter", "created", "updated", "labels",
                 "project", "attachment", "parent")

#: Rows per page of the list unless the page says otherwise, and the most one
#: request of it may ask for.
PAGE_ROWS = 25
MOST_ROWS = 200

#: Comments an opened issue shows before "Show earlier".
COMMENTS = 20

#: What bounds a list nobody filtered. Cloud refuses a search with no condition
#: at all, and "everything this account can see" is not a list anybody reads.
UNFILTERED = "updated >= -90d"

ISSUE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$")


def session(connection, token):
    """A :class:`Session` for one connection and its token - an access token,
    for a connection signed in through Atlassian."""
    site = str(connection.get("site") or "").rstrip("/")
    if connection.get("auth") == "oauth":
        from integrations import oauth
        return Session(site=site, auth="bearer", email="", token=token, verify=True,
                       api=oauth.api_base(connection.get("cloud_id", "")))
    from integrations.jira import client
    through = connection.get("gateway") and connection.get("cloud_id")
    return Session(site=site, auth=connection.get("auth", "basic"),
                   email=str(connection.get("email") or ""), token=token,
                   verify=bool(connection.get("verify_tls", True)),
                   api=client.gateway(connection["cloud_id"]) if through else None,
                   find_gateway=connection.get("flavour") == CLOUD and not through)


# -- reading ------------------------------------------------------------------
def test(get, connection, request):
    """Who the connection signs in as. The one check a person asks for."""
    me = query.signed_in(get, connection["flavour"])
    return {"account": _person(me) or "?",
            "site": get.site}


def projects(get, connection, request):
    """The projects the account can see, as ``[{key, name}]``, by name."""
    found = []
    if connection["flavour"] == CLOUD:
        start = 0
        while len(found) < 1000:
            page = get("/rest/api/3/project/search",
                       {"startAt": start, "maxResults": 100, "orderBy": "name"})
            values = page.get("values") or []
            found.extend(values)
            start += len(values)
            if not values or page.get("isLast", True):
                break
    else:
        answer = get("/rest/api/2/project")
        found = answer if isinstance(answer, list) else []
    rows = [{"key": str(one.get("key") or ""), "name": str(one.get("name") or "")}
            for one in found if isinstance(one, dict) and one.get("key")]
    return {"projects": sorted(rows, key=lambda one: one["name"].lower())}


def statuses(get, connection, request):
    """Every status name the site has, once each, with its category."""
    answer = get("/rest/api/%s/status" % api(connection["flavour"]))
    seen, rows = set(), []
    for one in answer if isinstance(answer, list) else []:
        name = str((one or {}).get("name") or "")
        if name and name.lower() not in seen:
            seen.add(name.lower())
            rows.append({"name": name, "category": _category(one)})
    order = {"new": 0, "indeterminate": 1, "done": 2}
    rows.sort(key=lambda one: (order.get(one["category"], 1), one["name"].lower()))
    return {"statuses": rows}


def jql(request, connection=None, picked=()):
    """The JQL a list request stands for.

    ``jql`` given is used as written - the JQL toggle is somebody typing a
    query, and rewriting it would be second-guessing them - with the ordering
    added only when it has none. Otherwise the filters are joined: projects,
    statuses, "assigned to me", and the search text, which is an issue key or
    words. With none of them the list is bounded by :data:`UNFILTERED`.

    ``picked`` are the keys Jira's own quick search found for the text (see
    :func:`picked`): words alone miss "1334" for QA-1334 - an issue's key is
    not text to Jira - and the start of a word.
    """
    order = query.ordering(str(request.get("order") or ""))
    written = str(request.get("jql") or "").strip()
    if written:
        if re.search(r"\border\s+by\b", written, re.IGNORECASE):
            return written
        return "%s ORDER BY %s" % (written, order)
    clauses = []
    project = request.get("project")
    if project in (None, "") and connection is not None and not request.get("everywhere"):
        # ``everywhere``: looking for an issue rather than at a list, so the
        # connection's default project does not narrow it.
        project = (connection.get("settings") or {}).get("default_project", "")
    for clause in (query.projects_clause(project or ""),
                   query.statuses_clause(request.get("status") or "")):
        if clause:
            clauses.append(clause)
    if request.get("mine"):
        clauses.append("assignee = currentUser()")
    text = str(request.get("text") or "").strip()
    if text and ISSUE_KEY.match(text):
        clauses.append('key = "%s"' % query.escape(text.upper()))
    elif text and picked:
        clauses.append('(text ~ "%s" OR key in (%s))' % (
            query.escape(text), ", ".join('"%s"' % query.escape(one) for one in picked)))
    elif text:
        clauses.append('text ~ "%s"' % query.escape(text))
    if not clauses:
        clauses.append(UNFILTERED)
    return "%s ORDER BY %s" % (" AND ".join(clauses), order)


def search(get, connection, request):
    """One page of the list: ``{rows, cursor, jql}``.

    ``size`` rows from ``cursor`` (a page of :data:`PAGE_ROWS` by default).
    Refresh asks again from the start for as many rows as are on screen, which
    may take more than one request of Jira but never more than those rows.
    """
    flavour = connection["flavour"]
    wanted = _bounded(request.get("size"), PAGE_ROWS, MOST_ROWS)
    text = str(request.get("text") or "").strip()
    found = (picked(get, flavour, text)
             if text and not request.get("jql") and not ISSUE_KEY.match(text) else [])
    question = jql(request, connection, found)
    rows, cursor = [], request.get("cursor") or None
    first = cursor
    try:
        while len(rows) < wanted:
            raw, cursor = query.page(get, flavour, question,
                                     min(query.PAGE, wanted - len(rows)),
                                     LIST_FIELDS, cursor)
            rows.extend(row(one, get.site) for one in raw)
            if cursor is None:
                break
        if not rows and not first:
            # Cloud answers a refused email and token as nobody, with no
            # issues; "no issues" must not be how an expired token looks.
            before = get.api
            query.signed_in(get, flavour)
            if get.api != before:
                return search(get, connection, request)
    except JiraError as exc:
        typed = str(request.get("text") or "").strip().upper()
        if (exc.status == 400 and not request.get("jql") and ISSUE_KEY.match(typed)
                and typed.lower() in str(exc).lower()):
            # A key typed into the search that no issue has: no rows, not an
            # error - the same answer a word nothing contains gets.
            return {"rows": [], "cursor": None, "jql": question}
        raise
    return {"rows": rows[:wanted], "cursor": cursor, "jql": question}


def picked(get, flavour, text):
    """Keys of the issues Jira's own quick search finds for ``text`` - by part
    of a key ("1334", "QA-13") or the start of a word - as its search box
    does. Empty when that search is not there or fails: the words still go."""
    try:
        raw = get("/rest/api/%s/issue/picker" % api(flavour),
                  {"query": text, "showSubTasks": "true", "showSubTaskParent": "true"})
    except JiraError:
        return []
    keys = []
    for section in (raw or {}).get("sections") or []:
        for one in section.get("issues") or []:
            key = str(one.get("key") or "")
            if ISSUE_KEY.match(key) and key not in keys:
                keys.append(key)
    return keys[:PICKED]


def issue(get, connection, request):
    """Everything an opened issue shows, with its newest comments."""
    flavour = connection["flavour"]
    key = _key(request)
    raw = get("/rest/api/%s/issue/%s" % (api(flavour), key),
              {"fields": ",".join(DETAIL_FIELDS), "expand": "transitions"})
    detail = _detail(raw, get.site, flavour)
    detail["comments"] = _comments(get, flavour, key, 0,
                                   _bounded(request.get("comments"), COMMENTS, 100))
    return {"issue": detail}


def comments(get, connection, request):
    """Earlier comments of one issue: ``start`` counts back from the newest."""
    key = _key(request)
    start = _bounded(request.get("start"), 0, 100000, least=0)
    return {"comments": _comments(get, connection["flavour"], key, start,
                                  _bounded(request.get("size"), COMMENTS, 100))}


def refresh(get, connection, request):
    """Refresh: the list's rows on screen and the opened issue, in one process.

    ``list`` is a ``search`` request, ``issue`` an ``issue`` one or absent. An
    opened issue that is gone is said in ``issue_error`` rather than failing
    the list it came with.
    """
    answer = {"list": search(get, connection, dict(request.get("list") or {})),
              "issue": None}
    wanted = request.get("issue")
    if wanted:
        try:
            answer["issue"] = issue(get, connection, dict(wanted))["issue"]
        except JiraError as exc:
            if exc.status not in (404, 410):
                raise
            answer["issue_error"] = ("It was deleted or moved, or this account can no "
                                     "longer see it.")
    return answer


#: Operation name -> function. A name not here is refused before Jira is asked.
#: People one "who can be mentioned" read brings back.
MENTION_PEOPLE = 50


def mentionable(get, connection, request):
    """People who can see an issue, matching ``query``: who a comment or a
    description may mention. An empty ``query`` is whoever Jira lists first.

    Cloud documents ``query`` as required, and may refuse an empty one. Then
    the people the issue can be assigned to are the start instead: that
    search needs no query. Either way it is one read, and one more only when
    the first is refused.
    """
    key = _key(request)
    text = str(request.get("query") or "").strip()
    flavour = connection["flavour"]
    version = api(flavour)
    params = {"issueKey": key, "maxResults": MENTION_PEOPLE}
    if flavour == CLOUD:
        if text:
            params["query"] = text
        try:
            found = get("/rest/api/%s/user/viewissue/search" % version, params)
        except JiraError as error:
            if text or error.status != 400:
                raise
            found = get("/rest/api/%s/user/assignable/search" % version,
                        {"issueKey": key, "maxResults": MENTION_PEOPLE})
    else:
        params["username"] = text
        found = get("/rest/api/%s/user/viewissue/search" % version, params)
    people = []
    for one in found if isinstance(found, list) else []:
        if not isinstance(one, dict):
            continue
        account = account_of(one, flavour)
        if account:
            people.append({"id": account, "name": _person(one) or account})
    return {"people": people}


def account_of(person, flavour):
    """The id Jira takes back for a person: Cloud's account id, Server's name."""
    if flavour == CLOUD:
        return str((person or {}).get("accountId") or "")
    return str((person or {}).get("name") or (person or {}).get("key") or "")


OPERATIONS = {"test": test, "projects": projects, "statuses": statuses,
              "search": search, "issue": issue, "comments": comments,
              "refresh": refresh, "mentionable": mentionable}


def call(name, connection, token, request, get=None):
    """Run one operation for one connection. Raises ``JiraError``/``ValueError``.

    ``get`` is the session to ask with, for a caller that wants to see
    afterwards whether it found the gateway (``get.switched``).
    """
    from integrations.jira import writes
    operation = OPERATIONS.get(name) or writes.OPERATIONS.get(name)
    if operation is None:
        raise ValueError("Unknown Jira operation %r. Known: %s"
                         % (name, ", ".join(sorted(set(OPERATIONS) | set(writes.OPERATIONS)))))
    return operation(get or session(connection, token), connection, dict(request or {}))


# -- shapes -------------------------------------------------------------------
def row(raw, site):
    """One issue as the list shows it."""
    fields = raw.get("fields") or {}
    key = str(raw.get("key") or "")
    return {"key": key, "summary": fields.get("summary") or "",
            "type": _name(fields.get("issuetype")),
            "priority": _name(fields.get("priority")),
            "status": _name(fields.get("status")),
            "category": _category(fields.get("status")),
            "assignee": _person(fields.get("assignee")),
            "updated": fields.get("updated") or "",
            "project": str((fields.get("project") or {}).get("key") or ""),
            "project_name": str((fields.get("project") or {}).get("name") or ""),
            "url": "%s/browse/%s" % (site, key)}


def _detail(raw, site, flavour):
    fields = raw.get("fields") or {}
    found = row(raw, site)
    description = fields.get("description")
    project = fields.get("project") or {}
    doc, lost = adf.editable(description) if flavour == CLOUD else (None, [])
    found.update({
        "reporter": _person(fields.get("reporter")),
        "created": fields.get("created") or "",
        "labels": [str(one) for one in fields.get("labels") or []],
        "project": {"key": str(project.get("key") or ""),
                    "name": str(project.get("name") or "")},
        "parent": str((fields.get("parent") or {}).get("key") or "")
        if isinstance(fields.get("parent"), dict) else "",
        "description_html": adf.to_html(description) if description else "",
        "description_text": adf.to_text(description, flavour),
        # Whether editing the description as text would lose anything - a
        # table, a list, a picture. The editor warns before it does.
        "description_plain": adf.is_plain(description),
        # On Cloud, the document as the description editor can hold it, and
        # what it would lose: nothing, unless the document has tables,
        # pictures or the like.
        "description_doc": doc,
        "description_lost": lost,
        "attachments": [_attachment(one) for one in fields.get("attachment") or []
                        if isinstance(one, dict)],
        "transitions": [{"id": str(one.get("id") or ""),
                         "name": str(one.get("name") or ""),
                         "to": _name(one.get("to")),
                         "category": _category(one.get("to"))}
                        for one in raw.get("transitions") or []
                        if isinstance(one, dict)],
    })
    return found


def _comments(get, flavour, key, start, size):
    """``{items, total, start}``: up to ``size`` comments, newest first."""
    page = get("/rest/api/%s/issue/%s/comment" % (api(flavour), key),
               {"startAt": start, "maxResults": size, "orderBy": "-created"})
    raw = (page.get("comments") or [])[:size]
    items = [{"id": str(one.get("id") or ""),
              "author": _person(one.get("author")),
              "created": one.get("created") or "",
              "html": adf.to_html(one.get("body")) if one.get("body") else ""}
             for one in raw if isinstance(one, dict)]
    return {"items": items, "start": start,
            "total": int(page.get("total") or len(items))}


def _attachment(raw):
    return {"id": str(raw.get("id") or ""), "url": str(raw.get("content") or ""),
            "filename": str(raw.get("filename") or ""),
            "mime": str(raw.get("mimeType") or ""),
            "size": int(raw.get("size") or 0),
            "created": raw.get("created") or "",
            "author": _person(raw.get("author"))}


def _name(value):
    return str((value or {}).get("name") or "") if isinstance(value, dict) else ""


def _category(status):
    """``new``, ``indeterminate`` or ``done``: what a status chip is coloured by."""
    if not isinstance(status, dict):
        return ""
    return str((status.get("statusCategory") or {}).get("key") or "")


def _person(value):
    """A name to show. Never the account id or the email."""
    if not isinstance(value, dict):
        return ""
    return str(value.get("displayName") or value.get("name") or "")


def _key(request):
    key = str(request.get("key") or "").strip().upper()
    if not ISSUE_KEY.match(key):
        raise ValueError("An issue key such as QA-12 is needed, not %r."
                         % request.get("key"))
    return key


def _bounded(value, default, most, least=1):
    try:
        number = int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        number = default
    return max(least, min(most, number))
