"""Jira changes an agent proposes and a person confirms (QCR ADR-007).

End to end with the project's own files, the real Jira client, the real Engine
and journal, and a loopback Jira: nothing is sent to Jira before Approve, the
version written is the one approved, a denied change is never made, and the
token is nowhere an agent, a card, the journal or the activity log can show it.
"""

import json
import os
import sys

import pytest

from integrations import activity, reader as reader_mod, store
from integrations.contracts import WRITE_ERRORS, WriteError
from integrations.jira import capabilities as jira_caps, writes
from integrations.jira.client import JiraError
from qcr import Authority, Grant, QCRError, Registry
from qcr.adapters.integrations import IntegrationAdapter
from qcr.engine import Engine
from qcr.executions import ExecutionStore

sys.path.insert(0, os.path.dirname(__file__))
from fake_jira import FakeJira  # noqa: E402

TOKEN = "tok-SECRET-123"
EMAIL = "you@yourcompany.example"
from integrations.agents.store import ACCESS_GRANTS  # noqa: E402

WRITE = ACCESS_GRANTS["jira_write"]
OFFERED = [{"id": "21", "name": "Send to review", "to": {"name": "Review", "statusCategory": {"key": "indeterminate"}}},
           {"id": "31", "name": "Done", "to": {"name": "Done", "statusCategory": {"key": "done"}}}]


def _raw_issue(status="In Progress", transitions=OFFERED):
    return {"key": "QA-142", "fields": {
        "summary": "Session expires", "updated": "2026-11-05T10:00:00.000+0000",
        "status": {"name": status, "statusCategory": {"key": "indeterminate"}},
        "issuetype": {"name": "Bug"}, "priority": {"name": "High"},
        "project": {"key": "QA", "name": "Quality"}, "description": None,
        "reporter": {"displayName": "Reporter R"}, "created": "2026-11-01T09:00:00.000+0000"},
        "transitions": transitions}


def _answers(status="In Progress", **more):
    answers = {"/rest/api/3/issue/QA-142?": _raw_issue(status),
               "GET /rest/api/3/issue/QA-142/comment": {"total": 0, "comments": []},
               "POST /rest/api/3/issue/QA-142/comment": (201, {"id": "1001"}),
               "GET /rest/api/3/issue/QA-142/transitions": {"transitions": OFFERED},
               "POST /rest/api/3/issue/QA-142/transitions": (204, None)}
    answers.update(more)
    return answers


class World:
    def __init__(self, tmp_path, answers, grants, settings):
        self.server = FakeJira(answers)
        self.paths = (str(tmp_path / "integrations.json"), str(tmp_path / "cyclesecrets.json"),
                      str(tmp_path / "environments.json"))
        entry, problems = store.upsert({"name": "Product team", "site": self.server.site, "email": EMAIL,
                                        "settings": settings}, self.paths[0], self.paths[1], new_token=TOKEN)
        assert not problems
        self.cid = entry["id"]
        authority = Authority()
        self.agent = authority.issue("agent:claude/s1", [Grant("p", one) for one in grants])
        self.registry = Registry(authority)
        self.store = ExecutionStore(str(tmp_path / "executions.sqlite"))
        self.engine = Engine(self.registry, self.store)
        self.told = []
        self.folder = tmp_path / "work"
        self.folder.mkdir()
        self.adapter = IntegrationAdapter(self.registry, "p", reader_mod.ProjectReader(*self.paths),
                                          notify=self.told.append, label=lambda actor: "Agent Claude",
                                          folder=lambda actor: str(self.folder), spawn=lambda work: work())
        self.adapter.refresh()

    def capability(self, op):
        return "integrations.jira.%s.%s" % (self.cid, op)

    def invoke(self, op, request, **extra):
        return self.engine.invoke(self.agent, "p", self.capability(op), request, **extra)

    def get(self, execution_id):
        return self.engine.get(self.agent, execution_id)

    def posted(self):
        """What changed something: every write, not a search (Cloud searches by POST)."""
        return [one for one in self.server.seen if one.method in ("POST", "PUT", "DELETE")
                and "/search" not in one.path]

    def activity(self):
        return activity.read(self.paths[0])

    def close(self):
        self.engine.close()
        self.store.close()
        self.server.close()


@pytest.fixture
def world(tmp_path):
    made = []

    def build(answers=None, grants=WRITE, **settings):
        folder = tmp_path / str(len(made))
        folder.mkdir()
        made.append(World(folder, _answers() if answers is None else answers, grants, settings))
        return made[-1]

    yield build
    for one in made:
        one.close()


