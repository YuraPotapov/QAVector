"""What the Integrations page asks Jira, and the command it asks through.

Every request here is one a person made, so the tests check that each answers
with the fewest requests it can: the list asks for the fields a row shows, an
opened issue is read alone with its newest comments, and nothing is fetched
that is not on screen.
"""

import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations import cli, store                                  # noqa: E402
from integrations.jira import operations                             # noqa: E402
from integrations.jira.client import JiraError, Session              # noqa: E402
from fake_jira import FakeJira                                       # noqa: E402

TOKEN = "tok-55e1-not-a-real-one"
EMAIL = "you@yourcompany.example"

ADF = {"type": "doc", "version": 1, "content": [
    {"type": "paragraph", "content": [{"type": "text", "text": "Steps <b>here</b>"}]}]}
RICH = {"type": "doc", "version": 1, "content": [
    {"type": "bulletList", "content": [{"type": "listItem", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]}]}]}


def _raw(key, summary="Session expires", status="In Progress", category="indeterminate"):
    return {"key": key, "fields": {
        "summary": summary, "updated": "2026-11-05T10:00:00.000+0000",
        "status": {"name": status, "statusCategory": {"key": category}},
        "issuetype": {"name": "Bug"}, "priority": {"name": "High"},
        "assignee": {"displayName": "Tester T", "emailAddress": EMAIL}}}


@pytest.fixture
def jira():
    made = []

    def serve(**answers):
        one = FakeJira(answers)
        made.append(one)
        return one

    yield serve
    for one in made:
        one.close()


def _connection(site, flavour="cloud", **settings):
    return {"id": "product-team", "provider": "jira", "name": "Product team",
            "site": site, "flavour": flavour, "auth": "basic", "email": EMAIL,
            "verify_tls": True, "settings": dict({"default_project": ""}, **settings)}


def _call(name, server, request=None, flavour="cloud", **settings):
    return operations.call(name, _connection(server.site, flavour, **settings), TOKEN,
                           request or {})


# -- the list -----------------------------------------------------------------
@pytest.mark.parametrize("request_, expected", [
    ({}, "updated >= -90d ORDER BY updated DESC"),
    ({"project": "QA", "status": "To Do, In Progress", "mine": True},
     'project = "QA" AND status in ("To Do", "In Progress") AND '
     "assignee = currentUser() ORDER BY updated DESC"),
    ({"text": "qa-142"}, 'key = "QA-142" ORDER BY updated DESC'),
    ({"text": 'say "hi"'}, 'text ~ "say \\"hi\\"" ORDER BY updated DESC'),
    ({"order": "updated ASC", "project": "WEB"}, 'project = "WEB" ORDER BY updated ASC'),
    ({"jql": "labels = staging", "project": "QA"}, "labels = staging ORDER BY updated DESC"),
    ({"jql": "labels = x order by key"}, "labels = x order by key"),
])
def test_the_jql_a_list_request_stands_for(request_, expected):
    assert operations.jql(request_) == expected


def test_the_default_project_narrows_a_list_nobody_filtered():
    connection = {"settings": {"default_project": "QA"}}
    assert operations.jql({}, connection) == 'project = "QA" ORDER BY updated DESC'


def test_a_page_asks_for_the_row_s_fields_only_and_gives_a_cursor(jira):
    server = jira(**{"/rest/api/3/search/jql": {
        "issues": [_raw("QA-142"), _raw("QA-139", status="To Do", category="new")],
        "nextPageToken": "next-1"}})
    answer = _call("search", server, {"size": 2})
    body = server.seen[0].body
    assert body["fields"] == list(operations.LIST_FIELDS)
    assert body["maxResults"] == 2
    assert answer["cursor"] == "next-1"
    assert answer["rows"][0] == {
        "key": "QA-142", "summary": "Session expires", "type": "Bug", "priority": "High",
        "status": "In Progress", "category": "indeterminate", "assignee": "Tester T",
        "updated": "2026-11-05T10:00:00.000+0000",
        "url": server.site + "/browse/QA-142"}
    assert EMAIL not in json.dumps(answer)
    assert len(server.seen) == 1, "one page is one request"


def test_load_more_hands_the_cursor_back(jira):
    server = jira(**{"/rest/api/3/search/jql": {"issues": [_raw("QA-1")], "isLast": True}})
    answer = _call("search", server, {"cursor": "next-1"})
    assert server.seen[0].body["nextPageToken"] == "next-1"
    assert answer["cursor"] is None


