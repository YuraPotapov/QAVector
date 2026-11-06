"""Asking Jira for issues: the JQL, and the pages it comes back in.

Shared by ``jira.issues`` and the Integrations page, which ask the same question
in two shapes - a step wants up to ``limit`` issues in one go, the page wants
one page at a time and a way to ask for the next.
"""

from integrations.jira.client import CLOUD, SERVER, api

#: Which side of an issue the person is on. ``any`` is the union, which is the
#: honest default for "what is on their plate" - work is often reported by one
#: person and carried by another. ``everyone`` asks about nobody in particular:
#: a column of the board - Ready for prod, whoever carries it - rather than one
#: person's plate.
EVERYONE = "everyone"
ROLES = ("assignee", "reporter", "creator", "mentioned", "watcher", "any",
         EVERYONE)

#: The union ``any`` expands to. Not ``watcher`` or ``mentioned``: both are far
#: wider than "their work" and would bury the answer.
ANY_ROLES = ("assignee", "reporter", "creator")

#: How issues come back when nobody says. Newest first is right for "what is
#: going on" - the question somebody reading a list is usually asking.
DEFAULT_ORDER = "updated DESC"

#: One page. Jira caps this itself; asking for more simply gets fewer.
PAGE = 50


def jql_for(user, role, extra="", project="", order="", status=""):
    """The JQL for "this person's issues", in the order the step asked for.

    Quoted, with the quotes in the names escaped. A user string and a project
    key both reach this from a cycle file or a variable, and JQL has the same
    injection problem every query language has: an unescaped quote ends the
    literal and whatever follows is read as query.

    ``order`` is the ORDER BY without those words. It is a field of its own
    rather than something written into ``extra`` because ``extra`` is bracketed
    - and an ORDER BY inside brackets is a syntax error, not an ordering. It
    matters more than it looks: a cycle that takes one task per run is choosing
    which task here.

    ``everyone`` leaves the person out altogether - a column of the board,
    whoever carries it - and the clauses that are left are joined as they are.
    """
    who = (user or "").strip()
    literal = who if who.endswith("()") else '"%s"' % escape(who)
    if role == EVERYONE:
        clauses = []
    elif role == "any":
        clauses = ["(%s)" % " OR ".join("%s = %s" % (one, literal)
                                        for one in ANY_ROLES)]
    elif role == "mentioned":
        clauses = ["text ~ %s" % literal]   # the only way Jira expresses it
    else:
        clauses = ["%s = %s" % (role, literal)]

    for where in (projects_clause(project), statuses_clause(status)):
        if where:
            clauses.append(where)
    extra = (extra or "").strip()
    if extra:
        # Bracketed, because an OR inside it would otherwise swallow
        # everything to its left.
        clauses.append("(%s)" % extra)
    return ("%s ORDER BY %s" % (" AND ".join(clauses), ordering(order))
            ).lstrip()


def ordering(order):
    """The ORDER BY tail, with those words stripped if somebody wrote them.

    Written both ways by people who have typed JQL before, and both readings
    are obvious - so accepting one and producing a syntax error for the other
    would be a gratuitous way to fail.
    """
    order = (order or "").strip()
    if not order:
        return DEFAULT_ORDER
    without = order.lstrip()
    if without[:8].upper() == "ORDER BY":
        without = without[8:].strip()
    return without or DEFAULT_ORDER


def projects_clause(project):
    """``project = "QA"`` or ``project in ("QA", "WEB")``, or nothing at all.

    Blank means every project the account can see, which is the right default
    for "what is on this person's plate" - work crosses projects. It is not
    the right answer for every account, though: with a small ``limit`` and
    ``ORDER BY updated DESC``, one busy project can take the whole page and a
    quiet one never appears, which is a wrong answer nobody notices.
    """
    keys = [one.strip() for one in str(project or "").split(",")]
    keys = ['"%s"' % escape(one) for one in keys if one]
    if not keys:
        return ""
    if len(keys) == 1:
        return "project = %s" % keys[0]
    return "project in (%s)" % ", ".join(keys)


def statuses_clause(status):
    """``status = "In Progress"`` or ``status in (...)``, or nothing at all.

    The same shape as :func:`projects_clause`, and escaped for the same reason:
    a status name comes from a cycle file or a variable. The names are the
    workflow's own - nothing here knows what a project calls its columns.
    """
    names = [one.strip() for one in str(status or "").split(",")]
    names = ['"%s"' % escape(one) for one in names if one]
    if not names:
        return ""
    if len(names) == 1:
        return "status = %s" % names[0]
    return "status in (%s)" % ", ".join(names)


def escape(text):
    """``text`` made safe inside a double-quoted JQL literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def page(get, flavour, query, size, fields, cursor=None):
    """One page of issues: ``(raw issues, cursor of the next page or None)``.

    The cursor is whatever the flavour pages by - Cloud's opaque
    ``nextPageToken``, Server's ``startAt`` offset - and is only ever handed
    back to this function, so nothing above it has to know which.
    """
    if flavour == CLOUD:
        # The older /search endpoints are gone from Cloud; this one pages by
        # an opaque token rather than by offset.
        body = {"jql": query, "maxResults": size, "fields": list(fields)}
        if cursor:
            body["nextPageToken"] = cursor
        answer = get("/rest/api/3/search/jql", body=body)
        issues = answer.get("issues") or []
        token = answer.get("nextPageToken")
        more = bool(issues) and bool(token) and not answer.get("isLast")
        return issues, (token if more else None)
    start = int(cursor or 0)
    answer = get("/rest/api/2/search",
                 {"jql": query, "maxResults": size, "startAt": start,
                  "fields": ",".join(fields)})
    issues = answer.get("issues") or []
    start += size
    more = bool(issues) and start < int(answer.get("total") or 0)
    return issues, (start if more else None)


def search(get, flavour, query, limit, cancel, fields):
    """Every issue the query finds, up to ``limit``, in the query's order.

    Paged, because Jira caps a page well below what a step may ask for, and
    silently returning the first fifty of two hundred would be the kind of
    wrong answer nobody notices.
    """
    found, cursor = [], None
    while len(found) < limit:
        if cancel is not None:
            cancel.raise_if_set()
        issues, cursor = page(get, flavour, query, min(PAGE, limit - len(found)),
                              fields, cursor)
        found.extend(issues)
        if cursor is None:
            break
    if not found:
        before = getattr(get, "api", "")
        signed_in(get, flavour)
        if getattr(get, "api", "") != before:
            # The token was refused at the site and taken by the gateway: the
            # empty answer was the site's, not the queue's.
            return search(get, flavour, query, limit, cancel, fields)
    return found[:limit]


def signed_in(get, flavour):
    """Who the credentials belong to; raises ``JiraError`` when refused.

    Cloud answers a search with refused Basic credentials as an anonymous one:
    200 and no issues, since ``currentUser()`` is nobody and nobody can browse
    the project. An expired token would read as an empty queue, so an empty
    answer is checked against the one endpoint that does say 401.
    """
    return get("/rest/api/%s/myself" % api(flavour))


__all__ = ["CLOUD", "SERVER", "EVERYONE", "ROLES", "ANY_ROLES", "DEFAULT_ORDER",
           "PAGE", "jql_for", "ordering", "projects_clause", "statuses_clause",
           "escape", "page", "search", "signed_in"]