def test_a_comment_waits_for_the_person_and_the_edited_version_is_written(world):
    w = world()
    execution = w.invoke("comment", {"key": "qa-142", "body": "Draft from the agent"})
    assert execution["state"] == "dispatched" and w.posted() == []
    [told] = w.told
    assert told["type"] == "integration_approval" and told["execution_id"] == execution["execution_id"]
    assert told["actor"] == "agent:claude/s1" and told["editable"] == ["body"]
    assert told["card"] == {"operation": "comment", "key": "QA-142", "summary": "Session expires",
                            "title": "Comment on QA-142", "changes": [], "notes": [],
                            "edit": {"body": {"label": "Comment", "value": "Draft from the agent",
                                              "multiline": True, "required": True}},
                            "connection": w.cid, "connection_name": "Product team"}

    assert w.adapter.writer.answer(execution["execution_id"], True, "you", {"body": "Reviewed text",
                                                                            "key": "QA-1"})
    done = w.get(execution["execution_id"])
    assert done["state"] == "succeeded", done
    assert done["output"]["changed"] is True and done["output"]["summary"] == "QA-142: comment"
    assert done["output"]["issue"]["key"] == "QA-142"
    [post] = w.posted()
    assert post.path == "/rest/api/3/issue/QA-142/comment"         # the key cannot be edited
    assert "Reviewed text" in json.dumps(post.body) and "Draft" not in json.dumps(post.body)
    events = w.engine.subscribe(w.agent, execution["execution_id"])["events"]
    answered = [one for one in events if one["type"] == "approval.answered"]
    assert answered[0]["payload"] == {"approved": True, "by": "you", "edited": True}

    [line] = w.activity()
    assert line["op"] == "comment" and line["target"] == "QA-142" and line["ok"] is True
    assert line["source"] == {"label": "Agent Claude", "detail": execution["execution_id"]}
    assert "Reviewed" not in json.dumps(line)
    everything = json.dumps([w.told, done, events, w.activity()])
    assert TOKEN not in everything


def test_a_denied_comment_is_never_sent_and_not_logged(world):
    w = world()
    execution = w.invoke("comment", {"key": "QA-142", "body": "Draft"})
    assert w.adapter.writer.answer(execution["execution_id"], False, "you")
    done = w.get(execution["execution_id"])
    assert done["state"] == "failed" and done["error"]["code"] == "denied"
    assert w.posted() == [] and w.activity() == []
    assert not w.adapter.writer.answer(execution["execution_id"], True, "you")


def test_a_transition_by_status_name_shows_from_and_to_and_sends_the_transition_id(world):
    w = world()
    execution = w.invoke("transition", {"key": "QA-142", "to": "review"})
    [told] = w.told
    assert told["card"]["changes"] == [{"label": "Status", "from": "In Progress", "to": "Review"}]
    assert told["card"]["notes"] == ["By the transition Send to review."] and told["editable"] == []
    w.adapter.writer.answer(execution["execution_id"], True, "you", {"to": "31"})   # not editable
    assert w.get(execution["execution_id"])["state"] == "succeeded"
    [post] = w.posted()
    assert post.path == "/rest/api/3/issue/QA-142/transitions" and post.body == {"transition": {"id": "21"}}


def test_a_transition_to_where_the_issue_already_is_changes_nothing_and_asks_nobody(world):
    w = world(_answers(status="Review"))
    execution = w.invoke("transition", {"key": "QA-142", "to": "Review"})
    assert execution["state"] == "succeeded"
    assert execution["output"]["changed"] is False and execution["output"]["summary"] == "QA-142 is already in Review"
    assert w.told == [] and w.posted() == [] and w.activity() == []


def test_a_transition_the_issue_does_not_offer_names_the_ones_it_does(world):
    w = world()
    execution = w.invoke("transition", {"key": "QA-142", "to": "Closed"})
    assert execution["state"] == "failed" and execution["error"]["code"] == "not_offered"
    assert "Send to review (id 21, to Review)" in execution["error"]["message"]
    assert w.told == [] and w.posted() == []


def test_a_screen_that_asks_for_fields_says_which(world):
    offered = [dict(OFFERED[1], fields={"resolution": {"required": True, "name": "Resolution",
                                                       "allowedValues": [{"id": "1", "name": "Fixed"}]}})]
    w = world(_answers(**{"GET /rest/api/3/issue/QA-142/transitions": {"transitions": offered}}))
    execution = w.invoke("transition", {"key": "QA-142", "to": "31"})
    w.adapter.writer.answer(execution["execution_id"], True, "you")
    done = w.get(execution["execution_id"])
    assert done["error"]["code"] == "fields_required" and "Resolution" in done["error"]["message"]
    assert w.posted() == [] and w.activity() == []      # refused before Jira changed anything


