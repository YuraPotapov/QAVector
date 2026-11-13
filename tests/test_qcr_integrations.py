"""Integration reads through QCR: why a read failed, and what is never shown.

A caller - an agent above all - must be able to tell an issue outside the
connection's scope from an expired token or a dead network, without any of the
underlying error's words (which can name the site, the token or the issue)
reaching it. And the site address is no secret: issue links stay links.
"""

import base64
import json
import os
import sys

import pytest

from integrations import reader as reader_mod, store
from integrations.contracts import READ_ERRORS, OperationContract, Provider, ReadError
from integrations.jira import capabilities as jira_caps
from integrations.jira.client import JiraError
from qcr import Authority, Grant, QCRError, Registry
from qcr.adapters.integrations import IntegrationAdapter

sys.path.insert(0, os.path.dirname(__file__))
from fake_jira import FakeJira  # noqa: E402

TOKEN = "tok-SECRET-123"
SITE = "https://jira.example.invalid"
GATEWAY = "https://gateway.example.invalid/ex/jira/cloud-1"
ENTRY = {"id": "team", "provider": "fake", "name": "Team"}


def contract(name="item"):
    return OperationContract(
        name, "Read one item.",
        {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"],
         "additionalProperties": False},
        {"type": "object", "properties": {"url": {"type": "string"}, "note": {"type": "string"}},
         "required": ["url", "note"], "additionalProperties": False})


def fake_provider(behaviour):
    def read(name, connection, token, request):
        return behaviour(request)
    return Provider("fake", {"item": contract()}, {"item": lambda *a: None}, read)


@pytest.fixture
def connection(monkeypatch):
    monkeypatch.setattr(store, "get", lambda connection_id, path=None: dict(ENTRY)
                        if connection_id == "team" else None)
    monkeypatch.setattr(store, "credentials", lambda *a, **k: dict(
        ENTRY, token=TOKEN, site=SITE, api=GATEWAY, email="me@example.invalid", scope=""))


def reader_with(behaviour, tmp_path):
    return reader_mod.ProjectReader(str(tmp_path / "integrations.json"), str(tmp_path / "s.json"),
                                    str(tmp_path / "e.json"), providers={"fake": fake_provider(behaviour)})


def failing(exc):
    def behaviour(request):
        raise exc
    return behaviour


@pytest.mark.parametrize("exc, code", [
    (JiraError("%s answered 401 for %s" % (SITE, TOKEN), 401), "auth"),
    (JiraError("forbidden", 403), "auth"),
    (JiraError("Issue QA-1 does not exist on %s" % SITE, 404), "not_found"),
    (JiraError("slow down", 429), "rate_limited"),
    (JiraError("bad JQL near " + TOKEN, 400), "invalid_request"),
    (JiraError("Jira answered 502", 502), "service_error"),
    (JiraError("Cannot reach %s: refused" % SITE, 0), "network"),
    (ConnectionResetError("reset"), "network"),
    (store.Unavailable("Connection 'Team' has no token.", "no_token"), "no_token"),
    (store.Unavailable("Connection 'Team': expired", "signin"), "signin"),
    (store.Unavailable("Connection 'Team': variable missing", "scope"), "scope_invalid"),
    (ReadError("out_of_scope"), "out_of_scope"),
    (RuntimeError("something odd with " + TOKEN), "failed"),
])
def test_a_failed_read_says_why_in_a_code_and_a_fixed_sentence(tmp_path, connection, exc, code):
    with pytest.raises(ReadError) as caught:
        reader_with(failing(exc), tmp_path).read("team", "fake", "item", {"key": "QA-1"})
    assert caught.value.code == code
    assert str(caught.value) == READ_ERRORS[code]
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_missing_connection_or_operation_is_named_as_such(tmp_path, connection):
    source = reader_with(lambda request: {"url": "", "note": ""}, tmp_path)
    with pytest.raises(ReadError) as missing:
        source.read("gone", "fake", "item", {"key": "QA-1"})
    with pytest.raises(ReadError) as unknown:
        source.read("team", "fake", "delete", {"key": "QA-1"})
    with pytest.raises(ReadError) as invalid:
        source.read("team", "fake", "item", {"key": 5})
    assert (missing.value.code, unknown.value.code, invalid.value.code) == (
        "unavailable", "unsupported", "invalid_request")


def test_an_answer_outside_the_contract_is_not_passed_on(tmp_path, connection):
    with pytest.raises(ReadError) as caught:
        reader_with(lambda request: {"url": SITE}, tmp_path).read("team", "fake", "item", {"key": "QA-1"})
    assert caught.value.code == "invalid_response"


def test_the_site_stays_a_link_and_the_token_and_gateway_do_not(tmp_path, connection):
    answer = {"url": SITE + "/browse/QA-1",
              "note": "token %s via %s" % (TOKEN, GATEWAY)}
    result = reader_with(lambda request: answer, tmp_path).read("team", "fake", "item", {"key": "QA-1"})
    assert result["url"] == SITE + "/browse/QA-1"
    assert TOKEN not in result["note"] and GATEWAY not in result["note"]


def test_unknown_codes_fall_back_to_failed():
    error = ReadError("Some sentence with a secret")
    assert error.code == "failed" and str(error) == READ_ERRORS["failed"]


