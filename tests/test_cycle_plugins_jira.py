"""Reading somebody's Jira issues.

Every test here runs a real HTTP server on a loopback port and answers it with
canned Jira payloads. That is the only honest way to test a client for somebody
else's API without an account: it checks what this code sends and what it makes
of what comes back, and claims nothing about Jira itself.

Two properties are worth more than the rest. A user string reaches the query
from a cycle file, so it has to be escaped into the JQL rather than pasted into
it. And an email address is personal data that arrives in every issue and that
nothing downstream needs, so it must not end up in a step's outputs, where it
would travel into reports and into an agent's prompt.
"""

import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import registry                                       # noqa: E402
from cycle.context import CancelToken, Cancelled, RunContext     # noqa: E402
from cycle.plugins.jira import (JiraError, jql_for,               # noqa: E402
                                projects_clause)
from cycle.workspace import create                               # noqa: E402
from domain.cycle import CycleRun, CycleStep                     # noqa: E402

ADF = {"type": "doc", "content": [
    {"type": "paragraph", "content": [{"type": "text", "text": "Cannot repeat on "},
                                      {"type": "text", "text": "staging"}]},
    {"type": "paragraph", "content": [{"type": "text", "text": "Closing it."}]}]}


def _issue(key, summary="Login fails"):
    return {"key": key, "fields": {
        "summary": summary, "status": {"name": "In Progress"},
        "issuetype": {"name": "Bug"}, "priority": {"name": "High"},
        "assignee": {"displayName": "Tester T",
                     "emailAddress": "someone@example.invalid"},
        "reporter": {"displayName": "Someone Else"},
        "created": "2026-09-01T10:00:00.000+0000",
        "updated": "2026-09-18T12:00:00.000+0000"}}


class _Jira(object):
    """A loopback server answering whatever the test told it to."""

    def __init__(self, answers):
        self.answers = answers          # path prefix -> payload or (status, payload)
        self.seen = []                  # (method, path, body, authorization)
        seen, answers_ = self.seen, self.answers

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _answer(self, body):
                for prefix, payload in answers_.items():
                    if self.path.startswith(prefix):
                        break
                else:
                    payload = (404, {"errorMessages": ["no such path"]})
                status, value = payload if isinstance(payload, tuple) else (200, payload)
                if callable(value):
                    # The index of THIS call: seen already has it in it.
                    value = value(body, len(seen) - 1)
                # Bytes are served as they are: an attachment is a file, not
                # JSON, and the plugin fetches it down a different path.
                binary = isinstance(value, bytes)
                raw = value if binary else json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "image/png" if binary
                                 else "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):           # noqa: N802 - the base class's spelling
                seen.append(("GET", self.path, None,
                             self.headers.get("Authorization")))
                self._answer(None)

            def do_POST(self):          # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                seen.append(("POST", self.path, body,
                             self.headers.get("Authorization")))
                self._answer(body)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.site = "http://127.0.0.1:%d" % self._server.server_port
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def paths(self):
        return [one[1] for one in self.seen]


@pytest.fixture
def jira():
    made = []

    def serve(**answers):
        one = _Jira(answers)
        made.append(one)
        return one

    yield serve
    for one in made:
        one.close()


@pytest.fixture
def run(tmp_path):
    """Runs jira.issues against a site, with settings a test can override."""
    def go(site, cancel=None, **settings):
        workspace = create("20260919-000000-j", str(tmp_path))
        context = RunContext(CycleRun(id="r", cycle_id="c",
                                      workspace=workspace),
                             None, workspace, cancel=cancel or CancelToken())
        step = CycleStep(id="mine", plugin="jira.issues", settings=dict({
            "site": site, "email": "me@example.invalid", "token": "sekret",
            "user": "5b10a2"}, **settings))
        return registry.get("jira.issues").execute(context, step)
    return go


CLOUD_SEARCH = "/rest/api/3/search/jql"
CLOUD_COMMENT = "/rest/api/3/issue"
SERVER_SEARCH = "/rest/api/2/search"
SERVER_COMMENT = "/rest/api/2/issue"


# --------------------------------------------------------------------- Cloud
def test_it_reads_the_issues_and_the_comments_on_them(jira, run):
    server = jira(**{
        CLOUD_SEARCH: {"issues": [_issue("QA-1"), _issue("QA-2")], "isLast": True},
        CLOUD_COMMENT: {"comments": [
            {"author": {"displayName": "Reviewer"},
             "created": "2026-09-17T09:00:00.000+0000", "body": ADF}]}})

    result = run(server.site)

    assert result.ok, result.message
    assert result.outputs["keys"] == ["QA-1", "QA-2"]
    assert result.outputs["count"] == 2
    assert result.outputs["comment_count"] == 2
    assert result.message == "jira: 2 issues, 2 comments"


