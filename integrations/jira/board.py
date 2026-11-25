"""Jira field discovery and single-field moves for the interactive Board."""

import json
import re
from functools import partial
from urllib.parse import quote

from integrations import scope
from . import operations, query, writes
from .fieldvalues import SCALAR_TYPES, TEXTAREA, scalar, wire_scalar
from .client import CLOUD, JiraError, api

FIELD_ID = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*\Z")
KINDS = {
    "status": "status", "priority": "option", "option": "option",
    "resolution": "option", "issuetype": "option", "securitylevel": "option",
    "project": "option", "version": "option", "component": "option",
    "user": "user", **SCALAR_TYPES,
}
READ_ONLY = {"project", "issuekey", "created", "updated", "resolutiondate", "lastViewed"}
DOCUMENTS = {"description", "environment"}
COMPLEX_CUSTOM = {TEXTAREA, "com.atlassian.jira.plugin.system.customfieldtypes:cascadingselect"}
FALLBACK_STATUS = {"id": "status", "name": "Status", "kind": "status", "editable": True}
OPTIONAL_ERRORS = {403, 404, 405}
STORY_POINTS_NAMES = ("story points", "story point estimate", "story point", "storypoints")
BLOCKER_LABELS = {"blocker", "blocked", "blocking", "impediment"}
AGILE_BOARDS = "/rest/agile/1.0/board"
CLOUD_RECONCILE_LIMIT = 50
# A Board spreads its cards over columns, so a page holds more than the list.
BOARD_ROWS = 100


def field_spec(field, data):
    schema = data.get("schema") or {}
    custom = schema.get("custom") or ""
    if (not FIELD_ID.fullmatch(field) or field in DOCUMENTS
            or custom in COMPLEX_CUSTOM):
        return None
    kind = KINDS.get(schema.get("type"))
    if kind is None:
        return None
    return {"id": field, "name": data.get("name") or field, "kind": kind,
            "editable": field not in READ_ONLY,
            "integer": schema.get("type") == "integer"}


def value_of(raw, spec, flavour):
    kind = spec["kind"]
    if isinstance(raw, dict):
        if kind == "user":
            value = operations.account_of(raw, flavour)
        elif kind in {"option", "status"}:
            value = str(raw.get("id") or "")
        else:
            return None
        label = raw.get("displayName") or raw.get("name") or raw.get("value") or value
        if not value:
            return None
        choice = {"value": value, "label": str(label)}
        category = operations._category(raw) if kind == "status" else ""
        if category:
            choice["category"] = category.lower()
        return choice
    if raw is None or raw == "":
        return {"value": None, "label": "No value"}
    if kind in {"text", "number", "date", "datetime", "boolean"}:
        try:
            raw = scalar(raw, spec)
        except ValueError:
            return None
        if isinstance(raw, bool):
            label = "Yes" if raw else "No"
        else:
            label = str(raw)
        return {"value": raw, "label": label}
    return None


