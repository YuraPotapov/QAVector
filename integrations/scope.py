"""A connection's predefined query: the part of every question it asks.

A project's connection may say which part of its service belongs to the project
- ``project = {{jira_project}}`` - and every query made through it is ANDed with
that: the Issues list, a typed query, a key, a search from anywhere, a cycle's
queue. Two QAVector projects can then hold the same connection text and see
different Jira projects.

The query may use variables, in the ``{{name}}`` form an environment's own
``url`` uses, and they are read from **one environment the connection names**
(``settings.variables_from``). The environments are the project's
``launch-projects.json``; this module reads only what it needs of that file - an
environment's id and its variables - and nothing else of it.

**Injection.** The scope is ANDed into queries as ``(scope) AND (the rest)``,
so anything that could close that bracket early would take the rest of the
query out of it. Three rules keep that from happening:

* a variable's value is always **one quoted, escaped literal** - ``{{x}}``
  becomes ``"QA"``, never raw text - so an edited environments file cannot
  add a clause, whatever it puts in the value; a value with control
  characters is refused, and so is ``{{x}}`` written inside quotes;
* the predefined query, a typed query and a cycle's extra JQL must each be
  **balanced** - every bracket closed, every quote ended - or they are refused
  (:func:`clause`), so none of them can close a bracket it did not open;
* ORDER BY is found only at the top level, outside quotes and brackets, and a
  predefined query may not have one.

Resolution is strict. A variable the environment does not define, or an
environment that is not there, is an error rather than an empty string: an
unresolved scope must never become "every project", which is exactly what the
person wrote it to prevent.

What this is not: a permission. The token's own Jira permissions are what an
account can read; the scope says which part of that belongs to this project.
"""

import json
import os
import re

#: The environments file of a project, beside its connections.
ENVIRONMENTS_FILE = "launch-projects.json"

#: One ``{{name}}``. The name rule is the environments' own
#: (``gui/cms_gui/launchvars.py``), so a variable that is valid there is here.
REFERENCE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: ORDER BY as JQL reads it: two words, any case, any space between.
_ORDER_BY = re.compile(r"order\s+by\b", re.IGNORECASE)

#: ``project = "KEY"``, ``project = KEY`` or ``project in ("A", B)``.
_PROJECT = re.compile(
    r"\bproject\s*(?:=\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[A-Za-z0-9_-]+)"
    r"|in\s*\(([^)]*)\))", re.IGNORECASE)

#: Characters no variable value may hold: they have no business in a query.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ScopeError(ValueError):
    """The predefined query, or a query ANDed with it, cannot be used.

    A ValueError, so a caller that already turns those into "invalid" (the
    integrations CLI) says it as such without knowing this module."""


# -- structure ----------------------------------------------------------------
def _structure(text):
    """``(problem or None, where a top-level ORDER BY starts or None)``.

    One pass over JQL that knows its two kinds of string - ``"..."`` and
    ``'...'``, with backslash escapes - and its brackets: enough to tell
    whether a piece is whole, which is all the AND around it needs.
    """
    depth, quote, order, index = 0, "", None, 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return "it closes a bracket it never opened", None
        elif (depth == 0 and order is None and _ORDER_BY.match(text, index)
                and (index == 0 or not (text[index - 1].isalnum() or text[index - 1] == "_"))):
            order = index
        index += 1
    if quote:
        return "a %s string is never ended" % ("quoted" if quote == '"' else "single-quoted"), None
    if depth:
        return "a bracket is never closed", None
    return None, order


def clause(text, what="The query"):
    """``(body, ordering)`` of a JQL piece: what may go inside a bracket, and
    its top-level ``ORDER BY ...`` ("" when none). Raises ScopeError when the
    piece is not whole - an unclosed bracket or quote, or a ``)`` too many -
    since ANDed into another query it could reach outside its brackets."""
    text = str(text or "").strip()
    problem, order = _structure(text)
    if problem:
        raise ScopeError("%s cannot be used: %s." % (what, problem))
    if order is None:
        return text, ""
    return text[:order].strip(), text[order:].strip()


def bracketed(query, what="The predefined query"):
    """``query`` as a clause another may be ANDed with - checked whole,
    bracketed - or "" when there is none. An ORDER BY is refused: a clause
    cannot have one. Raises ScopeError."""
    body, order = clause(query, what)
    if order:
        raise ScopeError("%s cannot have ORDER BY: it is ANDed into other queries, "
                         "which keep their own ordering." % what)
    return "(%s)" % body if body else ""


def anded(within, piece, what="The query"):
    """``(within) AND (piece)``, ``piece`` checked whole first; ``piece``'s own
    ORDER BY is kept at the end. ``within`` empty leaves ``piece`` as it is.
    Raises ScopeError."""
    limit = bracketed(within)
    if not limit:
        return str(piece or "").strip()
    body, order = clause(piece, what)
    joined = "%s AND (%s)" % (limit, body) if body else limit
    return ("%s %s" % (joined, order)).strip()