def test_the_first_key_comes_back_on_its_own_for_a_step_that_wants_one(jira,
                                                                       run):
    """`${...}` cannot index a list, so a later step handed `keys` gets the
    whole list where it wanted one issue."""
    server = jira(**{CLOUD_SEARCH: {"issues": [_issue("QA-1"), _issue("QA-2")],
                                    "isLast": True},
                     CLOUD_COMMENT: {"comments": []}})
    result = run(server.site)

    assert result.outputs["key"] == "QA-1"
    assert result.outputs["keys"] == ["QA-1", "QA-2"]


def test_nothing_found_leaves_the_single_key_empty_rather_than_missing(jira,
                                                                       run):
    """Always present, so a condition against it resolves."""
    server = jira(**{CLOUD_SEARCH: {"issues": [], "isLast": True}})
    assert run(server.site).outputs["key"] == ""


def test_an_issue_carries_what_a_later_step_would_want_of_it(jira, run):
    server = jira(**{CLOUD_SEARCH: {"issues": [_issue("QA-1")], "isLast": True},
                     CLOUD_COMMENT: {"comments": []}})
    one = run(server.site).outputs["issues"][0]

    assert one["summary"] == "Login fails"
    assert one["status"] == "In Progress"
    assert one["type"] == "Bug"
    assert one["assignee"] == "Tester T"
    assert one["reporter"] == "Someone Else"
    assert one["url"] == server.site + "/browse/QA-1"


def test_a_cloud_comment_arrives_as_text_rather_than_a_document_tree(jira, run):
    """Cloud sends Atlassian Document Format. What reads this is a report or an
    agent, and neither wants a tree of nodes."""
    server = jira(**{CLOUD_SEARCH: {"issues": [_issue("QA-1")], "isLast": True},
                     CLOUD_COMMENT: {"comments": [
                         {"author": {"displayName": "Reviewer"}, "body": ADF}]}})
    body = run(server.site).outputs["issues"][0]["comments"][0]["body"]

    assert body == "Cannot repeat on staging\nClosing it."


def test_nobody_s_email_ends_up_in_the_outputs(jira, run):
    """It is in every issue Jira returns, nothing downstream needs it, and a
    step's outputs travel into reports and into agents' prompts."""
    server = jira(**{CLOUD_SEARCH: {"issues": [_issue("QA-1")], "isLast": True},
                     CLOUD_COMMENT: {"comments": []}})
    result = run(server.site)

    assert "someone@example.invalid" not in json.dumps(result.outputs)


def test_the_whole_answer_keeps_its_json_and_gets_a_reading_copy(jira, run, tmp_path):
    server = jira(**{CLOUD_SEARCH: {"issues": [_issue("QA-1")], "isLast": True},
                     CLOUD_COMMENT: {"comments": []}})
    result = run(server.site)

    assert [one.name for one in result.artifacts] == ["jira", "jira.html"]
    assert result.outputs["report_path"].endswith("jira.json")
    assert result.outputs["html_path"].endswith("jira.html")
    directory = tmp_path / "20260919-000000-j"
    technical = directory / result.outputs["report_path"]
    readable = directory / result.outputs["html_path"]
    answer = json.loads(technical.read_text())
    assert set(answer) == {"jql", "issues"}
    assert answer["issues"] == result.outputs["issues"]
    assert technical.read_text() == json.dumps(answer, indent=2, ensure_ascii=False) + "\n"
    assert technical.parent == readable.parent
    assert "Login fails" in readable.read_text()
    assert all(artifact.bytes == (directory / artifact.path).stat().st_size
               for artifact in result.artifacts)


def test_reading_copy_keeps_rich_bodies_without_adding_requests_or_json_fields(jira, run, tmp_path):
    issue = _issue("QA-1")
    issue["fields"]["description"] = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 1},
         "content": [{"type": "text", "text": "Acceptance"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Required",
                                            "marks": [{"type": "strong"}]}]}]}
    comment = {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "Reviewed", "marks": [{"type": "em"}]}]}]}
    server = jira(**{CLOUD_SEARCH: {"issues": [issue], "isLast": True},
                     CLOUD_COMMENT: {"comments": [{"body": comment,
                                      "author": {"displayName": "Reviewer"}}]}})
    result = run(server.site)
    assert result.ok, result.message
    assert len(server.seen) == 2
    assert result.outputs["issues"][0]["description"] == "Acceptance\nRequired"
    assert result.outputs["issues"][0]["comments"][0]["body"] == "Reviewed"
    page = (tmp_path / "20260919-000000-j" / result.outputs["html_path"]).read_text()
    assert "<h3>Acceptance</h3>" in page
    assert "<strong>Required</strong>" in page
    assert "<em>Reviewed</em>" in page
    assert "sekret" not in page and "me@example.invalid" not in page