def identity(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return json.dumps(value, ensure_ascii=False)


def _root(connection):
    return "/rest/api/%s" % api(connection["flavour"])


def _fields(get, connection):
    return [row for row in get(_root(connection) + "/field") or [] if isinstance(row, dict)]


def _catalog(fields):
    result = {item["id"]: item for row in fields
              for item in [field_spec(str(row.get("id") or ""), row)] if item}
    result.setdefault("status", FALLBACK_STATUS)
    return result


def _spec(catalog, field):
    if not isinstance(field, str) or field not in catalog:
        raise ValueError("This field is no longer available on the Board. Choose another field.")
    return catalog[field]


def _optional(get, path, default, params=None):
    try:
        return get(path, params)
    except JiraError as exc:
        if exc.status not in OPTIONAL_ERRORS:
            raise
        return default


def _layout_metadata(get, path, default, params=None):
    # Issues remain usable when the connection cannot read native board metadata.
    try:
        response = get(path, params)
    except JiraError:
        return default
    return response if isinstance(response, type(default)) else default


def _project_boards(get, projects):
    boards = {}
    for project in projects or ("",):
        offset = 0
        while True:
            params = {"startAt": offset, "maxResults": 50}
            if project:
                params["projectKeyOrId"] = project
            result = _layout_metadata(get, AGILE_BOARDS, {}, params)
            if not result:
                return []
            values = result.get("values") or []
            for item in values:
                board_id = str(item.get("id") or "")
                if board_id.isascii() and board_id.isdecimal():
                    boards[board_id] = {"id": board_id, "name": item.get("name") or board_id}
            offset += len(values)
            if not values or result.get("isLast", True):
                break
    return list(boards.values())


def boards(get, connection, request):
    within = connection.get("scope")
    scoped = scope.project_keys(within) or []
    projects = [key for key in scope.project_keys(request.get("jql")) or []
                if not scoped or key in scoped]
    result = _project_boards(get, projects or scoped)
    return {"boards": sorted(result, key=lambda board: board["name"].casefold())}


def _project_constraint(jql):
    if (re.match(r"^\s*\(*\s*project\s*(?:=|in\b)", jql, re.IGNORECASE)
            and not re.search(r"\b(?:or|not)\b", jql, re.IGNORECASE)):
        return scope.project_keys(jql) or []
    return []


def _status_order(get, request, found):
    board_id = str(request.get("board_id") or "")
    if not board_id:
        projects = set(_project_constraint(found["scope"]) or _project_constraint(found["jql"]))
        project_filter = str(request.get("project") or "") if not request.get("jql") else ""
        projects.update(key.strip().upper() for key in project_filter.split(",") if key.strip())
        if found["cursor"] is not None and not projects:
            return {}
        projects.update(row["project"].upper() for row in found["rows"] if row["project"])
        if len(projects) != 1:
            return {}
        candidates = _project_boards(get, tuple(projects))
        if len(candidates) != 1:
            return {}
        board_id = candidates[0]["id"]
    if not board_id.isascii() or not board_id.isdecimal():
        raise ValueError("Choose a Jira board.")
    config = _layout_metadata(get, "%s/%s/configuration" % (AGILE_BOARDS, board_id), {})
    columns = (config.get("columnConfig") or {}).get("columns") or []
    statuses = [str(status["id"]) for column in columns for status in column.get("statuses") or []
                if status.get("id")]
    if not statuses:
        return {}
    return {"source_board": str(config.get("name") or board_id), "status_order": list(dict.fromkeys(statuses))}


def _editmeta(get, connection, key):
    return (get("%s/issue/%s/editmeta" % (_root(connection), key)).get("fields") or {})


def _story_points(fields, story_fields, flavour):
    for spec in story_fields:
        value = value_of(fields.get(spec["id"]), spec, flavour)
        if value and value["value"] is not None:
            return value["value"]
    return None


def _board_row(raw, site, spec, flavour, story_fields, flag_fields):
    fields = raw.get("fields") or {}
    field = spec["id"]
    if field == "issuekey":
        value = value_of(raw.get("key"), spec, flavour)
    else:
        value = value_of(fields[field], spec, flavour) if field in fields else None
    labels = {label.casefold() for label in fields.get("labels") or [] if isinstance(label, str)}
    return dict(operations.row(raw, site), group=value,
                assignee_id=operations.account_of(fields.get("assignee"), flavour),
                blocked=bool(labels & BLOCKER_LABELS or any(fields.get(name) for name in flag_fields)),
                due_date=fields.get("duedate") or "",
                story_points=_story_points(fields, story_fields, flavour),
                issue_id=str(raw.get("id") or ""),
                issue_type_id=str((fields.get("issuetype") or {}).get("id") or ""))


def _choices(get, connection, spec, rows, jql, known, *, all_issue_types=False):
    kind, field = spec["kind"], spec["id"]
    root = _root(connection)
    values, contexts = [], []
    if kind == "status":
        projects = {}
        for row in rows:
            if row["project"]:
                projects.setdefault(row["project"], set()).add(row["issue_type_id"])
        within = connection.get("scope")
        scoped_projects = scope.project_keys(within) or ()
        for key in scope.project_keys(jql) or ():
            if within and key not in scoped_projects:
                continue
            projects.setdefault(key, {""})
        for key, types in projects.items():
            if all_issue_types:
                types = {""}
            unseen = [[key, kind_id] for kind_id in sorted(types) if [key, kind_id] not in known]
            if not unseen or [key, ""] in known:
                continue
            path = root + "/project/" + quote(key, safe="") + "/statuses"
            for entry in _optional(get, path, []):
                if [key, ""] in unseen or [key, str(entry.get("id") or "")] in unseen:
                    values.extend(entry.get("statuses") or [])
            contexts.extend(unseen)
    elif kind in {"option", "user"} and spec["editable"] and field != "assignee":
        for row in rows:
            context = [row["project"], row["issue_type_id"]]
            if context in known or context in contexts:
                continue
            meta = _optional(get, "%s/issue/%s/editmeta" % (root, row["key"]), {})
            data = (meta.get("fields") or {}).get(field) or {}
            # Another issue in this context may be editable under its workflow.
            if data:
                contexts.append(context)
                values.extend(data.get("allowedValues") or [])
    elif kind == "boolean":
        values = [False, True]
    choices = [value_of(raw, spec, connection["flavour"]) for raw in values]
    return [item for item in choices if item and item["value"] is not None], contexts


def _configured_statuses(get, connection, spec, choices, status_order):
    missing = set(status_order) - {item["value"] for item in choices}
    if not missing:
        return
    for raw in _layout_metadata(get, _root(connection) + "/status", []):
        if isinstance(raw, dict) and str(raw.get("id") or "") in missing:
            choice = value_of(raw, spec, connection["flavour"])
            if choice:
                choices.append(choice)


def _search(get, connection, request, *, reconcile=(), size_limit=operations.MOST_ROWS):
    definitions = _fields(get, connection)
    catalog = _catalog(definitions)
    field = request.get("field") or "status"
    spec = catalog.get(field, FALLBACK_STATUS) if isinstance(field, str) else FALLBACK_STATUS
    if spec["id"] != field:
        request = dict(request, cursor=None, known_contexts=[])
    story_fields = [item for name in STORY_POINTS_NAMES for item in catalog.values()
                    if item["kind"] == "number" and " ".join(item["name"].casefold().split()) == name]
    flag_fields = []
    for definition in definitions:
        schema = definition.get("schema") or {}
        name = str(definition.get("name") or "").casefold()
        flagged = (schema.get("custom") == "com.pyxis.greenhopper.jira:gh-flagged"
                   or (schema.get("type") == "array" and name == "flagged"))
        field_id = str(definition.get("id") or "")
        if flagged and FIELD_ID.fullmatch(field_id):
            flag_fields.append(field_id)
    fields = (*operations.LIST_FIELDS, spec["id"], "duedate", "labels",
              *(item["id"] for item in story_fields), *flag_fields)
    found = operations.search(
        get, connection, request, fields=tuple(dict.fromkeys(fields)),
        project_row=lambda raw, site: _board_row(raw, site, spec, connection["flavour"], story_fields, flag_fields),
        reconcile=reconcile, size_limit=size_limit,
    )
    order = _status_order(get, request, found) if spec["kind"] == "status" and not request.get("cursor") else {}
    choices, contexts = _choices(get, connection, spec, found["rows"], found["jql"],
                                 request.get("known_contexts") or [],
                                 all_issue_types=bool(order.get("status_order")))
    if order:
        _configured_statuses(get, connection, spec, choices, order["status_order"])
    return dict(found, fields=sorted(catalog.values(), key=lambda item: item["name"].casefold()),
                field=spec["id"], choices=choices, contexts=contexts, **order)


def _key_filter(key):
    return 'key = "%s"' % query.escape(key)


def _inside_scope(get, connection, key):
    within = connection.get("scope")
    if within:
        rows, _cursor = query.page(get, connection["flavour"],
                                   scope.anded(within, _key_filter(key)), 1, ("summary",))
        if not rows:
            raise writes.Refused("%s is outside this connection's scope." % key)


def open_issue(get, connection, request):
    _inside_scope(get, connection, operations._key(request))
    return operations.issue(get, connection, request)


def search(get, connection, request):
    return _search(get, connection, dict(request, size=request.get("size") or BOARD_ROWS))


def reload(get, connection, request):
    """Re-read the loaded window so ordering and pagination survive a move."""
    ids = request.get("issue_ids")
    if not isinstance(ids, list):
        ids = [request.get("issue_id")]
    reconcile = []
    for item in ids:
        issue_id = str(item or "")
        if issue_id.isascii() and issue_id.isdecimal() and len(issue_id) <= 20:
            reconcile.append(int(issue_id))
    reconcile = tuple(dict.fromkeys(reconcile))
    size = max(BOARD_ROWS, int(request.get("size") or 0))
    request = dict(request, cursor=None)
    if connection["flavour"] != CLOUD or len(reconcile) <= CLOUD_RECONCILE_LIMIT:
        return _search(get, connection, request, reconcile=reconcile, size_limit=size)

    # An enhanced search can reconcile only 50 issue IDs. Each read
    # is authoritative for its own chunk, even when another chunk is stale.
    verified_rows = []
    for start in range(0, len(reconcile), CLOUD_RECONCILE_LIMIT):
        chunk = reconcile[start:start + CLOUD_RECONCILE_LIMIT]
        result = _search(get, connection, request, reconcile=chunk, size_limit=size)
        keys = {str(issue_id) for issue_id in chunk}
        verified_rows.extend((index, row) for index, row in enumerate(result["rows"])
                             if row["issue_id"] in keys)

    requested = {str(issue_id) for issue_id in reconcile}
    rows = [row for row in result["rows"] if row["issue_id"] not in requested]
    for position, row in sorted(verified_rows, key=lambda item: item[0]):
        rows.insert(position, row)
    return dict(result, rows=rows)


def people(get, connection, request):
    field = request.get("field")
    spec = _spec(_catalog(_fields(get, connection)), field)
    if spec["kind"] != "user" or not spec["editable"]:
        raise ValueError("Choose a user field.")
    key = operations._key(request)
    _inside_scope(get, connection, key)
    if field == "assignee":
        found = writes.assignable(get, connection, request)["people"]
        return {"choices": [{"value": item["id"], "label": item["name"]}
                            for item in found if item["id"]]}
    param = "query" if connection["flavour"] == CLOUD else "username"
    found = get(_root(connection) + "/user/search",
                {param: str(request.get("query") or ""), "maxResults": 50})
    choices = [value_of(item, spec, connection["flavour"])
               for item in found or [] if isinstance(item, dict) and item.get("active", True)]
    return {"choices": [item for item in choices if item]}


def _status_move(get, connection, request, key, target):
    path = "%s/issue/%s/transitions" % (_root(connection), key)
    offered = get(path, {"expand": "transitions.fields"}).get("transitions") or []
    choices = [{"id": str(item["id"]), "name": item.get("name") or str(item["id"])}
               for item in offered
               if item.get("id") and str((item.get("to") or {}).get("id") or "") == target]
    if not choices:
        raise writes.Refused("%s has no transition to that status." % key)
    selected = str(request.get("transition") or "")
    if not selected and len(choices) > 1:
        raise writes.Refused("Choose a transition.", "transitions", choices=choices)
    selected = selected or choices[0]["id"]
    if selected not in {item["id"] for item in choices}:
        raise writes.Refused("That transition is no longer available.")
    try:
        return writes.transition(get, connection,
                                 {"key": key, "to": selected, "fields": request.get("fields")},
                                 offered=offered, read_back=False)
    except writes.Refused as exc:
        if exc.kind == "fields":
            exc.details["transition_id"] = selected
        raise


def _set_value(get, connection, request, key, spec):
    field, target = spec["id"], request["value"]
    path = "%s/issue/%s" % (_root(connection), key)
    meta = _editmeta(get, connection, key).get(field) or {}
    operations_allowed = meta.get("operations")
    if not meta or (operations_allowed is not None and "set" not in operations_allowed):
        raise writes.Refused("Jira does not allow editing %s on %s." % (spec["name"], key))
    allowed = meta.get("allowedValues")
    if target is None:
        if meta.get("required"):
            raise writes.Refused("%s is required on %s." % (spec["name"], key))
        value = None
    elif spec["kind"] in {"option", "user"}:
        if not isinstance(target, str) or not target:
            raise ValueError("Choose a Jira value.")
        choices = [value_of(item, spec, connection["flavour"]) for item in allowed or []]
        if allowed is not None and not any(item and item["value"] == target for item in choices):
            raise writes.Refused("This value is unavailable for %s on %s." % (spec["name"], key))
        value = {"id": target}
        if spec["kind"] == "user":
            value = {"accountId" if connection["flavour"] == CLOUD else "name": target}
    else:
        value = wire_scalar(target, spec, connection["flavour"])
    get(path, body={"fields": {field: value}}, method="PUT")


def _write_result(get, connection, key, summary):
    result = {"summary": summary}
    try:
        result.update(writes._after(get, connection, key))
    except JiraError:
        # The Board reload reads this issue even if detail or comments are unavailable.
        pass
    return result


def _track_write(get, key, path, params=None, body=None, method=None):
    try:
        return get(path, params=params, body=body, method=method)
    except JiraError as exc:
        status = exc.status
        ambiguous = not status or status == 408 or 500 <= status < 600
        if (body is not None or method == "PUT") and ambiguous:
            raise writes.Refused("Jira did not confirm the change for %s." % key, "unknown_outcome") from None
        raise


def move(get, connection, request):
    key = operations._key(request)
    field = request.get("field")
    spec = _spec(_catalog(_fields(get, connection)), field)
    if not spec["editable"] or "value" not in request or "from" not in request:
        raise writes.Refused("This field cannot be changed on the Board.")
    _inside_scope(get, connection, key)
    path = "%s/issue/%s" % (_root(connection), key)
    current = (get(path, {"fields": ",".join(dict.fromkeys((field, "updated")))})
               .get("fields") or {})
    current_value = value_of(current[field], spec, connection["flavour"]) if field in current else None
    target = request["value"]
    if request.get("confirm_only"):
        if current_value and identity(target) == identity(current_value["value"]):
            return _write_result(get, connection, key, "%s: target value confirmed" % key)
        raise writes.Refused("The change for %s could not be confirmed." % key, "unknown_outcome")
    if (not current_value or identity(current_value["value"]) != identity(request["from"])
            or (request.get("updated") and request["updated"] != current.get("updated"))):
        raise writes.Refused("%s changed in Jira. Its Board card will be refreshed." % key, "conflict")
    if identity(target) == identity(current_value["value"]):
        raise writes.Refused("The issue is already in that column.")
    summary = "%s: %s" % (key, spec["name"])
    tracked = partial(_track_write, get, key)
    if field == "status":
        summary = _status_move(tracked, connection, request, key, target)["summary"]
    elif field == "assignee":
        if target is not None and (not isinstance(target, str) or not target or target == "me"):
            raise ValueError("Choose a Jira account.")
        writes._assign(tracked, connection, key, target)
    else:
        _set_value(tracked, connection, request, key, spec)
    return _write_result(get, connection, key, summary)


OPERATIONS = {"board_search": search, "board_reload": reload, "board_people": people,
              "board_move": move, "board_issue": open_issue, "board_catalog": boards}