# -- variables ----------------------------------------------------------------
def names(template):
    """The variables ``template`` uses, once each, in order."""
    found = []
    for name in REFERENCE.findall(str(template or "")):
        if name not in found:
            found.append(name)
    return found


def literal(value):
    """``value`` as one JQL string literal. Raises ScopeError for a value no
    query should hold."""
    value = str(value if value is not None else "")
    if _CONTROL.search(value):
        raise ScopeError("a variable's value holds a line break or another control "
                         "character")
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def default_environments_path(integrations_path):
    """The environments file beside a project's connections file."""
    return os.path.join(os.path.dirname(os.path.abspath(integrations_path)),
                        ENVIRONMENTS_FILE)


#: How a virtual environment - a users.json value nobody has put in an
#: environment of its own - is named (``launchprojectsfile.VIRTUAL_PREFIX``).
VIRTUAL_PREFIX = "env:"


def environments(path):
    """``{id: (name, {variable: value})}`` for every environment written in
    ``path``. A missing file is none; an unreadable one raises ScopeError.

    A virtual id (``env:<value>``) also finds the one environment that took
    that users.json value since, as the Environments page itself does.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ScopeError("cannot read %s: %s" % (path, exc))
    found, holders = {}, {}
    projects = document.get("projects") if isinstance(document, dict) else None
    for project in projects if isinstance(projects, list) else []:
        rows = project.get("environments") if isinstance(project, dict) else None
        for one in rows if isinstance(rows, list) else []:
            if not isinstance(one, dict) or not one.get("id"):
                continue
            values = one.get("variables") if isinstance(one.get("variables"), dict) else {}
            found[str(one["id"])] = (str(one.get("name") or one["id"]),
                                     {str(key): "" if value is None else str(value)
                                      for key, value in values.items()})
            if one.get("account_key"):
                holders.setdefault(str(one["account_key"]), []).append(str(one["id"]))
    for key, ids in holders.items():
        virtual = VIRTUAL_PREFIX + key
        if virtual not in found and len(ids) == 1:
            found[virtual] = found[ids[0]]
    return found


def _template_problems(template):
    """What is wrong with the predefined query as written, before any value."""
    # Each reference stands for one literal, so checking the shape with a
    # literal in its place checks the shape the query will have.
    shaped = REFERENCE.sub('"x"', template)
    try:
        bracketed(shaped)
    except ScopeError as exc:
        return [str(exc)]
    quoted = [one for one in names(template)
              if re.search(r"[\"']\s*\{\{\s*%s\s*\}\}|\{\{\s*%s\s*\}\}\s*[\"']"
                           % (one, one), template)]
    if quoted:
        return ["Write %s without quotes around it: its value is quoted for you."
                % ", ".join("{{%s}}" % one for one in quoted)]
    return []


def missing(template, env_id, path):
    """What stops ``template`` resolving, as messages; empty when it resolves."""
    template = str(template or "").strip()
    if not template:
        return []
    found = _template_problems(template)
    if found:
        return found
    wanted = names(template)
    if not wanted:
        return []
    if not env_id:
        return ["The predefined query uses %s: choose the environment its variables "
                "come from." % ", ".join("{{%s}}" % one for one in wanted)]
    try:
        known = environments(path)
    except ScopeError as exc:
        return [str(exc)]
    if env_id not in known:
        return ["The environment the predefined query takes its variables from (%s) is "
                "not in this project. Choose another in Connections -> Settings." % env_id]
    name, values = known[env_id]
    absent = [one for one in wanted if one not in values]
    if absent:
        return ["Environment %s does not define %s, which the predefined query uses."
                % (name, ", ".join("{{%s}}" % one for one in absent))]
    for one in wanted:
        try:
            literal(values[one])
        except ScopeError as exc:
            return ["Variable %s of environment %s cannot be used: %s."
                    % (one, name, exc)]
    return []


def resolve(settings, path):
    """The predefined query of a connection's ``settings``, its variables put
    in as literals; "" when there is none. Raises ScopeError."""
    settings = settings or {}
    template = str(settings.get("predefined_query") or "").strip()
    if not template:
        return ""
    env_id = str(settings.get("variables_from") or "").strip()
    problems = missing(template, env_id, path)
    if problems:
        raise ScopeError(" ".join(problems))
    if names(template):
        values = environments(path)[env_id][1]
        template = REFERENCE.sub(lambda match: literal(values[match.group(1)]),
                                 template).strip()
    bracketed(template)                     # the values cannot change it; checked anyway
    return template


def project_keys(query):
    """The project keys a query narrows to, or None when it does not say.

    Best effort: ``project = KEY``, ``project = "KEY"`` and ``project in (...)``
    anywhere in it. Used only to shorten a list of projects to choose from -
    the query itself is what Jira is given, so a form this does not read makes
    the list longer, never the answer wrong.
    """
    keys = []
    for single, many in _PROJECT.findall(str(query or "")):
        for raw in ([single] if single else many.split(",")):
            raw = raw.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            if raw and raw.upper() not in keys:
                keys.append(raw.upper())
    return keys or None
