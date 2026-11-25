import pytest

from fake_jira import FakeJira
from integrations.jira import board, operations, writes
from integrations.jira.fieldvalues import wire_scalar

STATUS = {"id": "status", "name": "Status", "schema": {"type": "status"}}
CHOICE = {"id": "customfield_17", "name": "Track", "schema": {"type": "option"}}
NUMBER = {"id": "customfield_18", "name": "Estimate", "schema": {"type": "number"}}
LABELS = {"id": "labels", "name": "Labels", "schema": {"type": "array", "items": "string"}}
ASSIGNEE = {"id": "assignee", "name": "Assignee", "schema": {"type": "user"}}


@pytest.fixture
def serve():
    servers = []

    def make(answers):
        jira = FakeJira(answers)
        servers.append(jira)
        return jira

    yield make
    for jira in servers:
        jira.close()


def call(jira, operation, document, *, flavour="cloud", scope=""):
    connection = {"site": jira.site, "flavour": flavour, "auth": "basic", "scope": scope}
    return operations.call(operation, connection, "test-token", document)


def issue(project="QA", **fields):
    return {"id": "123", "key": project + "-1", "fields": {
        "summary": "Card", "project": {"key": project}, "issuetype": {"id": "100"},
        "status": {"id": "1", "name": "To Do"}, "updated": "seen", **fields}}


def test_cloud_board_reuses_scoped_search_and_keeps_contextual_choices_distinct(serve):
    def search(request):
        assert request.body["jql"] == '(project in (QA, WEB)) AND (status = Open) ORDER BY key'
        assert "customfield_17" in request.body["fields"]
        if request.body.get("nextPageToken") == "next":
            return {"issues": [issue("WEB", customfield_17=None)], "isLast": True}
        return {"issues": [issue(customfield_17={"id": "7", "value": "Core"})],
                "nextPageToken": "next"}

    jira = serve({"/rest/api/3/field": [STATUS, CHOICE, LABELS],
                  "POST /rest/api/3/search/jql": search,
                  "/rest/api/3/issue/QA-1/editmeta": {"fields": {"customfield_17": {
                      "allowedValues": [{"id": "7", "value": "Core"}, {"id": "8", "value": "UI"}]}}},
                  "/rest/api/3/issue/WEB-1/editmeta": {"fields": {"customfield_17": {
                      "allowedValues": [{"id": "9", "value": "Core"}]}}}})
    request = {"jql": "status = Open ORDER BY key", "field": "customfield_17", "size": 1}
    first = call(jira, "board_search", request, scope="project in (QA, WEB)")
    assert [field["id"] for field in first["fields"]] == ["status", "customfield_17"]
    assert first["rows"][0]["group"] == {"value": "7", "label": "Core"}
    assert first["choices"][-1] == {"value": "8", "label": "UI"}
    second = call(jira, "board_search", dict(request, cursor=first["cursor"],
                                            known_contexts=first["contexts"]),
                  scope="project in (QA, WEB)")
    assert second["rows"][0]["group"]["value"] is None
    assert second["choices"] == [{"value": "9", "label": "Core"}]
    assert second["cursor"] is None


def test_server_board_status_options_and_pagination(serve):
    jira = serve({"/rest/api/2/field": [STATUS],
                  "/rest/api/2/search": {"issues": [issue()], "total": 5},
                  "/rest/api/2/project/QA/statuses": [
                      {"id": "100", "statuses": [{"id": "1", "name": "To Do"},
                                                 {"id": "4", "name": "Done",
                                                  "statusCategory": {"key": "done"}}]},
                      {"id": "101", "statuses": [{"id": "5", "name": "Cancelled"}]}]})
    result = call(jira, "board_search", {"size": 1, "cursor": 2}, flavour="server")
    assert "startAt=2" in jira.seen[1].path
    assert result["cursor"] == 3
    assert result["choices"][-1] == {"value": "4", "label": "Done", "category": "done"}
    assert len(result["choices"]) == 2