def test_no_answer_after_the_change_was_sent_is_an_unknown_outcome(world, monkeypatch):
    def lost(get, connection, request):
        raise JiraError("Cannot reach %s: timed out (token %s)" % ("site", TOKEN), 0)
    monkeypatch.setitem(writes.OPERATIONS, "comment", lost)
    w = world()
    execution = w.invoke("comment", {"key": "QA-142", "body": "Draft"})
    w.adapter.writer.answer(execution["execution_id"], True, "you")
    done = w.get(execution["execution_id"])
    assert done["error"] == {"code": "unknown_outcome", "message": WRITE_ERRORS["unknown_outcome"]}
    assert TOKEN not in json.dumps([done, w.activity()])


def test_jira_refusing_the_change_says_a_code_not_its_words(world):
    w = world(_answers(**{"POST /rest/api/3/issue/QA-142/comment": (403, {"errorMessages": ["secret words"]})}))
    execution = w.invoke("comment", {"key": "QA-142", "body": "Draft"})
    w.adapter.writer.answer(execution["execution_id"], True, "you")
    done = w.get(execution["execution_id"])
    assert done["error"]["code"] == "auth" and "secret words" not in json.dumps(done)
    [line] = w.activity()
    assert line["ok"] is False and line["summary"] == WRITE_ERRORS["auth"]


def test_an_issue_outside_the_predefined_query_is_refused_before_anything_is_shown(world):
    w = world({"/rest/api/3/search/jql": {"issues": []}}, predefined_query="project = WEB")
    execution = w.invoke("comment", {"key": "QA-142", "body": "Draft"})
    assert execution["state"] == "failed" and execution["error"]["code"] == "out_of_scope"
    assert w.told == [] and w.posted() == []


def test_one_change_per_connection_at_a_time_and_the_same_key_is_the_same_change(world):
    w = world()
    first = w.invoke("comment", {"key": "QA-142", "body": "Draft"}, idempotency_key="k1")
    again = w.invoke("comment", {"key": "QA-142", "body": "Draft"}, idempotency_key="k1")
    assert again["execution_id"] == first["execution_id"] and len(w.told) == 1
    with pytest.raises(QCRError) as busy:
        w.invoke("transition", {"key": "QA-142", "to": "Review"})
    assert busy.value.code == "busy"


def test_a_canceled_or_closed_wait_drops_the_change(world):
    w = world()
    first = w.invoke("comment", {"key": "QA-142", "body": "Draft"})
    w.engine.cancel(w.agent, first["execution_id"])
    assert not w.adapter.writer.answer(first["execution_id"], True, "you")
    second = w.invoke("comment", {"key": "QA-142", "body": "Draft"})
    w.adapter.writer.close()
    assert w.get(second["execution_id"])["error"]["code"] == "source_unavailable"
    assert w.posted() == []


def test_writes_and_reads_are_separate_grants(world):
    reader = world(grants=("integrations.jira.read", "integrations.jira.issue"))
    ids = [cap.capability_id for cap in reader.registry.list_capabilities(reader.agent, "p")]
    assert ids == [reader.capability("issue")]
    with pytest.raises(QCRError):
        reader.invoke("comment", {"key": "QA-142", "body": "Draft"})
    writer = world()
    ids = sorted(cap.capability_id for cap in writer.registry.list_capabilities(writer.agent, "p"))
    assert ids == sorted(writer.capability(op) for op in ("assign", "attach", "comment", "comment_edit",
                                                            "create", "edit", "transition"))
    with pytest.raises(QCRError):
        writer.registry.read(writer.agent, "p", writer.capability("issue"), {"key": "QA-142"})


def test_deleting_a_comment_is_not_offered_to_agents():
    contracts = jira_caps.provider().contracts
    assert {name for name, one in contracts.items() if one.access == "write"} == {
        "comment", "transition", "assign", "edit", "comment_edit", "create", "attach"}
    assert "comment_delete" not in contracts


def test_a_write_error_keeps_only_sentences_of_its_own_making():
    assert str(WriteError("auth", "Jira said: token xyz")) == WRITE_ERRORS["auth"]
    assert str(WriteError("refused", "QA-1 cannot be moved that way now.")) == "QA-1 cannot be moved that way now."
    assert WriteError("nonsense").code == "failed"


