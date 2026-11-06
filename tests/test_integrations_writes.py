"""Changing Jira from the Integrations page, and the log of what was changed.

Each write is checked for what it sends - and for what it refuses to send: a
transition whose screen wants fields, an edit over somebody else's change, a
file from another host. Every change answers with the issue read back, and
every change, made or failed, is written down on the Activity tab; reads and
refusals are not.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations import activity, cli, store                          # noqa: E402
from integrations.jira import writes                                    # noqa: E402
from integrations.jira.operations import session                        # noqa: E402
from fake_jira import FakeJira                                          # noqa: E402

TOKEN = "tok-write-not-a-real-one"
EMAIL = "you@yourcompany.example"


def _issue(key="QA-1", status="To Do", updated="2026-11-07T10:00:00.000+0000"):
    return {"key": key, "fields": {
        "summary": "Session expires", "updated": updated,
        "status": {"name": status, "statusCategory": {"key": "new"}},
        "issuetype": {"name": "Bug"}, "priority": {"name": "High"},
        "assignee": {"displayName": "Tester T"}}, "transitions": []}


@pytest.fixture
def jira():
    made = []

    def serve(**answers):
        base = {"/rest/api/3/issue/QA-1?": _issue(),
                "/rest/api/3/issue/QA-1/comment?": {"total": 0, "comments": []},
                "/rest/api/2/issue/QA-1?": _issue(),
                "/rest/api/2/issue/QA-1/comment?": {"total": 0, "comments": []}}
        base.update(answers)
        one = FakeJira(base)
        made.append(one)
        return one

    yield serve
    for one in made:
        one.close()


def _connection(site, flavour="cloud"):
    return {"id": "product-team", "provider": "jira", "name": "Product team", "site": site,
            "flavour": flavour, "auth": "basic" if flavour == "cloud" else "bearer",
            "email": EMAIL, "verify_tls": True, "settings": {}}


def _do(name, server, request, flavour="cloud"):
    connection = _connection(server.site, flavour)
    return writes.OPERATIONS[name](session(connection, TOKEN), connection, request)


def _writes(server):
    return [(one.method, one.path.split("?")[0], one.body) for one in server.seen
            if one.method != "GET"]


# -- the workflow -------------------------------------------------------------
TRANSITIONS = {"transitions": [
    {"id": "31", "name": "Done", "to": {"name": "Done"},
     "fields": {"resolution": {"required": True, "name": "Resolution",
                               "allowedValues": [{"id": "1", "name": "Fixed"},
                                                 {"id": "2", "name": "Won't do"}]}}},
    {"id": "21", "name": "Start Progress", "to": {"name": "In Progress"}, "fields": {}}]}


def test_a_transition_whose_screen_wants_fields_says_which_and_changes_nothing(jira):
    server = jira(**{"/rest/api/3/issue/QA-1/transitions": TRANSITIONS})
    with pytest.raises(writes.Refused) as caught:
        _do("transition", server, {"key": "QA-1", "to": "31"})
    assert caught.value.kind == "fields"
    assert caught.value.details["needs"] == [{"key": "resolution", "name": "Resolution",
                                              "allowed": [{"id": "1", "name": "Fixed"},
                                                          {"id": "2", "name": "Won't do"}]}]
    assert _writes(server) == []


def test_a_transition_with_its_fields_is_made_and_the_issue_read_back(jira):
    server = jira(**{"/rest/api/3/issue/QA-1/transitions": TRANSITIONS,
                     "POST /rest/api/3/issue/QA-1/transitions": (204, None)})
    answer = _do("transition", server, {"key": "QA-1", "to": "31",
                                        "fields": {"resolution": "1"}})
    assert _writes(server) == [("POST", "/rest/api/3/issue/QA-1/transitions",
                                {"transition": {"id": "31"},
                                 "fields": {"resolution": {"id": "1"}}})]
    assert answer["issue"]["key"] == "QA-1" and answer["summary"] == "QA-1: Done"


def test_a_transition_the_issue_does_not_offer_is_refused_with_what_it_does(jira):
    server = jira(**{"/rest/api/3/issue/QA-1/transitions": TRANSITIONS})
    with pytest.raises(writes.Refused) as caught:
        _do("transition", server, {"key": "QA-1", "to": "99"})
    assert "Done, Start Progress" in str(caught.value)


# -- comments -----------------------------------------------------------------
def test_a_comment_is_a_document_on_cloud_and_text_on_server(jira):
    server = jira(**{"POST /rest/api/3/issue/QA-1/comment": {"id": "9"},
                     "POST /rest/api/2/issue/QA-1/comment": {"id": "9"}})
    _do("comment", server, {"key": "QA-1", "body": "Line one\nline two\n\nNext"})
    _do("comment", server, {"key": "QA-1", "body": "Plain"}, flavour="server")
    cloud, plain = [one[2] for one in _writes(server)]
    assert cloud["body"]["type"] == "doc"
    assert [node["type"] for node in cloud["body"]["content"][0]["content"]] == [
        "text", "hardBreak", "text"]
    assert len(cloud["body"]["content"]) == 2
    assert plain == {"body": "Plain"}


def test_an_empty_comment_is_not_sent(jira):
    server = jira()
    with pytest.raises(writes.Refused):
        _do("comment", server, {"key": "QA-1", "body": "  "})
    assert server.seen == []


# -- editing ------------------------------------------------------------------
def test_an_edit_over_somebody_else_s_change_is_refused(jira):
    server = jira(**{"/rest/api/3/issue/QA-1?fields=updated": _issue(
        updated="2026-11-07T11:00:00.000+0000")})
    with pytest.raises(writes.Refused) as caught:
        _do("edit", server, {"key": "QA-1", "updated": "2026-11-07T10:00:00.000+0000",
                             "fields": {"summary": "New"}})
    assert caught.value.kind == "conflict"
    assert _writes(server) == []


def test_an_edit_sends_only_what_changed(jira):
    server = jira(**{"PUT /rest/api/3/issue/QA-1": (204, None)})
    answer = _do("edit", server, {"key": "QA-1", "updated": "2026-11-07T10:00:00.000+0000",
                                  "fields": {"summary": " New ", "labels": ["needs review", ""],
                                             "priority": "3", "description": "Steps"}})
    (method, path, body), = _writes(server)
    assert (method, path) == ("PUT", "/rest/api/3/issue/QA-1")
    assert body["fields"]["summary"] == "New"
    assert body["fields"]["labels"] == ["needs-review"]
    assert body["fields"]["priority"] == {"id": "3"}
    assert body["fields"]["description"]["type"] == "doc"
    assert answer["summary"] == "QA-1: description, labels, priority, summary"


def test_what_may_be_edited_comes_from_the_issue_s_screen(jira):
    server = jira(**{"/rest/api/3/issue/QA-1/editmeta": {"fields": {
        "summary": {}, "labels": {}, "priority": {"allowedValues": [
            {"id": "1", "name": "Highest"}, {"id": "3", "name": "Medium"}]}}}})
    meta = _do("edit_meta", server, {"key": "QA-1"})
    assert meta == {"editable": ["labels", "priority", "summary"],
                    "priorities": [{"id": "1", "name": "Highest"},
                                   {"id": "3", "name": "Medium"}]}


# -- who carries it -----------------------------------------------------------
def test_assigning_to_me_uses_my_account_id_on_cloud_and_my_name_on_server(jira):
    server = jira(**{"/rest/api/3/myself": {"accountId": "acc-1"},
                     "/rest/api/2/myself": {"name": "tester"},
                     "PUT /rest/api/3/issue/QA-1/assignee": (204, None),
                     "PUT /rest/api/2/issue/QA-1/assignee": (204, None)})
    _do("assign", server, {"key": "QA-1", "account": "me"})
    _do("assign", server, {"key": "QA-1", "account": "me"}, flavour="server")
    _do("assign", server, {"key": "QA-1", "account": ""})
    assert [one[2] for one in _writes(server)] == [
        {"accountId": "acc-1"}, {"name": "tester"}, {"accountId": None}]


def test_people_to_assign_are_found_by_query_on_cloud_and_username_on_server(jira):
    server = jira(**{"/rest/api/3/user/assignable/search": [
        {"accountId": "acc-1", "displayName": "Olena", "emailAddress": EMAIL}],
        "/rest/api/2/user/assignable/search": [{"name": "olena", "displayName": "Olena"}]})
    assert _do("assignable", server, {"key": "QA-1", "query": "ol"}) == {
        "people": [{"id": "acc-1", "name": "Olena"}]}
    assert _do("assignable", server, {"key": "QA-1", "query": "ol"}, flavour="server") == {
        "people": [{"id": "olena", "name": "Olena"}]}
    assert "query=ol" in server.seen[0].path and "username=ol" in server.seen[1].path


# -- creating -----------------------------------------------------------------
def test_a_project_s_issue_types_and_a_type_s_fields_required_first(jira):
    server = jira(**{
        "/rest/api/3/issue/createmeta/QA/issuetypes?": {"issueTypes": [
            {"id": "10001", "name": "Bug"}, {"id": "10002", "name": "Sub-task",
                                            "subtask": True}]},
        "/rest/api/3/issue/createmeta/QA/issuetypes/10001": {"fields": [
            {"fieldId": "project", "name": "Project", "required": True},
            {"fieldId": "labels", "name": "Labels", "schema": {"type": "array",
                                                              "items": "string"}},
            {"fieldId": "summary", "name": "Summary", "required": True,
             "schema": {"type": "string"}},
            {"fieldId": "customfield_1", "name": "Team", "required": True,
             "schema": {"type": "option"}, "allowedValues": [{"id": "7", "value": "QA"}]},
            {"fieldId": "priority", "name": "Priority", "required": True,
             "hasDefaultValue": True, "schema": {"type": "priority"}}]}})
    types = _do("create_meta", server, {"project": "QA"})["types"]
    assert [one["name"] for one in types] == ["Bug", "Sub-task"] and types[1]["subtask"]
    fields = _do("create_meta", server, {"project": "QA", "type": "10001"})["fields"]
    assert [one["key"] for one in fields] == ["summary", "customfield_1", "labels", "priority"]
    assert fields[1]["allowed"] == [{"id": "7", "name": "QA"}]
    assert fields[3]["required"] is False, "a field with a default need not be asked for"


def test_creating_an_issue_and_reading_it_back(jira):
    server = jira(**{"POST /rest/api/3/issue": {"key": "QA-1", "id": "100"}})
    answer = _do("create", server, {"project": "QA", "type": "10001", "fields": {
        "summary": "New one", "description": "Steps", "labels": ["a b"],
        "customfield_1": {"id": "7"}, "priority": ""}})
    (method, path, body), = _writes(server)
    assert body["fields"]["project"] == {"key": "QA"}
    assert body["fields"]["issuetype"] == {"id": "10001"}
    assert body["fields"]["labels"] == ["a-b"] and "priority" not in body["fields"]
    assert body["fields"]["customfield_1"] == {"id": "7"}
    assert answer["issue"]["key"] == "QA-1" and answer["summary"] == "QA-1: created"


def test_an_issue_without_a_summary_is_not_sent(jira):
    server = jira()
    with pytest.raises(writes.Refused):
        _do("create", server, {"project": "QA", "type": "1", "fields": {"summary": " "}})
    assert server.seen == []


# -- files --------------------------------------------------------------------
def test_a_file_is_attached_and_the_issue_read_back(jira, tmp_path):
    server = jira(**{"POST /rest/api/3/issue/QA-1/attachments": [{"id": "11"}]})
    shot = tmp_path / "empty-state.png"
    shot.write_bytes(b"\x89PNG data")
    answer = _do("attach", server, {"key": "QA-1", "path": str(shot)})
    upload = [one for one in server.seen if one.method == "POST"][0]
    assert b'filename="empty-state.png"' in upload.body and b"Content-Type: image/png" in upload.body
    assert answer["summary"] == "QA-1: attached empty-state.png"


def test_a_missing_or_huge_file_is_not_sent(jira, tmp_path, monkeypatch):
    server = jira()
    with pytest.raises(writes.Refused):
        _do("attach", server, {"key": "QA-1", "path": str(tmp_path / "none.png")})
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 20)
    monkeypatch.setattr(writes, "UPLOAD_BYTES", 10)
    with pytest.raises(writes.Refused):
        _do("attach", server, {"key": "QA-1", "path": str(big)})
    assert server.seen == []


def test_a_file_is_saved_only_from_the_connection_s_own_site(jira, tmp_path):
    server = jira(**{"/secure/attachment/10/x.png": b"PNG"})
    target = tmp_path / "saved.png"
    answer = _do("download", server, {"key": "QA-1", "id": "10", "to": str(target),
                                      "url": server.site + "/secure/attachment/10/x.png"})
    assert target.read_bytes() == b"PNG" and answer["bytes"] == 3
    with pytest.raises(Exception):
        _do("download", server, {"key": "QA-1", "id": "10", "to": str(target),
                                 "url": "https://elsewhere.example.invalid/x.png"})


# -- the log ------------------------------------------------------------------
@pytest.fixture
def project(tmp_path):
    return str(tmp_path / "integrations.json"), str(tmp_path / "cyclesecrets.json"), tmp_path


def _cli(capsys, project, *args):
    code = cli.main(list(args) + ["--integrations-file=%s" % project[0],
                                  "--cycle-secrets-file=%s" % project[1]])
    return code, json.loads(capsys.readouterr().out)


def _request(project, document):
    path = project[2] / "request.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return "--from=%s" % path


def test_changes_made_and_failed_are_logged_and_reads_and_refusals_are_not(
        capsys, project, jira):
    server = jira(**{"POST /rest/api/3/issue/QA-1/comment": {"id": "9"},
                     "/rest/api/3/issue/QA-2/comment": (403, {"errorMessages": ["No"]}),
                     "/rest/api/3/search/jql": {"isLast": True, "issues": [_issue()]}})
    store.upsert({"name": "Product team", "site": server.site, "email": EMAIL},
                 project[0], project[1], new_token=TOKEN)
    _cli(capsys, project, "call", "product-team", "comment",
         _request(project, {"key": "QA-1", "body": "Done here"}))
    _cli(capsys, project, "call", "product-team", "comment",
         _request(project, {"key": "QA-2", "body": "And here"}))
    _cli(capsys, project, "call", "product-team", "comment",
         _request(project, {"key": "QA-1", "body": " "}))
    _cli(capsys, project, "call", "product-team", "search")
    code, answer = _cli(capsys, project, "activity")
    rows = answer["activity"]
    assert [(one["op"], one["target"], one["ok"]) for one in rows] == [
        ("comment", "QA-2", False), ("comment", "QA-1", True)]
    assert rows[1]["summary"] == "QA-1: comment" and rows[1]["source"] == {"label": "This page"}
    assert "Done here" not in json.dumps(rows) and TOKEN not in json.dumps(rows)
    log = activity.path_for(project[0])
    assert oct(os.stat(log).st_mode & 0o777) == "0o600"


def test_a_refusal_comes_back_with_what_to_ask_for(capsys, project, jira):
    server = jira(**{"/rest/api/3/issue/QA-1/transitions": TRANSITIONS})
    store.upsert({"name": "Product team", "site": server.site, "email": EMAIL},
                 project[0], project[1], new_token=TOKEN)
    code, answer = _cli(capsys, project, "call", "product-team", "transition",
                        _request(project, {"key": "QA-1", "to": "31"}))
    assert code == 1 and answer["kind"] == "fields"
    assert answer["needs"][0]["key"] == "resolution"


def test_the_log_keeps_the_newest_entries(project, monkeypatch):
    monkeypatch.setattr(activity, "KEEP", 5)
    monkeypatch.setattr(activity, "TRIM_AT", 8)
    for n in range(40):
        activity.record(project[0], "c", "comment", "QA-%d" % n, True, "x" * 50)
    rows = activity.read(project[0])
    # Trimmed by size, so roughly: far fewer than were written, the newest kept.
    assert len(rows) < 20 and rows[0]["target"] == "QA-39"


def test_a_cycle_step_s_change_is_logged_with_its_run(jira, tmp_path):
    from cycle import model
    from cycle.executor import run_cycle
    server = jira(**{"/rest/api/3/issue/QA-1": _issue(status="To Do"),
                     "/rest/api/3/issue/QA-1/transitions": {"transitions": [
                         {"id": "21", "name": "Start Progress", "to": {"name": "In Progress"}}]},
                     "POST /rest/api/3/issue/QA-1/transitions": (204, None)})
    path = str(tmp_path / "integrations.json")
    connection = dict(_connection(server.site), token=TOKEN, log=path)
    cycle = model.parse_cycle({"id": "move", "steps": [{
        "id": "start", "plugin": "jira.transition",
        "with": {"connection": "product-team", "issue": "QA-1", "to": "Start Progress"}}]},
        "move")
    run = run_cycle(cycle, str(tmp_path / "run"), connections={"product-team": connection})
    rows = activity.read(path)
    assert len(rows) == 1
    assert rows[0]["op"] == "transition" and rows[0]["target"] == "QA-1"
    assert rows[0]["source"] == {"label": "Cycle move · step start",
                                 "detail": "Run %s" % run.id}