def test_native_board_supplies_status_order_even_for_empty_statuses(serve):
    initial = [{"id": "1", "name": "To Do"}, {"id": "9", "name": "On Prod"}]
    other_type = [{"id": "5", "name": "Draft"}, {"id": "6", "name": "Reopen"}]
    configured = [{"statuses": [{"id": status}]} for status in ("5", "1", "6", "9", "10")]
    jira = serve({"/rest/api/3/field": [STATUS],
                  "POST /rest/api/3/search/jql": {"issues": [issue()]},
                  "/rest/api/3/project/QA/statuses": [{"id": "100", "statuses": initial},
                                                       {"id": "101", "statuses": other_type}],
                  "/rest/api/3/status": [{"id": "10", "name": "Done"}, {"id": "99", "name": "Other"}],
                  "/rest/agile/1.0/board?": {"values": [{"id": 46, "name": "QA board"}], "isLast": True},
                  "/rest/agile/1.0/board/46/configuration": {
                      "name": "QA board", "columnConfig": {"columns": configured}}})
    first = call(jira, "board_search", {"project": "QA"})
    assert first["source_board"] == "QA board"
    assert first["status_order"] == ["5", "1", "6", "9", "10"]
    assert {item["label"] for item in first["choices"]} == {"To Do", "On Prod", "Draft", "Reopen", "Done"}
    catalog = call(jira, "board_catalog", {}, scope="project = QA")
    assert catalog["boards"] == [{"id": "46", "name": "QA board"}]
    lists = [item.path for item in jira.seen if item.path.startswith("/rest/agile/1.0/board?")]
    assert len(lists) == 2 and all("projectKeyOrId=QA" in path for path in lists)
    calls = len(jira.seen)
    second = call(jira, "board_search", {"board_id": "46"})
    assert second["status_order"] == first["status_order"]
    assert not any(item.path.startswith("/rest/agile/1.0/board?") for item in jira.seen[calls:])


def test_multiple_jira_boards_do_not_supply_an_arbitrary_order(serve):
    jira = serve({"/rest/api/3/field": [STATUS],
                  "POST /rest/api/3/search/jql": {"issues": [issue()]},
                  "/rest/agile/1.0/board?": {"values": [{"id": 46, "name": "First"},
                                                        {"id": 47, "name": "Second"}], "isLast": True},
                  "/rest/agile/1.0/board/47/configuration": (403, {})})
    assert "status_order" not in call(jira, "board_search", {"project": "QA"})
    assert not any("/configuration" in item.path for item in jira.seen)
    selected = call(jira, "board_search", {"board_id": "47"})
    assert selected["rows"] and "status_order" not in selected


def test_unrestricted_first_page_does_not_choose_a_board_for_other_projects(serve):
    def search(request):
        if request.body.get("nextPageToken") == "next":
            return {"issues": [issue("WEB")]}
        return {"issues": [issue()], "nextPageToken": "next"}

    jira = serve({"/rest/api/3/field": [STATUS],
                  "POST /rest/api/3/search/jql": search,
                  "/rest/agile/1.0/board?": {"values": [{"id": 46, "name": "QA board"}], "isLast": True}})
    first = call(jira, "board_search", {"size": 1})
    assert first["cursor"] == "next"
    assert "status_order" not in first
    second = call(jira, "board_search", {"size": 1, "cursor": first["cursor"]})
    assert second["rows"][0]["project"] == "WEB"
    assert "status_order" not in second
    assert not any(item.path.startswith("/rest/agile/") for item in jira.seen)


def test_a_board_page_holds_a_hundred_issues_unless_a_size_is_asked(serve):
    def search(request):
        start = int(request.body.get("nextPageToken") or 0)
        rows = [dict(issue(), id=str(number), key="QA-%d" % number)
                for number in range(start, start + request.body["maxResults"])]
        return {"issues": rows, "nextPageToken": str(start + len(rows))}

    jira = serve({"/rest/api/3/field": [STATUS], "POST /rest/api/3/search/jql": search})
    result = call(jira, "board_search", {})
    assert len(result["rows"]) == board.BOARD_ROWS == 100
    assert result["cursor"] == "100"
    assert len(call(jira, "board_search", {"size": 3})["rows"]) == 3


def test_board_search_requests_card_fields_and_finds_story_point_fields_by_name(serve):
    fields = [
        STATUS,
        {"id": "customfield_10016", "name": "Story Points", "schema": {"type": "number"}},
        {"id": "customfield_10018", "name": "Story point estimate", "schema": {"type": "number"}},
        {"id": "customfield_10019", "name": "Flagged", "schema": {"type": "array"}},
    ]

    def search(request):
        assert {"duedate", "assignee", "priority", "labels", "customfield_10016",
                "customfield_10018", "customfield_10019"} <= set(request.body["fields"])
        return {"issues": [
            issue(duedate="2026-11-25", customfield_10016=0, customfield_10018=3,
                  assignee={"displayName": "Olena", "accountId": "olena-123"},
                  priority={"name": "High"}, labels=["BLOCKER"]),
            issue("WEB", customfield_10018=2.5, customfield_10019=[{"value": "Impediment"}]),
            issue("APP", labels=["integration"], customfield_10019=[]),
        ]}

    jira = serve({"/rest/api/3/field": fields,
                  "POST /rest/api/3/search/jql": search})
    rows = call(jira, "board_search", {})["rows"]
    assert rows[0]["assignee"] == "Olena"
    assert rows[0]["assignee_id"] == "olena-123"
    assert rows[0]["priority"] == "High"
    assert rows[0]["due_date"] == "2026-11-25"
    assert [row["story_points"] for row in rows] == [0, 2.5, None]
    assert [row["blocked"] for row in rows] == [True, True, False]