# -- 7.3: assign, edit, comment_edit, create, attach ------------------------------
PEOPLE = [{"accountId": "acc-1", "displayName": "Dana Tester"}, {"accountId": "acc-2", "displayName": "Dan Other"}]
EDITMETA = {"fields": {"summary": {}, "description": {}, "labels": {},
                       "priority": {"allowedValues": [{"id": "2", "name": "High"}, {"id": "3", "name": "Medium"}]}}}


def _doc(text):
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _approve(w, execution, changes=None):
    assert w.adapter.writer.answer(execution["execution_id"], True, "you", changes or {})
    return w.get(execution["execution_id"])


def test_assign_finds_the_person_by_name_and_shows_from_whom_to_whom(world):
    w = world(_answers(**{"/rest/api/3/user/assignable/search": PEOPLE[:1],
                          "PUT /rest/api/3/issue/QA-142/assignee": (204, None)}))
    execution = w.invoke("assign", {"key": "QA-142", "account": "dana tester"})
    [told] = w.told
    assert told["card"]["title"] == "Assign QA-142"
    assert told["card"]["changes"] == [{"label": "Assignee", "from": "nobody", "to": "Dana Tester"}]
    assert _approve(w, execution)["state"] == "succeeded"
    [put] = w.posted()
    assert put.path == "/rest/api/3/issue/QA-142/assignee" and put.body == {"accountId": "acc-1"}


def test_assign_names_everybody_when_several_match_and_sends_nothing(world):
    w = world(_answers(**{"/rest/api/3/user/assignable/search": PEOPLE}))
    execution = w.invoke("assign", {"key": "QA-142", "account": "Dan"})
    assert execution["error"]["code"] == "not_offered"
    assert "Dana Tester, Dan Other" in execution["error"]["message"] and w.posted() == []


def test_an_edit_shows_what_changes_lets_the_person_change_the_text_and_guards_the_issue(world):
    w = world(_answers(**{"/rest/api/3/issue/QA-142/editmeta": EDITMETA,
                          "PUT /rest/api/3/issue/QA-142": (204, None)}))
    execution = w.invoke("edit", {"key": "QA-142", "fields": {"summary": "Session expires too soon",
                                                              "priority": "medium", "labels": ["auth"]}})
    [told] = w.told
    card = told["card"]
    assert card["title"] == "Edit QA-142" and told["editable"] == ["fields.summary"]
    assert card["edit"]["fields.summary"]["was"] == "Session expires"
    assert card["changes"] == [{"label": "Labels", "from": "none", "to": "auth"},
                               {"label": "Priority", "from": "High", "to": "Medium"}]
    done = _approve(w, execution, {"fields.summary": "Session ends after a minute"})
    assert done["state"] == "succeeded", done
    put = next(one for one in w.posted() if one.method == "PUT")
    assert put.body == {"fields": {"summary": "Session ends after a minute", "labels": ["auth"],
                                   "priority": {"id": "3"}}}


def test_an_edit_that_changes_nothing_asks_nobody_and_a_closed_field_is_refused(world):
    w = world(_answers(**{"/rest/api/3/issue/QA-142/editmeta": EDITMETA}))
    same = w.invoke("edit", {"key": "QA-142", "fields": {"summary": "Session expires", "priority": "High"}})
    assert same["state"] == "succeeded" and same["output"]["changed"] is False and w.told == []
    w2 = world(_answers(**{"/rest/api/3/issue/QA-142/editmeta": {"fields": {"summary": {}}}}))
    closed = w2.invoke("edit", {"key": "QA-142", "fields": {"labels": ["x"]}})
    assert closed["error"]["code"] == "refused" and "labels" in closed["error"]["message"]


def test_an_edit_over_somebody_else_s_change_is_refused_not_written(world):
    raw = _raw_issue()
    seen = {"n": 0}

    def issue(request):
        seen["n"] += 1
        # Read for the card, then changed in Jira before the person approved.
        return dict(raw, fields=dict(raw["fields"], updated="2026-11-17T09:00:00.000+0000")) if seen["n"] > 1 else raw
    w = world(_answers(**{"/rest/api/3/issue/QA-142?": issue, "/rest/api/3/issue/QA-142/editmeta": EDITMETA}))
    execution = w.invoke("edit", {"key": "QA-142", "fields": {"summary": "New"}})
    done = _approve(w, execution)
    assert done["error"]["code"] == "conflict" and w.posted() == []