def test_reading_copy_obeys_the_description_limit(jira, run, tmp_path):
    issue = _issue("QA-1")
    issue["fields"]["description"] = _adf(_para("A long description"))
    server = jira(**{CLOUD_SEARCH: {"issues": [issue], "isLast": True}})
    result = run(server.site, comments=False, description_limit=6)
    page = (tmp_path / "20260919-000000-j" / result.outputs["html_path"]).read_text()
    assert "cut at 6 characters" in page
    assert "A long description" not in page


# -------------------------------------------------------------------- Server
def test_server_uses_its_own_endpoint_and_takes_comments_as_text(jira, run):
    server = jira(**{SERVER_SEARCH: {"issues": [_issue("QA-1")], "total": 1},
                     SERVER_COMMENT: {"comments": [
                         {"author": {"name": "reviewer"},
                          "body": "plain text, the way Server sends it"}]}})

    result = run(server.site, flavour="server")

    assert result.ok, result.message
    assert any(one.startswith(SERVER_SEARCH) for one in server.paths())
    assert result.outputs["issues"][0]["comments"][0]["body"] == (
        "plain text, the way Server sends it")


def test_a_server_account_without_a_display_name_still_has_one(jira, run):
    server = jira(**{SERVER_SEARCH: {"issues": [_issue("QA-1")], "total": 1},
                     SERVER_COMMENT: {"comments": [
                         {"author": {"name": "reviewer"}, "body": "hi"}]}})
    assert run(server.site, flavour="server").outputs[
        "issues"][0]["comments"][0]["author"] == "reviewer"


# ------------------------------------------------------------ authentication
def test_cloud_signs_in_with_the_email_and_the_token(jira, run):
    server = jira(**{CLOUD_SEARCH: {"issues": [], "isLast": True}})
    run(server.site)

    scheme, value = server.seen[0][3].split()
    assert scheme == "Basic"
    assert base64.b64decode(value).decode() == "me@example.invalid:sekret"


def test_a_server_token_is_sent_as_a_bearer(jira, run):
    server = jira(**{SERVER_SEARCH: {"issues": [], "total": 0}})
    run(server.site, flavour="server", auth="bearer", email="")

    assert server.seen[0][3] == "Bearer sekret"


def test_the_token_is_never_in_a_url(jira, run):
    """A URL reaches logs and proxies; an Authorization header does not."""
    server = jira(**{CLOUD_SEARCH: {"issues": [], "isLast": True}})
    run(server.site)

    assert all("sekret" not in path for path in server.paths())


# ---------------------------------------------------------------- the query
def test_any_means_assignee_or_reporter_or_creator():
    """Work is often reported by one person and carried by another."""
    query = jql_for("5b10a2", "any")
    assert query.startswith('(assignee = "5b10a2" OR reporter = "5b10a2" '
                            'OR creator = "5b10a2")')
    assert query.endswith("ORDER BY updated DESC")


def test_one_role_asks_about_that_role_alone():
    assert jql_for("tester", "assignee").startswith('assignee = "tester"')


# ------------------------------------------------------------- the ordering
def test_the_step_decides_the_order():
    """It decides which issue `limit: 1` gets, so a cycle taking one task per
    run is choosing its queue here."""
    assert jql_for("tester", "assignee", order="created ASC").endswith(
        "ORDER BY created ASC")


def test_an_ordering_written_the_long_way_is_taken_as_meant():
    """People who have typed JQL before write the words. Both readings are
    obvious, so producing a syntax error for one of them would be gratuitous."""
    assert jql_for("tester", "assignee", order="ORDER BY created ASC").endswith(
        "ORDER BY created ASC")
    assert jql_for("tester", "assignee", order="order by created ASC").count(
        "ORDER BY") == 1


def test_no_ordering_is_still_newest_first():
    for blank in ("", "   ", None, "ORDER BY"):
        assert jql_for("tester", "assignee", order=blank).endswith(
            "ORDER BY updated DESC"), blank


def test_the_ordering_goes_after_the_extra_clause_not_inside_it():
    """`extra` is bracketed, and an ORDER BY inside brackets is a syntax error
    rather than an ordering - which is the whole reason this is its own field."""
    query = jql_for("tester", "assignee", 'statusCategory != "Done"',
                    order="created ASC")

    assert query.index("ORDER BY") > query.index("statusCategory")
    assert "ORDER BY" not in query[: query.index(")", query.index("statusCategory"))]


def test_current_user_is_a_function_rather_than_a_name():
    assert 'currentUser()' in jql_for("currentUser()", "assignee")
    assert '"currentUser()"' not in jql_for("currentUser()", "assignee")


def test_no_project_reads_every_project_the_account_can_see():
    """The right default for "what is on this person's plate" - work crosses
    projects, and most people do not think in one."""
    assert "project" not in jql_for("tester", "any")
    assert projects_clause("") == ""
    assert projects_clause("  ,  ") == ""


def test_one_project_narrows_to_it():
    assert projects_clause("QA") == 'project = "QA"'
    assert 'project = "QA"' in jql_for("tester", "assignee", project="QA")