def test_status_move_chooses_a_transition_and_requests_its_fields(serve):
    choices = [
        {"id": "10", "to": {"id": "4"}, "name": "Finish", "fields": {
            "resolution": {"required": True, "allowedValues": [{"id": "1", "name": "Fixed"}]},
            "customfield_20": {"required": True, "name": "Reason", "schema": {"type": "string"}},
            "duedate": {"required": True, "name": "Due date", "schema": {"type": "date"}},
            "components": {"required": True, "schema": {"type": "array", "items": "component"},
                           "allowedValues": [{"id": "15", "name": "API"}, {"id": "16", "name": "UI"}]}}},
        {"id": "11", "to": {"id": "4"}, "name": "Complete"},
        {"id": "12", "to": {"id": "5"}, "name": "Cancel"},
    ]
    jira = serve({"/rest/api/3/field": [STATUS],
                  "/rest/api/3/issue/QA-1": issue(),
                  "/rest/api/3/issue/QA-1/comment": {},
                  "GET /rest/api/3/issue/QA-1/transitions": {"transitions": choices},
                  "POST /rest/api/3/issue/QA-1/transitions": (204, None)})
    request = {"key": "QA-1", "field": "status", "from": "1", "value": "4", "updated": "seen"}
    with pytest.raises(writes.Refused) as select:
        call(jira, "board_move", request)
    assert select.value.kind == "transitions"
    assert [item["id"] for item in select.value.details["choices"]] == ["10", "11"]
    with pytest.raises(writes.Refused) as required:
        call(jira, "board_move", dict(request, transition="10"))
    assert required.value.kind == "fields"
    assert required.value.details["transition_id"] == "10"
    needs = required.value.details["needs"]
    assert [item.get("kind") for item in needs] == [None, "text", "date", None]
    assert needs[-1]["multiple"] is True
    assert all(item.method == "GET" for item in jira.seen)
    fields = {"resolution": "1", "customfield_20": "Verified", "duedate": "2026-11-28",
              "components": ["15", "16"]}
    assert call(jira, "board_move", dict(request, transition="10", fields=fields))["summary"] == "QA-1: Finish"
    posted = next(item for item in jira.seen if item.method == "POST")
    assert posted.body == {"transition": {"id": "10"}, "fields": dict(
        fields, resolution={"id": "1"}, components=[{"id": "15"}, {"id": "16"}])}


@pytest.mark.parametrize("target", [13.5, None])
def test_board_writes_typed_scalar_or_clears_optional_field_and_detects_conflict(serve, target):
    jira = serve({"/rest/api/3/field": [NUMBER],
                  "/rest/api/3/issue/QA-1": issue(customfield_18=5),
                  "/rest/api/3/issue/QA-1/editmeta": {"fields": {
                      "customfield_18": {"required": False, "operations": ["set"]}}},
                  "/rest/api/3/issue/QA-1/comment": (403, {}),
                  "PUT /rest/api/3/issue/QA-1": (204, None)})
    request = {"key": "QA-1", "field": "customfield_18", "value": target, "from": 5}
    with pytest.raises(writes.Refused) as changed:
        call(jira, "board_move", dict(request, updated="old"))
    assert changed.value.kind == "conflict"
    assert all(item.method != "PUT" for item in jira.seen)
    assert call(jira, "board_move", dict(request, updated="seen"))["summary"] == "QA-1: Estimate"
    put = next(item for item in jira.seen if item.method == "PUT")
    assert put.body == {"fields": {"customfield_18": target}}


