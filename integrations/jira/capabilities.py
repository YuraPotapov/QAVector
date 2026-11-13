"""Opt-in contracts for Jira's first four explicit reads.

Legacy GUI/Cycle operations retain their contracts and routing. Entries here
describe operations.call results, not a second HTTP client. Other operations
are deliberately not advertised until their contracts/policies are implemented.
"""

from integrations.contracts import OperationContract, Provider, ReadError
from . import operations, query


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
}


def read(name, connection, token, request, get=None):
    """Use the existing executor, adding scope membership for direct key reads.

    Search already intersects the scope. Issue/comments do not do so in the
    legacy GUI path; this explicit-read facade checks membership before fetching
    their content. A status list is site metadata, not a list of scoped issues.
    """
    if name not in CONTRACTS or CONTRACTS[name].access != "read":
        raise ReadError("unsupported")
    get = get or operations.session(connection, token)
    if name in ("issue", "comments") and connection.get("scope"):
        key = request["key"].upper()
        scoped_query = operations.jql({"text": key}, connection)
        rows, _ = query.page(get, connection["flavour"], scoped_query, 1, ("key",))
        if not any(isinstance(row, dict) and row.get("key", "").upper() == key for row in rows):
            raise ReadError("out_of_scope")
    return operations.call(name, connection, token, request, get=get)


def provider():
    return Provider("jira", CONTRACTS, operations.OPERATIONS, read)