def test_several_projects_become_an_in_clause():
    assert projects_clause("QA, WEB") == 'project in ("QA", "WEB")'


def test_the_spaces_people_actually_type_are_forgiven():
    assert projects_clause(" QA ,WEB , OPS ") == 'project in ("QA", "WEB", "OPS")'


def test_a_trailing_comma_does_not_become_an_empty_project():
    assert projects_clause("QA, WEB,") == 'project in ("QA", "WEB")'


def test_the_project_and_the_extra_jql_both_narrow_the_same_query():
    query = jql_for("tester", "assignee", 'statusCategory = "To Do"', "QA, WEB")
    assert 'project in ("QA", "WEB")' in query
    assert 'AND (statusCategory = "To Do")' in query


def test_a_project_key_cannot_end_its_literal_and_become_query():
    """It comes from a cycle file or a variable, like the user string does."""
    query = jql_for("tester", "assignee", project='QA") OR project = SECRET OR x in ("')

    # Four unescaped quotes and no more: the user's literal and the project's.
    # The injected words sit inside the project literal, which is exactly what
    # escaping them achieves - so looking for the words would prove nothing.
    assert _bare_quotes(query) == 4
    assert query[: query.index("ORDER BY")].rstrip().endswith('"')
    # Any substring check over the whole query is meaningless here: the words
    # ARE in it, inside the literal, which is what escaping them does.


def test_the_project_reaches_the_query_jira_is_actually_sent(jira, run):
    server = jira(**{CLOUD_SEARCH: {"issues": [], "isLast": True}})
    run(server.site, project="QA, WEB")

    assert 'project in ("QA", "WEB")' in server.seen[0][2]["jql"]


def test_extra_jql_is_anded_in_its_own_brackets():
    """Without the brackets an OR in it would swallow the user clause."""
    query = jql_for("tester", "assignee", "a = 1 OR b = 2")
    assert 'assignee = "tester" AND (a = 1 OR b = 2)' in query


def _bare_quotes(text):
    """Quotes that would actually end a JQL literal - the unescaped ones.

    Counting the escaped ones out is what the test is about: the injected words
    are allowed to be inside the literal, which is exactly what escaping them
    achieves. What must not happen is a second literal opening.
    """
    count, index = 0, 0
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            count += 1
        index += 1
    return count


def test_a_quote_in_a_name_cannot_end_the_literal_and_become_query():
    """JQL has the injection problem every query language has, and the user
    string comes from a cycle file or a variable."""
    query = jql_for('a" OR project = SECRET OR x = "', "assignee")

    # Exactly one literal: the one this opened and closed. The injected words
    # are inside it, which is the whole point of escaping them.
    assert _bare_quotes(query) == 2
    assert query.startswith('assignee = "')
    assert query[: query.index("ORDER BY")].rstrip().endswith('"')


def test_a_backslash_is_escaped_too():
    assert jql_for("a\\b", "assignee").startswith('assignee = "a\\\\b"')


# ------------------------------------------------------------------- paging
def test_cloud_follows_the_page_token_until_the_last_page(jira, run):
    def pages(body, _count):
        if not body.get("nextPageToken"):
            return {"issues": [_issue("QA-%d" % n) for n in range(50)],
                    "nextPageToken": "more", "isLast": False}
        return {"issues": [_issue("QA-50"), _issue("QA-51")], "isLast": True}

    server = jira(**{CLOUD_SEARCH: pages, CLOUD_COMMENT: {"comments": []}})
    result = run(server.site, limit=60, comments=False)

    assert result.outputs["count"] == 52
    assert len([one for one in server.paths() if one.startswith(CLOUD_SEARCH)]) == 2


def test_server_pages_by_offset_until_it_has_them_all(jira, run):
    def pages(_body, count):
        start = 0 if count == 0 else 50
        return {"issues": [_issue("QA-%d" % (start + n))
                           for n in range(50 if start == 0 else 2)],
                "total": 52, "startAt": start}

    server = jira(**{SERVER_SEARCH: pages})
    result = run(server.site, flavour="server", limit=60, comments=False)

    assert result.outputs["count"] == 52


def test_the_limit_is_what_comes_back_even_when_jira_has_more(jira, run):
    server = jira(**{CLOUD_SEARCH: {"issues": [_issue("QA-%d" % n)
                                               for n in range(50)],
                                    "nextPageToken": "more", "isLast": False}})
    assert run(server.site, limit=5, comments=False).outputs["count"] == 5


def test_comments_are_one_request_per_issue_and_can_be_turned_off(jira, run):
    server = jira(**{CLOUD_SEARCH: {"issues": [_issue("QA-1"), _issue("QA-2")],
                                    "isLast": True}})
    result = run(server.site, comments=False)

    assert result.outputs["comment_count"] == 0
    assert not any(one.startswith(CLOUD_COMMENT) for one in server.paths())