def test_the_code_reaches_a_qcr_caller_through_the_registry(tmp_path, connection):
    source = reader_with(failing(ReadError("out_of_scope")), tmp_path)
    source.connections = lambda: [dict(ENTRY)]
    authority = Authority()
    caller = authority.issue("agent", [Grant("p", "integrations.fake.read"),
                                       Grant("p", "integrations.fake.item")])
    registry = Registry(authority)
    IntegrationAdapter(registry, "p", source).refresh()
    with pytest.raises(QCRError) as caught:
        registry.read(caller, "p", "integrations.fake.team.item", {"key": "QA-1"})
    assert caught.value.code == "out_of_scope"
    assert str(caught.value) == READ_ERRORS["out_of_scope"]


def test_jira_refuses_an_issue_outside_the_connection_scope(monkeypatch):
    monkeypatch.setattr(jira_caps.query, "page", lambda *a, **k: ([{"key": "QA-2"}], None))
    called = []
    monkeypatch.setattr(jira_caps.operations, "call", lambda *a, **k: called.append(a))
    connection = {"scope": "project = QA", "flavour": "cloud", "site": SITE}
    with pytest.raises(ReadError) as caught:
        jira_caps.read("issue", connection, TOKEN, {"key": "QA-1"}, get=object())
    assert caught.value.code == "out_of_scope" and not called


# -- end to end: the project's own files, the real Jira client, a loopback Jira ---
EMAIL = "you@yourcompany.example"


def _raw_issue(key="QA-142"):
    return {"key": key, "fields": {
        "summary": "Session expires", "updated": "2026-11-05T10:00:00.000+0000",
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "issuetype": {"name": "Bug"}, "priority": {"name": "High"},
        "project": {"key": "QA", "name": "Quality"}, "description": None,
        "reporter": {"displayName": "Reporter R"}, "created": "2026-11-01T09:00:00.000+0000"},
        "transitions": []}


@pytest.fixture
def live(tmp_path):
    """A project with one Jira connection, a registry, and an agent allowed to read issues."""
    made = []

    def build(answers, grants=("integrations.jira.read", "integrations.jira.issue"), **settings):
        server = FakeJira(answers)
        made.append(server)
        paths = (str(tmp_path / "integrations.json"), str(tmp_path / "cyclesecrets.json"),
                 str(tmp_path / "environments.json"))
        entry, problems = store.upsert({"name": "Product team", "site": server.site, "email": EMAIL,
                                        "settings": settings}, paths[0], paths[1], new_token=TOKEN)
        assert not problems
        authority = Authority()
        caller = authority.issue("agent", [Grant("p", one) for one in grants])
        registry = Registry(authority)
        IntegrationAdapter(registry, "p", reader_mod.ProjectReader(*paths)).refresh()
        return server, registry, caller, entry["id"]

    yield build
    for server in made:
        server.close()


def test_an_agent_reads_an_issue_through_qcr_and_sees_no_token(live):
    server, registry, caller, cid = live({
        "/rest/api/3/issue/QA-142?": _raw_issue(),
        "/rest/api/3/issue/QA-142/comment": {"total": 0, "comments": []}})
    ids = [cap.capability_id for cap in registry.list_capabilities(caller, "p", namespace="integrations")]
    assert ids == ["integrations.jira.%s.issue" % cid]
    issue = registry.read(caller, "p", "integrations.jira.%s.issue" % cid, {"key": "QA-142"})["issue"]
    assert issue["summary"] == "Session expires" and issue["url"] == server.site + "/browse/QA-142"
    basic = base64.b64encode(("%s:%s" % (EMAIL, TOKEN)).encode()).decode()
    assert server.seen[0].authorization == "Basic " + basic
    assert TOKEN not in json.dumps(issue) and basic not in json.dumps(issue)


def test_a_refused_token_reaches_the_agent_as_auth_without_the_site(live):
    server, registry, caller, cid = live({"/rest/api/3/issue/QA-142?": (401, {"errorMessages": ["no"]})})
    with pytest.raises(QCRError) as caught:
        registry.read(caller, "p", "integrations.jira.%s.issue" % cid, {"key": "QA-142"})
    assert caught.value.code == "auth"
    assert server.site not in str(caught.value) and "127.0.0.1" not in str(caught.value)


def test_an_issue_outside_the_predefined_query_is_never_fetched(live):
    server, registry, caller, cid = live({"/rest/api/3/search/jql": {"issues": []}},
                                         predefined_query="project = WEB")
    with pytest.raises(QCRError) as caught:
        registry.read(caller, "p", "integrations.jira.%s.issue" % cid, {"key": "QA-142"})
    assert caught.value.code == "out_of_scope"
    assert server.paths() == ["/rest/api/3/search/jql"]
    assert "project = WEB" in json.dumps(server.seen[0].body)


def test_without_the_operation_grant_nothing_is_asked(live):
    server, registry, caller, cid = live({}, grants=("integrations.jira.read",))
    with pytest.raises(QCRError):
        registry.read(caller, "p", "integrations.jira.%s.issue" % cid, {"key": "QA-142"})
    assert server.seen == []


def test_a_compiled_schema_checks_as_validate_output_does_and_ignores_later_changes():
    from qavector_sdk.validation import ContractError, compile_output, validate_output
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    check = compile_output(schema)
    schema["properties"]["n"]["type"] = "string"
    assert check({"n": 1}) == {"n": 1}
    with pytest.raises(ContractError) as compiled:
        check({"n": "x"})
    with pytest.raises(ContractError) as plain:
        validate_output({"n": "x"}, {"type": "object", "properties": {"n": {"type": "integer"}}})
    assert (compiled.value.code, str(compiled.value)) == (plain.value.code, str(plain.value))
    with pytest.raises(ContractError):
        compile_output({"type": "no-such-type"})
