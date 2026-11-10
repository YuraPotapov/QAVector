"""A connection's predefined query: ANDed with every query, and never escaped.

What matters: the scope bounds whatever the page, a typed query or a cycle asks;
its variables come from the environment the connection names; and nothing - a
value somebody wrote into the environments file, a typed ")" too many, an extra
JQL in a cycle - can take a query out of the scope's brackets.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations import scope, store                               # noqa: E402
from integrations.jira import operations                            # noqa: E402

TOKEN = "tok-71c2-not-a-real-one"
EMAIL = "you@yourcompany.example"


def _environments(path, **variables_by_env):
    """A launch-projects.json with one project and these environments."""
    document = {"version": 2, "projects": [{"id": "p1", "name": "Shop", "environments": [
        {"id": env_id, "name": env_id.title(), "variables": dict(values)}
        for env_id, values in variables_by_env.items()]}]}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return path


def _settings(query, env_id=""):
    return {"predefined_query": query, "variables_from": env_id}


@pytest.fixture
def envs(tmp_path):
    return _environments(str(tmp_path / "launch-projects.json"),
                         staging={"jira_project": "QA", "url": "http://x.example.invalid"},
                         other={"jira_project": "WEB"})


# -- resolving ----------------------------------------------------------------
def test_no_predefined_query_is_no_scope(envs):
    assert scope.resolve(_settings(""), envs) == ""
    assert scope.resolve({}, envs) == ""


def test_a_variable_becomes_a_quoted_literal_of_the_chosen_environment(envs):
    assert scope.resolve(_settings("project = {{jira_project}}", "staging"), envs) \
        == 'project = "QA"'
    assert scope.resolve(_settings("project = {{ jira_project }}", "other"), envs) \
        == 'project = "WEB"'


def test_a_query_without_variables_needs_no_environment(envs):
    assert scope.resolve(_settings('project in (QA, WEB)'), envs) == 'project in (QA, WEB)'


@pytest.mark.parametrize("settings, words", [
    (_settings("project = {{jira_project}}"), "choose the environment"),
    (_settings("project = {{jira_project}}", "gone"), "is not in this project"),
    (_settings("project = {{board}}", "staging"), "does not define {{board}}"),
])
def test_what_does_not_resolve_is_refused_never_left_out(envs, settings, words):
    with pytest.raises(scope.ScopeError) as caught:
        scope.resolve(settings, envs)
    assert words in str(caught.value)


def test_a_missing_environments_file_resolves_nothing(tmp_path):
    with pytest.raises(scope.ScopeError):
        scope.resolve(_settings("project = {{jira_project}}", "staging"),
                      str(tmp_path / "none.json"))


# -- injection ----------------------------------------------------------------
@pytest.mark.parametrize("value", [
    'QA") OR (project is not EMPTY',
    "QA OR project is not EMPTY",
    'QA" OR "1" = "1',
    "QA) OR (key is not empty",
    "QA\\",
])
def test_a_value_written_into_the_environments_file_stays_one_literal(tmp_path, value):
    path = _environments(str(tmp_path / "e.json"), staging={"jira_project": value})
    resolved = scope.resolve(_settings("project = {{jira_project}}", "staging"), path)
    # One literal: the whole value, escaped, and the query's shape unchanged.
    assert resolved == 'project = "%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    assert scope._structure(resolved) == (None, None)
    anded = scope.anded(resolved, "assignee = currentUser()")
    assert anded.startswith('(project = "') and anded.endswith(") AND (assignee = currentUser())")


def test_a_value_with_a_line_break_is_refused(tmp_path):
    path = _environments(str(tmp_path / "e.json"), staging={"jira_project": "QA\nOR 1=1"})
    with pytest.raises(scope.ScopeError) as caught:
        scope.resolve(_settings("project = {{jira_project}}", "staging"), path)
    assert "control character" in str(caught.value)


def test_a_reference_written_inside_quotes_is_refused(envs):
    with pytest.raises(scope.ScopeError) as caught:
        scope.resolve(_settings('project = "{{jira_project}}"', "staging"), envs)
    assert "without quotes" in str(caught.value)


@pytest.mark.parametrize("query, words", [
    ("project = QA) OR (project = WEB", "closes a bracket it never opened"),
    ("(project = QA", "never closed"),
    ('project = "QA', "never ended"),
    ("project = QA ORDER BY created", "cannot have ORDER BY"),
])
def test_a_predefined_query_that_is_not_whole_is_refused(envs, query, words):
    with pytest.raises(scope.ScopeError) as caught:
        scope.resolve(_settings(query), envs)
    assert words in str(caught.value)


@pytest.mark.parametrize("typed", [
    "status = Open) OR (project is not EMPTY",
    "status = Open) OR (project is not EMPTY ORDER BY created",
    '(status = "Open',
])
def test_a_typed_query_cannot_reach_past_the_scope(typed):
    connection = {"scope": 'project = "QA"', "settings": {}}
    with pytest.raises(scope.ScopeError):
        operations.jql({"jql": typed}, connection)


def test_order_by_inside_a_string_or_brackets_is_not_the_ordering():
    body, order = scope.clause('text ~ "order by" AND (summary ~ "x order by y")')
    assert order == "" and body.endswith('"x order by y")')
    body, order = scope.clause("status = Open order  by created DESC")
    assert (body, order) == ("status = Open", "order  by created DESC")
    body, order = scope.clause("reorder = 1 ORDER BY rank")
    assert (body, order) == ("reorder = 1", "ORDER BY rank")


# -- the page's queries -------------------------------------------------------
SCOPED = {"scope": 'project = "QA"', "settings": {"default_project": ""}}


@pytest.mark.parametrize("request_, expected", [
    ({}, '(project = "QA") ORDER BY updated DESC'),
    ({"status": "Open"}, '(project = "QA") AND status = "Open" ORDER BY updated DESC'),
    ({"text": "qa-12"}, '(project = "QA") AND key = "QA-12" ORDER BY updated DESC'),
    ({"text": "QA-12", "everywhere": True},
     '(project = "QA") AND key = "QA-12" ORDER BY updated DESC'),
    ({"jql": "status = Open"}, '(project = "QA") AND (status = Open) ORDER BY updated DESC'),
    ({"jql": "status = Open ORDER BY created"},
     '(project = "QA") AND (status = Open) ORDER BY created'),
    ({"jql": "ORDER BY created"}, '(project = "QA") ORDER BY created'),
])
def test_every_page_query_is_anded_with_the_scope(request_, expected):
    assert operations.jql(request_, SCOPED) == expected


def test_without_a_scope_a_page_query_is_what_it_was():
    plain = {"settings": {"default_project": ""}}
    assert operations.jql({"jql": "status = Open"}, plain) == \
        "status = Open ORDER BY updated DESC"
    assert operations.jql({"jql": "status = Open ORDER BY created"}, plain) == \
        "status = Open ORDER BY created"


def test_the_project_list_holds_only_the_projects_the_scope_names():
    def get(path, params=None):
        return {"values": [{"key": "QA", "name": "Quality"}, {"key": "WEB", "name": "Web"},
                           {"key": "OPS", "name": "Ops"}], "isLast": True}

    connection = {"flavour": "cloud", "scope": 'project in ("QA", web)'}
    found = operations.projects(get, connection, {})["projects"]
    assert [one["key"] for one in found] == ["QA", "WEB"]
    connection["scope"] = "assignee = currentUser()"
    assert len(operations.projects(get, connection, {})["projects"]) == 3


@pytest.mark.parametrize("query, keys", [
    ('project = "QA"', ["QA"]),
    ("project = qa", ["QA"]),
    ('project in ("QA", WEB)', ["QA", "WEB"]),
    ("project != QA", None),
    ("project not in (QA)", None),
    ("subproject = QA", None),
    ("", None),
])
def test_project_keys(query, keys):
    assert scope.project_keys(query) == keys


# -- credentials and the command line -----------------------------------------
@pytest.fixture
def project(tmp_path):
    integrations = str(tmp_path / "integrations.json")
    secrets = str(tmp_path / "cyclesecrets.json")
    _environments(str(tmp_path / "launch-projects.json"), staging={"jira_project": "QA"})
    return integrations, secrets


def _save(project, **settings):
    entry, problems = store.upsert(
        {"name": "Product team", "site": "https://x.example.invalid", "email": EMAIL,
         "settings": settings}, project[0], project[1], new_token=TOKEN)
    assert problems == []
    return entry


def test_credentials_carry_the_resolved_scope(project):
    _save(project, predefined_query="project = {{jira_project}}", variables_from="staging")
    assert store.credentials("product-team", *project)["scope"] == 'project = "QA"'


def test_credentials_with_a_scope_that_does_not_resolve_are_refused(project):
    _save(project, predefined_query="project = {{board}}", variables_from="staging")
    with pytest.raises(store.Unavailable) as caught:
        store.credentials("product-team", *project)
    assert caught.value.kind == "scope" and "{{board}}" in str(caught.value)
    # A sign-in check asks no query: it is not stopped by the scope.
    assert store.credentials("product-team", *project, scoped=False)["scope"] == ""


def test_scope_is_never_stored_with_the_connection(project):
    _save(project, predefined_query="project = QA")
    entry = dict(store.credentials("product-team", *project))
    store.upsert(entry, project[0], project[1])
    with open(project[0], encoding="utf-8") as handle:
        assert '"scope"' not in handle.read()


def _cli(capsys, *args):
    from integrations import cli
    code = cli.main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_save_says_what_the_scope_lacks_and_keeps_it(project, tmp_path, capsys):
    document = tmp_path / "save.json"
    document.write_text(json.dumps({"connection": {
        "name": "Product team", "site": "https://x.example.invalid", "email": EMAIL,
        "settings": {"predefined_query": "project = {{board}}",
                     "variables_from": "staging"}}, "token": TOKEN}))
    code, answer = _cli(capsys, "save", "--from=%s" % document,
                        "--integrations-file=%s" % project[0],
                        "--cycle-secrets-file=%s" % project[1])
    assert code == 0 and answer["ok"]
    assert any("{{board}}" in one for one in answer["warnings"])
    assert store.get("product-team", project[0])["settings"]["predefined_query"] \
        == "project = {{board}}"


def test_a_call_reads_variables_from_the_environments_file_it_is_given(
        project, tmp_path, capsys):
    from fake_jira import FakeJira
    _save(project, predefined_query="project = {{jira_project}}", variables_from="staging")
    elsewhere = _environments(str(tmp_path / "elsewhere.json"),
                              staging={"jira_project": "WEB"})
    server = FakeJira({"/rest/api/3/search/jql": {"isLast": True, "issues": []},
                       "/rest/api/3/myself": {"displayName": "Tester"}})
    try:
        entry = store.get("product-team", project[0])
        store.upsert(dict(entry, site=server.site), project[0], project[1])
        code, answer = _cli(capsys, "call", "product-team", "search",
                            "--integrations-file=%s" % project[0],
                            "--cycle-secrets-file=%s" % project[1],
                            "--environments-file=%s" % elsewhere)
        assert code == 0, answer
        assert answer["result"]["jql"].startswith('(project = "WEB")')
        posted = [one.body for one in server.seen if one.method == "POST"]
        assert posted and posted[0]["jql"].startswith('(project = "WEB")')
    finally:
        server.close()


def test_a_call_with_a_broken_scope_asks_nothing(project, capsys):
    _save(project, predefined_query="project = {{board}}", variables_from="staging")
    code, answer = _cli(capsys, "call", "product-team", "search",
                        "--integrations-file=%s" % project[0],
                        "--cycle-secrets-file=%s" % project[1])
    assert code == 1 and answer["kind"] == "scope"


def test_a_typed_query_that_breaks_out_is_invalid_and_not_sent(project, tmp_path, capsys):
    _save(project, predefined_query="project = {{jira_project}}", variables_from="staging")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"jql": "status = Open) OR (project is not EMPTY"}))
    code, answer = _cli(capsys, "call", "product-team", "search", "--from=%s" % request,
                        "--integrations-file=%s" % project[0],
                        "--cycle-secrets-file=%s" % project[1])
    assert code == 1 and answer["kind"] == "invalid"
    assert "closes a bracket" in answer["error"]


def test_jira_s_quick_search_is_asked_within_the_scope(tmp_path):
    """A number is part of a key in every project: asked across the site, the
    quick search's few keys can all be other projects', and the scoped list
    then finds nothing for an issue that is there."""
    from urllib.parse import parse_qs, urlparse
    from fake_jira import FakeJira
    server = FakeJira({
        "/rest/api/3/issue/picker": {"sections": [{"issues": [{"key": "QA-1296"}]}]},
        "/rest/api/3/search/jql": {"issues": [{"key": "QA-1296", "fields": {
            "summary": "Read access", "status": {"name": "Done"}}}]}})
    try:
        connection = {"id": "c", "provider": "jira", "name": "C", "site": server.site,
                      "flavour": "cloud", "auth": "basic", "email": EMAIL,
                      "verify_tls": True, "settings": {}, "scope": 'project = "QA"'}
        answer = operations.call("search", connection, TOKEN,
                                 {"text": "1296", "everywhere": True})
        assert [one["key"] for one in answer["rows"]] == ["QA-1296"]
        asked = parse_qs(urlparse(server.seen[0].path).query)
        assert asked["query"] == ["1296"] and asked["currentJQL"] == ['project = "QA"']
        assert server.seen[1].body["jql"] == (
            '(project = "QA") AND (text ~ "1296" OR key in ("QA-1296")) '
            'ORDER BY updated DESC')
    finally:
        server.close()