# ---------------------------------------------------------------- what fails
def test_refused_credentials_say_which_credentials(jira, run):
    """On Cloud it is an API token, not a password, and a bare 401 sends
    somebody to reset the wrong thing."""
    server = jira(**{CLOUD_SEARCH: (401, {})})
    result = run(server.site)

    assert not result.ok
    assert "401" in result.message and "API token" in result.message


def test_a_jql_jira_would_not_accept_says_what_it_objected_to(jira, run):
    server = jira(**{CLOUD_SEARCH: (400, {"errorMessages": [
        "Field 'nope' does not exist or you do not have permission to view it."]})})
    result = run(server.site, jql="nope = 1")

    assert not result.ok
    assert "does not exist" in result.message


def test_the_wrong_product_is_named_as_the_likely_cause(jira, run):
    """Pointing the Server endpoints at Cloud is the ordinary first mistake."""
    server = jira(**{SERVER_SEARCH: (404, {})})
    result = run(server.site, flavour="server")

    assert not result.ok
    assert "Cloud and Server" in result.message


def test_a_site_that_does_not_answer_fails_the_step_rather_than_hanging(run):
    result = run("http://127.0.0.1:1")      # nothing listens there
    assert not result.ok
    assert "Cannot reach" in result.message


def test_a_stop_between_pages_is_obeyed(jira, run):
    """A run told to stop must not carry on paging through a thousand issues."""
    token = CancelToken()

    def pages(_body, _count):
        token.set("stopped")
        return {"issues": [_issue("QA-%d" % n) for n in range(50)],
                "nextPageToken": "more", "isLast": False}

    server = jira(**{CLOUD_SEARCH: pages})
    with pytest.raises(Cancelled):
        run(server.site, cancel=token, limit=200, comments=False)


# ------------------------------------------------------------------ refusals
def test_the_site_has_to_look_like_a_url():
    plugin = registry.get("jira.issues")
    assert plugin.problems({"site": "yourcompany.atlassian.net", "token": "t",
                            "user": "u", "email": "e"})


def test_basic_authentication_without_an_email_is_refused_before_the_401():
    """Jira's own answer does not say which half of the pair was missing."""
    plugin = registry.get("jira.issues")
    problems = plugin.problems({"site": "https://x.invalid", "token": "t",
                                "user": "u"})
    assert any("email" in one for one in problems)


def test_a_bearer_token_needs_no_email():
    plugin = registry.get("jira.issues")
    assert plugin.problems({"site": "https://x.invalid", "token": "t",
                            "user": "u", "auth": "bearer"}) == []


def test_an_impossible_limit_is_refused_before_anything_is_asked():
    plugin = registry.get("jira.issues")
    for limit in (0, -1, 5000, 2.5, True):
        assert plugin.problems({"site": "https://x.invalid", "token": "t",
                                "user": "u", "email": "e", "limit": limit})


def test_a_reference_is_left_for_the_run_to_resolve():
    plugin = registry.get("jira.issues")
    assert plugin.problems({"site": "https://x.invalid", "token": "${vars.t}",
                            "user": "${vars.u}", "email": "e",
                            "limit": "${vars.n}"}) == []


def test_it_reads_and_only_reads():
    """No create, no transition, no edit - so a cycle can point at production."""
    plugin = registry.get("jira.issues")
    assert plugin.metadata.permissions == ("network",)
    assert "filesystem.write" not in plugin.metadata.permissions


def test_its_metadata_survives_the_wire():
    json.dumps(registry.get("jira.issues").metadata.to_dict())


def test_the_error_type_is_never_raised_out_of_a_step(jira, run):
    """Every failure here is a result with a status, not an exception."""
    server = jira(**{CLOUD_SEARCH: (500, {})})
    try:
        result = run(server.site)
    except JiraError:                       # pragma: no cover - the bug itself
        pytest.fail("JiraError escaped the step")
    assert not result.ok


# ------------------------------------------------ the body of the task itself
# The summary is a title. A plan made from the title and the comments alone is
# a plan made from the margins of the page - and the description was not even
# asked for, so it could never have arrived.
def test_the_description_is_among_the_fields_asked_for():
    from cycle.plugins.jira import FIELDS
    assert "description" in FIELDS


def _adf(*content):
    return {"type": "doc", "version": 1, "content": list(content)}


def _para(*text):
    return {"type": "paragraph",
            "content": [{"type": "text", "text": one} for one in text]}


def test_a_description_comes_back_as_readable_text():
    from cycle.plugins.jira import _issue

    one = _issue({"key": "QA-1", "fields": {
        "summary": "Short title",
        "description": _adf(_para("The actual task."))}},
        "https://x.invalid", "cloud", 0)

    assert one["description"] == "The actual task."
    assert one["summary"] == "Short title"