@pytest.mark.parametrize("applied,status", [(True, 524), (False, 503)])
def test_lost_transition_response_can_only_be_confirmed_by_reads(serve, applied, status):
    state = {"status": "1"}

    def write(_request):
        if applied:
            state["status"] = "4"
        return status, {}

    def current(_request):
        return issue(status={"id": state["status"], "name": "Done" if applied else "To Do"})

    jira = serve({"/rest/api/3/field": [STATUS],
                  "/rest/api/3/issue/QA-1": current,
                  "/rest/api/3/issue/QA-1/comment": {},
                  "GET /rest/api/3/issue/QA-1/transitions": {"transitions": [
                      {"id": "10", "to": {"id": "4"}, "name": "Finish"}]},
                  "POST /rest/api/3/issue/QA-1/transitions": write})
    request = {"key": "QA-1", "field": "status", "from": "1", "value": "4", "updated": "seen"}
    with pytest.raises(writes.Refused) as failure:
        call(jira, "board_move", request)
    assert failure.value.kind == "unknown_outcome"
    checking = dict(request, confirm_only=True)
    if applied:
        assert call(jira, "board_move", checking)["issue"]["status"] == "Done"
    else:
        for _ in range(2):
            with pytest.raises(writes.Refused) as probe:
                call(jira, "board_move", checking)
            assert probe.value.kind == "unknown_outcome"
    assert len(jira.paths("POST")) == 1


@pytest.mark.parametrize("flavour, version, account_field", [("cloud", 3, "accountId"), ("server", 2, "name")])
def test_board_uses_dedicated_assignment_endpoint(serve, flavour, version, account_field):
    root = f"/rest/api/{version}"
    jira = serve({root + "/field": [ASSIGNEE],
                  root + "/issue/QA-1": issue(assignee={account_field: "old", "displayName": "User"}),
                  root + "/issue/QA-1/comment": {},
                  "PUT " + root + "/issue/QA-1/assignee": (204, None)})
    call(jira, "board_move", {"key": "QA-1", "field": "assignee", "value": "new", "from": "old"},
         flavour=flavour)
    assert next(item for item in jira.seen if item.method == "PUT").body == {account_field: "new"}


def test_after_drop_cloud_reconciles_the_visible_query_without_a_mutation(serve):
    jira = serve({"/rest/api/3/field": [STATUS],
                  "POST /rest/api/3/search/jql": {"issues": [], "isLast": True},
                  "/rest/api/3/myself": {"accountId": "x"}})
    result = call(jira, "board_reload", {"field": "status", "size": 25,
                                        "issue_id": "123", "jql": "status = Open"})
    assert result["rows"] == []
    assert jira.seen[1].body["reconcileIssues"] == [123]
    assert jira.seen[1].body["jql"] == "status = Open ORDER BY updated DESC"


def test_cloud_reload_reconciles_all_batch_ids_without_using_the_stale_other_chunk(serve):
    def search(request):
        verified = request.body.get("reconcileIssues") or []
        rows = []
        for index in (1, 51):
            status = {"id": "4", "name": "Done"} if index in verified else {"id": "1", "name": "To Do"}
            rows.append(dict(issue(status=status), id=str(index), key="QA-%d" % index))
        return {"issues": rows, "isLast": True}

    jira = serve({"/rest/api/3/field": [STATUS], "POST /rest/api/3/search/jql": search})
    result = call(jira, "board_reload", {"field": "status", "issue_ids": list(range(1, 52))})
    assert [row["group"]["label"] for row in result["rows"]] == ["Done", "Done"]
    batches = [request.body["reconcileIssues"] for request in jira.seen
               if request.path == "/rest/api/3/search/jql"]
    assert batches == [list(range(1, 51)), [51]]


def test_board_open_and_project_metadata_follow_current_connection_scope(serve):
    jira = serve({"/rest/api/3/field": [STATUS],
                  "POST /rest/api/3/search/jql": {"issues": [], "isLast": True},
                  "/rest/api/3/myself": {"accountId": "x"},
                  "/rest/api/3/project/QA/statuses": []})
    result = call(jira, "board_search", {"project": "WEB"}, scope="project = QA")
    assert result["rows"] == []
    assert not any("/project/WEB/" in item.path for item in jira.seen)
    with pytest.raises(writes.Refused, match="outside this connection"):
        call(jira, "board_issue", {"key": "WEB-1"}, scope="project = QA")
    assert not any("/issue/WEB-1" in item.path for item in jira.seen)


def test_jira_datetime_grouping_normalizes_jira_offsets():
    spec = {"id": "updated", "name": "Updated", "kind": "datetime"}
    raw = "2026-11-05T12:30:00.123+0200"
    value = board.value_of(raw, spec, "cloud")["value"]
    assert value == "2026-11-05T10:30:00.123000+00:00"
    assert wire_scalar(value, spec, "cloud") == "2026-11-05T10:30:00.123+0000"