def test_refresh_asks_for_as_many_rows_as_are_shown_and_no_more(jira):
    pages = iter([{"issues": [_raw("QA-%d" % n) for n in range(50)], "nextPageToken": "t"},
                  {"issues": [_raw("QA-%d" % n) for n in range(50, 60)], "nextPageToken": "u"}])
    server = jira(**{"/rest/api/3/search/jql": lambda request: next(pages)})
    answer = _call("search", server, {"size": 60})
    assert [one.body["maxResults"] for one in server.seen] == [50, 10]
    assert len(answer["rows"]) == 60 and answer["cursor"] == "u"


def test_server_pages_by_offset(jira):
    server = jira(**{"/rest/api/2/search": {
        "issues": [_raw("QA-%d" % n) for n in range(25)], "total": 40}})
    answer = _call("search", server, {"size": 25}, flavour="server")
    assert "startAt=0" in server.seen[0].path and "maxResults=25" in server.seen[0].path
    assert answer["cursor"] == 25 and len(server.seen) == 1


def test_a_typed_key_no_issue_has_is_no_rows_not_an_error(jira):
    server = jira(**{"/rest/api/3/search/jql": (400, {"errorMessages": [
        "An issue with key 'QA-9999' does not exist for field 'key'."]})})
    assert _call("search", server, {"text": "QA-9999"})["rows"] == []


def test_a_wrong_jql_is_an_error_with_jira_s_words(jira):
    server = jira(**{"/rest/api/3/search/jql": (400, {"errorMessages": [
        "Field 'colour' does not exist or you do not have permission to view it."]})})
    with pytest.raises(JiraError) as caught:
        _call("search", server, {"jql": "colour = red"})
    assert caught.value.status == 400 and "colour" in str(caught.value)


# -- one issue ----------------------------------------------------------------
def _issue_answers(description=ADF):
    raw = _raw("QA-142")
    raw["fields"].update({
        "description": description, "labels": ["regression", "staging"],
        "reporter": {"displayName": "Reporter R"}, "created": "2026-11-01T09:00:00.000+0000",
        "project": {"key": "QA", "name": "Quality"},
        "attachment": [{"id": "10", "filename": "empty-state.png", "mimeType": "image/png",
                        "size": 2048, "content": "/secure/attachment/10/x.png",
                        "author": {"displayName": "Tester T"}}]})
    raw["transitions"] = [{"id": "31", "name": "Done",
                           "to": {"name": "Done", "statusCategory": {"key": "done"}}}]
    return {"/rest/api/3/issue/QA-142?": raw,
            "/rest/api/3/issue/QA-142/comment": {"total": 31, "comments": [
                {"id": "7", "author": {"displayName": "Olena O"},
                 "created": "2026-11-05T09:00:00.000+0000", "body": ADF}]}}


def test_an_opened_issue_is_one_read_and_its_newest_comments(jira):
    server = jira(**_issue_answers())
    detail = _call("issue", server, {"key": "qa-142"})["issue"]
    assert server.paths() == [
        "/rest/api/3/issue/QA-142?fields=" + "%2C".join(operations.DETAIL_FIELDS)
        + "&expand=transitions",
        "/rest/api/3/issue/QA-142/comment?startAt=0&maxResults=20&orderBy=-created"]
    assert detail["labels"] == ["regression", "staging"]
    assert detail["project"] == {"key": "QA", "name": "Quality"}
    assert detail["transitions"] == [{"id": "31", "name": "Done", "to": "Done",
                                      "category": "done"}]
    assert detail["attachments"][0]["filename"] == "empty-state.png"
    assert detail["comments"]["total"] == 31 and detail["comments"]["items"][0]["author"] == "Olena O"
    # Escaped: a description is somebody else's text, not markup to trust.
    assert "&lt;b&gt;here&lt;/b&gt;" in detail["description_html"]
    assert detail["description_text"] == "Steps <b>here</b>"
    assert detail["description_plain"] is True
    assert EMAIL not in json.dumps(detail)


def test_a_description_with_a_list_is_marked_as_more_than_text(jira):
    server = jira(**_issue_answers(RICH))
    assert _call("issue", server, {"key": "QA-142"})["issue"]["description_plain"] is False


def test_earlier_comments_start_where_the_shown_ones_end(jira):
    server = jira(**{"/rest/api/3/issue/QA-1/comment": {"total": 31, "comments": []}})
    _call("comments", server, {"key": "QA-1", "start": 20})
    assert "startAt=20" in server.seen[0].path


def test_a_key_is_checked_before_jira_is_asked(jira):
    server = jira()
    with pytest.raises(ValueError):
        _call("issue", server, {"key": "../../etc"})
    assert server.seen == []