def test_a_table_keeps_its_rows_and_its_columns():
    """A table whose cells run together is a wall of words: the rows and the
    columns are what made it a table."""
    from cycle.plugins.jira import _flatten

    table = {"type": "table", "content": [
        {"type": "tableRow", "content": [
            {"type": "tableHeader", "content": [_para("Field")]},
            {"type": "tableHeader", "content": [_para("Rule")]}]},
        {"type": "tableRow", "content": [
            {"type": "tableCell", "content": [_para("status")]},
            {"type": "tableCell", "content": [_para("must be paid")]}]}]}

    lines = _flatten(table).strip().splitlines()
    assert lines == ["| Field | Rule |", "| status | must be paid |"]


def test_a_cell_of_two_sentences_stays_on_its_row():
    """One that kept its paragraph breaks would put the rest of the row on the
    next line, and the table would stop lining up."""
    from cycle.plugins.jira import _flatten

    row = {"type": "tableRow", "content": [
        {"type": "tableCell", "content": [_para("One."), _para("Two.")]},
        {"type": "tableCell", "content": [_para("b")]}]}

    assert _flatten(row).strip() == "| One. Two. | b |"


def test_a_list_is_a_set_of_things_rather_than_one_paragraph():
    from cycle.plugins.jira import _flatten

    bullets = {"type": "bulletList", "content": [
        {"type": "listItem", "content": [_para("first")]},
        {"type": "listItem", "content": [_para("second")]}]}
    numbered = {"type": "orderedList", "content": [
        {"type": "listItem", "content": [_para("one")]},
        {"type": "listItem", "content": [_para("two")]}]}

    assert _flatten(bullets).strip().splitlines() == ["- first", "- second"]
    assert _flatten(numbered).strip().splitlines() == ["1. one", "2. two"]


def test_a_links_address_is_kept_beside_its_words():
    """ADF puts it in a mark rather than in the text, so flattening the leaves
    alone drops every URL in the document."""
    from cycle.plugins.jira import _flatten

    linked = {"type": "paragraph", "content": [
        {"type": "text", "text": "the spec",
         "marks": [{"type": "link", "attrs": {"href": "https://x.invalid/s"}}]}]}

    assert _flatten(linked).strip() == "the spec (https://x.invalid/s)"


def test_a_mention_keeps_the_name_it_showed():
    from cycle.plugins.jira import _flatten

    said = {"type": "paragraph", "content": [
        {"type": "text", "text": "ask "},
        {"type": "mention", "attrs": {"id": "5b1", "text": "@Tester"}}]}

    assert _flatten(said).strip() == "ask @Tester"


def test_a_server_description_is_already_text():
    from cycle.plugins.jira import _issue

    one = _issue({"key": "QA-1", "fields": {"description": "  plain text  "}},
                 "https://x.invalid", "server", 0)
    assert one["description"] == "plain text"


def test_an_issue_with_no_description_says_nothing_rather_than_failing():
    from cycle.plugins.jira import _issue

    assert _issue({"key": "QA-1", "fields": {}}, "https://x.invalid",
                  "cloud", 0)["description"] == ""


# ------------------------------------------------------------- and its size
def test_a_very_long_description_is_cut_and_says_that_it_was():
    """What reads this is an agent about to plan work from it, and silently
    handing it three quarters of a specification is how it confidently plans
    three quarters of the job."""
    from cycle.plugins.jira import _issue

    one = _issue({"key": "QA-1", "fields": {
        "description": _adf(_para("x" * 5000))}},
        "https://x.invalid", "cloud", 100)

    assert one["description"].startswith("x" * 100)
    assert "cut at 100 characters" in one["description"]


def test_no_limit_keeps_all_of_it():
    from cycle.plugins.jira import _issue

    one = _issue({"key": "QA-1", "fields": {
        "description": _adf(_para("x" * 5000))}},
        "https://x.invalid", "cloud", 0)

    assert one["description"] == "x" * 5000


def test_a_description_that_fits_is_left_alone():
    from cycle.plugins.jira import _issue

    one = _issue({"key": "QA-1", "fields": {"description": _adf(_para("short"))}},
                 "https://x.invalid", "cloud", 20000)
    assert one["description"] == "short"


# --------------------------------------------------------------- attachments
# A bug report's specification is often a screenshot. The step used not to ask
# Jira for the attachment field at all, so nothing downstream knew a picture
# existed - and an image in the description was dropped from the text without
# trace, which is worse: the words that remained read as the whole of the task.
PNG = b"\x89PNG\r\n\x1a\n" + b"pretend this is an image" * 4
CONTENT = "/secure/attachment"


def _attached(filename="screenshot.png", mime="image/png", size=len(PNG),
              attachment_id="10001"):
    return {"id": attachment_id, "filename": filename, "mimeType": mime,
            "size": size, "created": "2026-09-18T12:00:00.000+0000",
            "author": {"displayName": "Someone Else",
                       "emailAddress": "someone@example.invalid"},
            "content": "%s/%s/%s" % (CONTENT, attachment_id, filename)}