def test_a_comment_edit_shows_the_old_text_and_sends_the_new(world):
    comment = {"id": "1001", "body": _doc("Old text"), "author": {"displayName": "Dana Tester"},
               "created": "2026-11-10T10:00:00.000+0000", "updated": "2026-11-10T10:00:00.000+0000"}
    w = world(_answers(**{"GET /rest/api/3/issue/QA-142/comment/1001": comment,
                          "PUT /rest/api/3/issue/QA-142/comment/1001": (200, comment)}))
    execution = w.invoke("comment_edit", {"key": "QA-142", "id": "1001", "body": "New text"})
    [told] = w.told
    assert told["card"]["edit"]["body"]["was"] == "Old text"
    assert told["card"]["notes"] == ["A comment by Dana Tester."]
    assert _approve(w, execution)["state"] == "succeeded"
    put = next(one for one in w.posted() if one.method == "PUT")
    assert "New text" in json.dumps(put.body)


def _create_answers(**more):
    made = _raw_issue()
    made = dict(made, key="QA-200")
    return _answers(**dict({
        "/rest/api/3/issue/createmeta/QA/issuetypes/10001": {"fields": [
            {"fieldId": "summary", "name": "Summary", "required": True},
            {"fieldId": "priority", "name": "Priority", "required": False,
             "allowedValues": [{"id": "2", "name": "High"}]},
            {"fieldId": "components", "name": "Components", "required": True,
             "allowedValues": [{"id": "7", "name": "Web"}]}]},
        "/rest/api/3/issue/createmeta/QA/issuetypes": {"issueTypes": [{"id": "10001", "name": "Bug"}]},
        "POST /rest/api/3/issue": (201, {"key": "QA-200"}),
        "/rest/api/3/issue/QA-200?": made,
        "GET /rest/api/3/issue/QA-200/comment": {"total": 0, "comments": []}}, **more))


def test_create_asks_for_the_screen_s_required_fields_before_showing_anything(world):
    w = world(_create_answers())
    execution = w.invoke("create", {"project": "QA", "type": "bug", "summary": "Login fails"})
    assert execution["error"]["code"] == "fields_required" and "Components" in execution["error"]["message"]
    assert w.told == [] and w.posted() == []


def test_create_resolves_names_shows_the_new_issue_and_logs_its_key(world):
    w = world(_create_answers())
    execution = w.invoke("create", {"project": "QA", "type": "Bug", "summary": "Login fails",
                                    "priority": "high", "fields": {"components": "web"}})
    [told] = w.told
    assert told["card"]["title"] == "Create a Bug in QA" and told["editable"] == ["summary", "description"]
    assert told["card"]["notes"] == ["Components: Web"]
    done = _approve(w, execution, {"description": "Steps: open the login page."})
    assert done["state"] == "succeeded", done
    post = next(one for one in w.posted() if one.path == "/rest/api/3/issue")
    fields = post.body["fields"]
    assert fields["project"] == {"key": "QA"} and fields["issuetype"] == {"id": "10001"}
    assert fields["priority"] == {"id": "2"} and fields["components"] == {"id": "7"}
    assert "Steps" in json.dumps(fields["description"])
    [line] = w.activity()
    assert line["target"] == "QA-200" and line["op"] == "create"


def test_create_is_refused_in_a_project_outside_the_predefined_query(world):
    w = world(_create_answers(**{"/rest/api/3/search/jql": {"issues": []}}), predefined_query="project = WEB")
    execution = w.invoke("create", {"project": "QA", "type": "Bug", "summary": "Login fails"})
    assert execution["error"]["code"] == "out_of_scope" and w.told == []


def test_attach_takes_a_file_only_from_the_conversation_s_folder(world, tmp_path):
    w = world(_answers(**{"POST /rest/api/3/issue/QA-142/attachments": [{"id": "55", "filename": "log.txt"}]}))
    (w.folder / "log.txt").write_text("trace")
    outside = tmp_path / "secret.txt"
    outside.write_text("no")
    for path in (str(outside), "../secret.txt", str(w.folder)):
        refused = w.invoke("attach", {"key": "QA-142", "path": path})
        assert refused["error"]["code"] == "out_of_folder", path
    execution = w.invoke("attach", {"key": "QA-142", "path": "log.txt"})
    [told] = w.told
    assert told["card"]["changes"] == [{"label": "File", "from": "", "to": "log.txt (1 KB)"}]
    assert _approve(w, execution)["state"] == "succeeded"
    [post] = w.posted()
    assert post.path == "/rest/api/3/issue/QA-142/attachments" and b"trace" in post.body


def test_attach_without_a_folder_is_refused(world):
    w = world()
    w.adapter.writer._folder = lambda actor: None
    assert w.invoke("attach", {"key": "QA-142", "path": "log.txt"})["error"]["code"] == "out_of_folder"