# -- the filter lists ---------------------------------------------------------
def test_projects_are_read_page_by_page_on_cloud_and_sorted_by_name(jira):
    pages = iter([{"values": [{"key": "WEB", "name": "Web platform"}], "isLast": False},
                  {"values": [{"key": "QA", "name": "Quality"}], "isLast": True}])
    server = jira(**{"/rest/api/3/project/search": lambda request: next(pages)})
    assert _call("projects", server)["projects"] == [
        {"key": "QA", "name": "Quality"}, {"key": "WEB", "name": "Web platform"}]
    assert len(server.seen) == 2


def test_statuses_come_once_each_in_workflow_order(jira):
    server = jira(**{"/rest/api/3/status": [
        {"name": "Done", "statusCategory": {"key": "done"}},
        {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        {"name": "To Do", "statusCategory": {"key": "new"}},
        {"name": "to do", "statusCategory": {"key": "new"}}]})
    assert [one["name"] for one in _call("statuses", server)["statuses"]] == [
        "To Do", "In Progress", "Done"]


# -- the client ---------------------------------------------------------------
def test_a_put_and_a_file_upload_are_what_jira_expects(jira):
    server = jira(**{"PUT /rest/api/3/issue/QA-1": (204, None),
                     "POST /rest/api/3/issue/QA-1/attachments": [{"id": "11"}]})
    get = Session(server.site, "basic", EMAIL, TOKEN)
    assert get("/rest/api/3/issue/QA-1", body={"fields": {}}, method="PUT") == {}
    assert get.upload("/rest/api/3/issue/QA-1/attachments", 'a"b.png', b"\x89PNG",
                      "image/png") == [{"id": "11"}]
    upload = server.seen[1]
    assert upload.headers["X-Atlassian-Token"] == "no-check"
    assert upload.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'filename="ab.png"' in upload.body and b"\x89PNG" in upload.body


# -- the command --------------------------------------------------------------
@pytest.fixture
def project(tmp_path):
    return (str(tmp_path / "integrations.json"), str(tmp_path / "cyclesecrets.json"),
            tmp_path)


def _cli(capsys, project, *args):
    code = cli.main(list(args) + ["--integrations-file=%s" % project[0],
                                  "--cycle-secrets-file=%s" % project[1]])
    return code, json.loads(capsys.readouterr().out)


def _request(project, name, document):
    path = project[2] / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return "--from=%s" % path


def test_save_list_and_delete_never_print_a_token(capsys, project):
    code, saved = _cli(capsys, project, "save", _request(project, "c.json", {
        "connection": {"name": "Product team", "site": "https://qa.example.invalid",
                       "email": EMAIL}, "token": TOKEN}))
    assert code == 0 and saved["connection"]["token_set"] is True
    code, listed = _cli(capsys, project, "list")
    assert [one["id"] for one in listed["connections"]] == ["product-team"]
    assert TOKEN not in json.dumps(saved) + json.dumps(listed)
    code, gone = _cli(capsys, project, "delete", "product-team")
    assert code == 0 and gone["id"] == "product-team"
    assert store.load(project[0]) == []


def test_a_wrong_connection_is_refused_with_what_to_fix(capsys, project):
    code, answer = _cli(capsys, project, "save", _request(project, "c.json", {
        "connection": {"name": "X", "site": "qa"}}))
    assert code == 1 and answer["kind"] == "invalid"
    assert "Site must start with http:// or https://." in answer["problems"]


def test_test_signs_in_and_remembers_as_whom(capsys, project, jira):
    server = jira(**{"/rest/api/3/myself": {"displayName": "Tester T",
                                            "emailAddress": EMAIL}})
    store.upsert({"name": "Product team", "site": server.site, "email": EMAIL},
                 project[0], project[1], new_token=TOKEN)
    code, answer = _cli(capsys, project, "test", "product-team")
    assert code == 0 and answer["result"]["account"] == "Tester T"
    expected = "Basic " + base64.b64encode(("%s:%s" % (EMAIL, TOKEN)).encode()).decode()
    assert server.seen[0].authorization == expected
    assert store.get("product-team", project[0])["last_check"]["account"] == "Tester T"


@pytest.mark.parametrize("answer, kind", [
    ((401, {}), "signin"), ((403, {}), "forbidden"), ((429, {}), "busy")])
def test_a_failure_is_named_for_the_page(capsys, project, jira, answer, kind):
    server = jira(**{"/rest/api/3/myself": answer})
    store.upsert({"name": "Product team", "site": server.site, "email": EMAIL},
                 project[0], project[1], new_token=TOKEN)
    code, payload = _cli(capsys, project, "call", "product-team", "test")
    assert code == 1 and payload["kind"] == kind and payload["status"] == answer[0]


def test_an_unreachable_site_and_a_missing_token_are_said_plainly(capsys, project):
    store.upsert({"name": "Gone", "site": "http://127.0.0.1:1", "email": EMAIL},
                 project[0], project[1], new_token=TOKEN)
    code, payload = _cli(capsys, project, "call", "gone", "test")
    assert payload["kind"] == "unreachable"
    store.upsert({"name": "Empty", "site": "http://127.0.0.1:1", "email": EMAIL},
                 project[0], project[1])
    code, payload = _cli(capsys, project, "call", "empty", "search")
    assert payload["kind"] == "no_token"


def test_an_unknown_operation_or_command_asks_nothing(capsys, project):
    store.upsert({"name": "One", "site": "http://127.0.0.1:1", "email": EMAIL},
                 project[0], project[1], new_token=TOKEN)
    code, payload = _cli(capsys, project, "call", "one", "delete_everything")
    assert payload["kind"] == "invalid" and "Unknown Jira operation" in payload["error"]
    code, payload = _cli(capsys, project, "frobnicate")
    assert code == 2 and payload["kind"] == "usage"


def test_the_launcher_hands_integrations_to_its_own_command(capsys, project, monkeypatch):
    import session_launcher
    monkeypatch.setattr("sys.argv", ["qavector", "--integrations", "list",
                                     "--integrations-file=%s" % project[0]])
    with pytest.raises(SystemExit) as caught:
        session_launcher.main()
    assert caught.value.code == 0
    assert json.loads(capsys.readouterr().out)["connections"] == []


# -- an API token with scopes -------------------------------------------------
@pytest.fixture
def scoped(jira, monkeypatch):
    """A site that refuses the token and a gateway that takes it: what Jira Cloud
    does with an API token that has scopes. One server plays both."""
    from integrations.jira import client
    server = jira(**{
        "/rest/api/3/myself": (401, {"errorMessages": ["Unauthorized"]}),
        "/_edge/tenant_info": {"cloudId": "cloud-7"},
        "/ex/jira/cloud-7/rest/api/3/myself": {"displayName": "Tester T"},
    })
    monkeypatch.setattr(client, "GATEWAY", server.site)
    return server


def test_a_token_the_site_refuses_is_tried_through_the_gateway_and_kept_there(
        capsys, project, scoped):
    store.upsert({"name": "Product team", "site": scoped.site, "email": EMAIL},
                 project[0], project[1], new_token=TOKEN)
    code, answer = _cli(capsys, project, "test", "product-team")
    assert code == 0 and answer["result"]["account"] == "Tester T"
    assert scoped.paths() == ["/rest/api/3/myself", "/_edge/tenant_info",
                              "/ex/jira/cloud-7/rest/api/3/myself"]
    expected = "Basic " + base64.b64encode(("%s:%s" % (EMAIL, TOKEN)).encode()).decode()
    assert scoped.seen[2].authorization == expected
    assert scoped.seen[1].authorization is None, "the cloud id is asked without the token"
    kept = store.get("product-team", project[0])
    assert kept["gateway"] is True and kept["cloud_id"] == "cloud-7"
    assert kept["last_check"]["account"] == "Tester T"
    # From now on straight to the gateway.
    del scoped.seen[:]
    _cli(capsys, project, "call", "product-team", "test")
    assert scoped.paths() == ["/ex/jira/cloud-7/rest/api/3/myself"]


def test_a_classic_token_stays_on_the_site(capsys, project, jira):
    server = jira(**{"/rest/api/3/myself": {"displayName": "Tester T"}})
    store.upsert({"name": "Product team", "site": server.site, "email": EMAIL},
                 project[0], project[1], new_token=TOKEN)
    code, _answer = _cli(capsys, project, "test", "product-team")
    assert code == 0 and server.paths() == ["/rest/api/3/myself"]
    assert "gateway" not in store.get("product-team", project[0])


def test_a_token_refused_by_both_is_a_refused_sign_in_and_nothing_is_kept(
        capsys, project, scoped):
    scoped.answers["/ex/jira/cloud-7/rest/api/3/myself"] = (401, {})
    store.upsert({"name": "Product team", "site": scoped.site, "email": EMAIL},
                 project[0], project[1], new_token=TOKEN)
    code, answer = _cli(capsys, project, "call", "product-team", "test")
    assert code == 1 and answer["kind"] == "signin"
    assert "gateway" not in store.get("product-team", project[0])


def test_a_server_connection_is_never_tried_through_the_gateway(capsys, project, scoped):
    scoped.answers["/rest/api/2/myself"] = (401, {})
    store.upsert({"name": "Ops", "site": scoped.site, "flavour": "server",
                  "auth": "bearer"}, project[0], project[1], new_token=TOKEN)
    _cli(capsys, project, "call", "ops", "test")
    assert scoped.paths() == ["/rest/api/2/myself"]


def test_a_new_token_is_found_out_again(project):
    entry, _ = store.upsert({"name": "Product team", "site": "https://x.example.invalid",
                             "email": EMAIL, "gateway": True, "cloud_id": "cloud-7"},
                            project[0], project[1], new_token=TOKEN)
    assert entry["gateway"] is True
    renamed, _ = store.upsert(dict(entry, name="Renamed"), project[0], project[1])
    assert renamed["gateway"] is True, "a rename keeps what was found out"
    again, _ = store.upsert(renamed, project[0], project[1], new_token="another")
    assert "gateway" not in again and "cloud_id" not in again


def test_a_cycle_step_with_a_scoped_token_asks_the_gateway(project):
    from cycle import registry
    from domain.cycle import Cycle, CycleStep
    from cycle import connections as binding
    from integrations.jira import client
    store.upsert({"name": "Product team", "site": "https://x.example.invalid",
                  "email": EMAIL, "gateway": True, "cloud_id": "cloud-7"},
                 project[0], project[1], new_token=TOKEN)
    cycle = Cycle(id="c", steps=(CycleStep("q", "jira.issues",
                                           settings={"connection": "product-team"}),))
    found, problems = binding.load(cycle, None, project[0], project[1])
    expanded = binding.expand(registry.get("jira.issues"), {"connection": "product-team"},
                              found)
    assert problems == []
    assert expanded["api"] == client.gateway("cloud-7")
    assert expanded["auth"] == "basic" and expanded["email"] == EMAIL
    assert expanded["token"] == TOKEN and expanded["site"] == "https://x.example.invalid"


def test_an_old_style_cycle_step_with_a_scoped_token_finds_the_gateway(scoped, tmp_path):
    from cycle import registry
    from cycle.context import CancelToken, RunContext
    from cycle.workspace import create
    from domain.cycle import CycleRun, CycleStep
    scoped.answers["/rest/api/3/search/jql"] = (401, {})
    scoped.answers["/ex/jira/cloud-7/rest/api/3/search/jql"] = {
        "isLast": True, "issues": [_raw("QA-142")]}
    workspace = create("20261107-000000-s", str(tmp_path))
    context = RunContext(CycleRun(id="r", cycle_id="c", workspace=workspace), None,
                         workspace, cancel=CancelToken())
    step = CycleStep("mine", "jira.issues", settings={
        "site": scoped.site, "email": EMAIL, "token": TOKEN, "user": "currentUser()",
        "comments": False, "attachments": "none", "subtasks": "none"})
    result = registry.get("jira.issues").execute(context, step)
    assert result.ok, result.message
    assert result.outputs["keys"] == ["QA-142"]
    assert result.outputs["issues"][0]["url"] == scoped.site + "/browse/QA-142"
    assert scoped.paths("POST") == ["/rest/api/3/search/jql",
                                    "/ex/jira/cloud-7/rest/api/3/search/jql"]


def test_a_site_that_answers_a_refused_token_as_nobody_is_not_an_empty_list(
        capsys, project, scoped):
    scoped.answers["/rest/api/3/search/jql"] = {"isLast": True, "issues": []}
    scoped.answers["/ex/jira/cloud-7/rest/api/3/search/jql"] = {
        "isLast": True, "issues": [_raw("QA-142")]}
    store.upsert({"name": "Product team", "site": scoped.site, "email": EMAIL},
                 project[0], project[1], new_token=TOKEN)
    code, answer = _cli(capsys, project, "call", "product-team", "search")
    assert code == 0 and [row["key"] for row in answer["result"]["rows"]] == ["QA-142"]
    assert store.get("product-team", project[0])["gateway"] is True


def test_an_empty_list_with_a_good_token_is_just_empty(capsys, project, jira):
    server = jira(**{"/rest/api/3/search/jql": {"isLast": True, "issues": []},
                     "/rest/api/3/myself": {"displayName": "Tester T"}})
    store.upsert({"name": "Product team", "site": server.site, "email": EMAIL},
                 project[0], project[1], new_token=TOKEN)
    code, answer = _cli(capsys, project, "call", "product-team", "search")
    assert code == 0 and answer["result"]["rows"] == []
    assert server.paths() == ["/rest/api/3/search/jql", "/rest/api/3/myself"]