def _with_files(key, *attachments):
    issue = _issue(key)
    issue["fields"]["attachment"] = list(attachments)
    return issue


def _serving(jira, *attachments, **extra):
    answers = {CLOUD_SEARCH: {"issues": [_with_files("QA-1", *attachments)],
                              "isLast": True},
               CLOUD_COMMENT: {"comments": []},
               CONTENT: PNG}
    answers.update(extra)
    return jira(**answers)


def test_the_attachment_field_is_asked_for(jira, run):
    """It is not in the response at all unless the search asks for it."""
    server = _serving(jira, _attached())
    run(server.site)
    assert any("attachment" in str(one[2] or {}).lower()
               or "attachment" in one[1] for one in server.seen)


def test_an_issue_lists_the_files_on_it(jira, run):
    server = _serving(jira, _attached())
    issue = run(server.site).outputs["issues"][0]

    assert len(issue["attachments"]) == 1
    only = issue["attachments"][0]
    assert only["filename"] == "screenshot.png"
    assert only["mime"] == "image/png"
    assert only["size"] == len(PNG)


def test_a_picture_is_downloaded_and_its_path_given(jira, run):
    server = _serving(jira, _attached())
    result = run(server.site)

    only = result.outputs["issues"][0]["attachments"][0]
    assert only["path"], "the file should have been fetched"
    assert result.outputs["images"] == [only["path"]]
    assert result.outputs["attachment_count"] == 1


def test_what_was_downloaded_is_the_file_itself(jira, run, tmp_path):
    server = _serving(jira, _attached())
    result = run(server.site)

    where = os.path.join(str(tmp_path), "20260919-000000-j",
                         result.outputs["issues"][0]["attachments"][0]["path"])
    with open(where, "rb") as handle:
        assert handle.read() == PNG


def test_a_downloaded_file_is_an_artifact_of_the_step(jira, run):
    """So it is listed with the run's other files rather than only inside an
    output nobody opens."""
    server = _serving(jira, _attached())
    kinds = [(one.type, one.name) for one in run(server.site).artifacts]
    assert ("image", "screenshot.png") in kinds


def test_by_default_only_the_pictures_are_fetched(jira, run):
    """A 40 MB log archive is not something an agent is going to read."""
    server = _serving(jira, _attached(),
                      _attached("trace.zip", "application/zip",
                                attachment_id="10002"))
    files = {one["filename"]: one
             for one in run(server.site).outputs["attachments"]}

    assert files["screenshot.png"]["path"]
    assert files["trace.zip"]["path"] == ""
    # Listed all the same: a step that did not fetch it should still say it
    # is there, or a reader believes the issue has nothing attached.
    assert files["trace.zip"]["url"]
    assert run(server.site).outputs["attachment_count"] == 2


def test_every_file_can_be_asked_for(jira, run):
    server = _serving(jira, _attached(),
                      _attached("trace.zip", "application/zip",
                                attachment_id="10002"))
    files = {one["filename"]: one for one
             in run(server.site, attachments="all").outputs["attachments"]}
    assert files["trace.zip"]["path"]


def test_nothing_is_fetched_when_the_step_says_not_to(jira, run):
    server = _serving(jira, _attached())
    result = run(server.site, attachments="none")

    assert result.outputs["attachments"][0]["path"] == ""
    assert result.outputs["images"] == []
    assert CONTENT not in " ".join(server.paths())


def test_the_budget_stops_it_rather_than_the_run(jira, run):
    """One issue with a video on it must not turn a read into a long wait."""
    server = _serving(jira, _attached("huge.png", size=10000000),
                      _attached("small.png", attachment_id="10002"))
    files = {one["filename"]: one for one in
             run(server.site, attachment_bytes=1000).outputs["attachments"]}

    assert files["huge.png"]["path"] == ""
    # And the small one after it is still taken: a picture that fits is worth
    # having, and giving up at the first thing too big loses it.
    assert files["small.png"]["path"]


def test_a_file_that_cannot_be_fetched_costs_the_file_and_not_the_issue(jira,
                                                                        run):
    server = jira(**{CLOUD_SEARCH: {"issues": [_with_files("QA-1", _attached())],
                                    "isLast": True},
                     CLOUD_COMMENT: {"comments": []},
                     CONTENT: (500, {"errorMessages": ["no"]})})
    result = run(server.site)

    assert result.ok, result.message
    assert result.outputs["issues"][0]["attachments"][0]["path"] == ""
    assert result.outputs["keys"] == ["QA-1"]


def test_a_filename_cannot_write_outside_the_step_s_own_directory(jira, run,
                                                                  tmp_path):
    """Jira allows a filename this machine does not, separators included."""
    server = _serving(jira, _attached("../../../../etc/passwd", "image/png"))
    path = run(server.site).outputs["issues"][0]["attachments"][0]["path"]

    assert path and ".." not in path
    where = os.path.realpath(os.path.join(str(tmp_path), "20260919-000000-j",
                                          path))
    assert where.startswith(os.path.realpath(str(tmp_path)))


def test_two_files_of_one_name_do_not_replace_each_other(jira, run):
    server = _serving(jira, _attached(), _attached(attachment_id="10002"))
    paths = [one["path"] for one in run(server.site).outputs["attachments"]]
    assert len(set(paths)) == 2


def test_credentials_are_not_sent_to_a_host_the_payload_named(jira, run):
    """The content URL comes out of somebody else's JSON. Following it wherever
    it points would post this account's token there."""
    server = _serving(jira, dict(_attached(),
                                 content="http://127.0.0.1:1/secure/attachment/1/x.png"))
    result = run(server.site)

    assert result.ok
    assert result.outputs["issues"][0]["attachments"][0]["path"] == ""


def test_an_image_in_the_description_is_no_longer_dropped_in_silence(jira, run):
    """It was reduced to nothing, so a task whose whole specification was a
    screenshot arrived as a description that did not mention one."""
    issue = _with_files("QA-1", _attached())
    issue["fields"]["description"] = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Looks like:"}]},
        {"type": "mediaSingle", "content": [
            {"type": "media", "attrs": {"id": "abc", "type": "file",
                                        "alt": "screenshot.png"}}]}]}
    server = jira(**{CLOUD_SEARCH: {"issues": [issue], "isLast": True},
                     CLOUD_COMMENT: {"comments": []}, CONTENT: PNG})

    described = run(server.site).outputs["issues"][0]["description"]
    assert "Looks like:" in described
    assert "[image: screenshot.png]" in described


def test_an_image_with_no_alt_text_still_leaves_a_mark(jira, run):
    issue = _with_files("QA-1")
    issue["fields"]["description"] = {"type": "doc", "content": [
        {"type": "mediaGroup", "content": [
            {"type": "media", "attrs": {"id": "abc", "type": "file"}}]}]}
    server = jira(**{CLOUD_SEARCH: {"issues": [issue], "isLast": True},
                     CLOUD_COMMENT: {"comments": []}})

    assert "[image]" in run(server.site).outputs["issues"][0]["description"]


def test_an_issue_with_nothing_attached_says_so_with_an_empty_list(jira, run):
    server = jira(**{CLOUD_SEARCH: {"issues": [_issue("QA-1")], "isLast": True},
                     CLOUD_COMMENT: {"comments": []}})
    result = run(server.site)

    assert result.outputs["issues"][0]["attachments"] == []
    assert result.outputs["attachment_count"] == 0
    assert result.outputs["images"] == []


def test_an_unknown_attachment_setting_is_refused_before_the_run(jira):
    problems = registry.get("jira.issues").problems(
        {"site": "https://x.example.invalid", "email": "a@example.invalid",
         "token": "t", "user": "u", "attachments": "pictures"})
    assert any("attachments" in one for one in problems)


# ------------------------------------------------------------- one issue only
def test_the_first_title_comes_back_beside_the_first_key(jira, run):
    """A cycle's subject wants a name as well as an identity."""
    server = jira(**{CLOUD_SEARCH: {"issues": [_issue("QA-1", "Login fails"),
                                               _issue("QA-2", "Other")],
                                    "isLast": True},
                     CLOUD_COMMENT: {"comments": []}})
    assert run(server.site).outputs["title"] == "Login fails"


def test_nothing_found_leaves_the_title_empty_rather_than_missing(jira, run):
    server = jira(**{CLOUD_SEARCH: {"issues": [], "isLast": True}})
    assert run(server.site).outputs["title"] == ""


def test_one_issue_asks_for_exactly_it_and_not_the_queue(jira, run):
    """Pinning a subject: whoever it belongs to, whatever its status."""
    server = jira(**{CLOUD_SEARCH: {"issues": [_issue("QA-7")], "isLast": True},
                     CLOUD_COMMENT: {"comments": []}})
    result = run(server.site, issue="QA-7", jql='status = "In Progress"',
                 project="WEB")

    assert result.ok, result.message
    assert server.seen[0][2]["jql"] == 'key = "QA-7"'
    assert result.outputs["key"] == "QA-7"


def test_an_empty_issue_means_the_queue_as_before(jira, run):
    server = jira(**{CLOUD_SEARCH: {"issues": [], "isLast": True}})
    run(server.site, issue="", project="QA")

    assert 'project = "QA"' in server.seen[0][2]["jql"]


def test_an_issue_that_is_not_a_key_is_refused_before_anything_is_asked():
    problems = registry.get("jira.issues").problems({
        "site": "https://example.invalid", "email": "me@example.invalid",
        "token": "t", "user": "u", "issue": 'QA-1" OR project = X'})
    assert any("issue must be an issue key" in one for one in problems)
