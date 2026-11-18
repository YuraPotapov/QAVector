"""An agent's change to Jira waits for the person (QCR ADR-007 D7), the window's side.

* The host says a change waits (``integration_approval``); QcrHost keeps it and
  follows its execution to its end.
* Its card is in the agent's conversation - no other - where it happened among
  the conversation's events, and on the Integrations
  page's Agent changes tab; answering in either is one answer.
* A comment's text can be changed before Approve, and the change goes to the
  host; nothing else can be changed. An empty comment is not approved.
"""

import json
import os
import types

import pytest

from cms_gui import qcrhost
from cms_gui.services import ServiceSupervisor

CLAUDE = {"id": "claude", "name": "Claude", "kind": "claude", "qavector_access": {}}
COMMENT = {"type": "integration_approval", "execution_id": "e-1", "actor": "agent:claude/s-1",
           "capability": "integrations.jira.team.comment", "editable": ["body"],
           "card": {"operation": "comment", "key": "QA-142", "summary": "Session expires",
                    "title": "Comment on QA-142", "changes": [], "notes": [],
                    "edit": {"body": {"label": "Comment", "value": "Draft from the agent", "multiline": True,
                                      "required": True}},
                    "connection": "team", "connection_name": "Product team"}}
MOVE = {"type": "integration_approval", "execution_id": "e-2", "actor": "agent:claude/s-2",
        "capability": "integrations.jira.team.transition", "editable": [],
        "card": {"operation": "transition", "key": "QA-142", "summary": "Session expires",
                 "title": "Move QA-142", "changes": [{"label": "Status", "from": "In Progress", "to": "Review"}],
                 "notes": ["By the transition Send to review."], "edit": {},
                 "connection": "team", "connection_name": "Product team"}}
EDIT = {"type": "integration_approval", "execution_id": "e-4", "actor": "agent:claude/s-1",
        "capability": "integrations.jira.team.edit", "editable": ["fields.summary"],
        "card": {"operation": "edit", "key": "QA-142", "summary": "Session expires", "title": "Edit QA-142",
                 "changes": [{"label": "Priority", "from": "High", "to": "Medium"}], "notes": [],
                 "edit": {"fields.summary": {"label": "Summary", "value": "Session ends", "was": "Session expires",
                                             "multiline": False, "required": True}},
                 "connection": "team", "connection_name": "Product team"}}


def ready_host(tmp_path):
    host = qcrhost.QcrHost(ServiceSupervisor())
    host.state, host.socket_path = qcrhost.READY, str(tmp_path / "qv" / "s.sock")
    os.makedirs(tmp_path / "qv", mode=0o700)
    host.sent = []
    host._proc = types.SimpleNamespace(write=lambda data: host.sent.append(json.loads(bytes(data))))
    return host


def execution(execution_id, state, **more):
    return {"type": "execution", "execution": dict({"execution_id": execution_id, "state": state,
                                                    "capability_id": "integrations.jira.team.comment",
                                                    "actor_id": "agent:claude/s-1"}, **more)}


SESSION = {"id": "s-1", "connection": "claude", "folder": "/tmp", "mode": "edit", "others": "deny",
           "source": {"label": "Chat"}, "turns": []}


@pytest.fixture
def chat(qapp, tmp_path):
    from cms_gui.pages import agentchat
    from cms_gui.settings import Settings
    host = ready_host(tmp_path)
    previous = agentchat.ChatTab.qcr_access
    agentchat.ChatTab.qcr_access = host
    tab = agentchat.ChatTab(Settings())
    tab.core = lambda: object()
    tab.set_connections([CLAUDE])
    host.integration_changes_changed.connect(tab.integration_changes_changed)
    tab._show(dict(SESSION), [], whole=True)
    yield tab, host, agentchat
    agentchat.ChatTab.qcr_access = previous
    tab.close()


def _cards(tab, agentchat):
    return tab.feed.change_cards()


def test_a_comment_waits_in_its_own_conversation_and_goes_as_the_person_left_it(chat):
    tab, host, agentchat = chat
    host._take(dict(COMMENT))
    host._take(dict(MOVE))                     # another conversation's: not here
    (card,) = _cards(tab, agentchat)
    assert card.kind.text() == "COMMENT ON QA-142" and card.meta.text() == "waits for you"
    assert card.where.text() == "Session expires · Jira: Product team"
    assert card.text_of("body") == "Draft from the agent" and not card.fields["body"].isReadOnly()
    card.fields["body"].setPlainText("Reviewed text")
    card.approve_button.click()
    [answer] = [one for one in host.sent if one["type"] == "integration_answer"]
    assert answer["execution_id"] == "e-1" and answer["approve"] is True
    assert answer["changes"] == {"body": "Reviewed text"} and answer["by"] == "you"
    (card,) = _cards(tab, agentchat)
    assert card.meta.text() == "approved - sending" and not card.approve_button.isVisibleTo(tab)
    assert card.fields["body"].isReadOnly()

    host._take(execution("e-1", "running"))
    host._take(execution("e-1", "succeeded", output={"summary": "QA-142: comment"}))
    (same,) = _cards(tab, agentchat)
    assert same is card, "a card is told how it stands, not rebuilt"
    assert card.meta.text() == "done" and card.result.text() == "QA-142: comment"
    # In the conversation an ended change stays as its record.
    assert not card.dismiss_button.isVisibleTo(tab)
    assert not card.fields["body"].isVisibleTo(tab) and card.shown["body"].text() == "Reviewed text"


def test_a_card_stands_where_it_happened_and_a_running_tool_below_it_still_ends(chat):
    tab, host, agentchat = chat
    events = [{"type": "user", "text": "Comment on QA-142", "at": 100.0, "seq": 1},
              {"type": "text", "text": "Proposing it.", "at": 101.0, "seq": 2},
              {"type": "tool.started", "id": "t1", "name": "qavector_invoke", "at": 103.0, "seq": 3},
              {"type": "user", "text": "And move it", "at": 200.0, "seq": 4}]
    tab._show(dict(SESSION), events, whole=True)
    host._take(dict(COMMENT))
    host.integration_changes["e-1"]["at"] = 102.0          # proposed during the first turn
    tab._show(dict(SESSION), events, whole=True)
    assert tab.feed.kinds() == ["you", "answer", "change", "tool", "you"]
    assert tab.feed.said()[2] == "COMMENT ON QA-142"
    tab.feed.append({"type": "tool.completed", "id": "t1", "ok": True})
    assert tab.feed.kinds() == ["you", "answer", "change", "tool", "you"]
    assert tab.feed._rows[3][1].stage["status"] == "done"


def test_a_delegation_card_and_a_reply_stand_in_the_conversation_by_time(chat):
    tab, host, agentchat = chat
    events = [{"type": "user", "text": "Ask Codex", "at": 100.0, "seq": 1},
              {"type": "text", "text": "Asking.", "at": 101.0, "seq": 2},
              {"type": "user", "text": "Thanks", "at": 300.0, "seq": 3}]
    tab._show(dict(SESSION), events, whole=True)
    tab.agent_request({"execution_id": "e-9", "session": "s-1", "target": "codex", "target_name": "Codex",
                       "goal": "Check the port", "at": 102.0})
    tab._inbox = [{"message_id": "m-1", "kind": "reply", "sender_connection": "codex", "body": "8070",
                   "created_at": 200.0}]
    tab._sync_others()
    assert tab.feed.kinds() == ["you", "answer", "request", "inbox", "you"]


def test_an_unchanged_comment_sends_no_changes_and_an_empty_one_is_not_approved(chat):
    tab, host, agentchat = chat
    host._take(dict(COMMENT))
    (card,) = _cards(tab, agentchat)
    card.fields["body"].setPlainText("   ")
    card.approve_button.click()
    assert [one for one in host.sent if one["type"] == "integration_answer"] == []
    assert card.result.text() == "Comment cannot be empty."
    card.fields["body"].setPlainText("Draft from the agent")
    card.approve_button.click()
    [answer] = [one for one in host.sent if one["type"] == "integration_answer"]
    assert answer["changes"] == {}


def test_a_denial_and_a_change_that_no_longer_waits(chat):
    tab, host, agentchat = chat
    host._take(dict(COMMENT))
    (card,) = _cards(tab, agentchat)
    card.deny_button.click()
    [answer] = [one for one in host.sent if one["type"] == "integration_answer"]
    assert answer["approve"] is False and answer["changes"] == {}
    host._take(execution("e-1", "failed", error={"code": "denied", "message": "Denied by you."}))
    (card,) = _cards(tab, agentchat)
    assert card.meta.text() == "denied" and card.result.text() == ""

    host._take(dict(COMMENT, execution_id="e-3"))
    card = next(one for one in _cards(tab, agentchat) if one.execution_id == "e-3")
    card.approve_button.click()
    request = [one for one in host.sent if one.get("execution_id") == "e-3"][0]
    host._take({"type": "reply", "id": request["id"], "ok": False,
                "error": {"code": "not_found", "message": "No change waits for that answer."}})
    assert host.integration_changes["e-3"]["state"] == "failed"
    assert "nothing was changed" in host.integration_changes["e-3"]["detail"]


def test_a_core_that_stops_ends_what_waited_and_says_what_may_have_been_sent(chat):
    tab, host, agentchat = chat
    host._take(dict(COMMENT))
    host._take(dict(MOVE))
    host.answer_integration("e-2", True)
    host._abandon("The QCR host stopped unexpectedly.")
    assert host.integration_changes["e-1"]["state"] == "failed"
    assert "before you answered" in host.integration_changes["e-1"]["detail"]
    assert host.integration_changes["e-2"]["state"] == "unknown"


def test_the_agent_changes_tab_lists_every_conversation_s_changes_and_answers_them(qapp, tmp_path):
    from cms_gui.pages.integrations import IntegrationsPage
    from cms_gui.settings import Settings
    host = ready_host(tmp_path)
    page = IntegrationsPage(Settings())
    try:
        page.set_change_source(host)
        tab = page.changes
        assert page.jira.tabs.tabText(page.jira.tabs.indexOf(tab)) == "Agent changes"
        assert tab.table.rowCount() == 0
        host._take(dict(COMMENT))
        host._take(dict(MOVE))
        assert page.jira.tabs.tabText(page.jira.tabs.indexOf(tab)) == "Agent changes (2)"
        assert tab.table.rowCount() == 2
        cells = [[tab.table.item(row, column).text() for column in range(6)] for row in range(2)]
        assert {row[2] for row in cells} == {"Comment on QA-142", "Move QA-142"}
        assert {row[1] for row in cells} == {"claude"} and {row[5] for row in cells} == {"waits for you"}
        tab.table.selectRow([row[2] for row in cells].index("Move QA-142"))
        assert [one.text() for one in tab.card.lines] == ["Status: from In Progress to Review"]
        assert tab.card.notes.text() == "By the transition Send to review."
        assert tab.card.fields == {} and "asked by claude" in tab.card.where.text()
        tab.card.approve_button.click()
        [answer] = [one for one in host.sent if one["type"] == "integration_answer"]
        assert answer["execution_id"] == "e-2" and answer["approve"] is True
        assert page.jira.tabs.tabText(page.jira.tabs.indexOf(tab)) == "Agent changes (1)"
    finally:
        page.shutdown()
        page.close()


def test_an_edit_card_shows_what_changes_and_sends_the_nested_field_the_person_changed(chat):
    tab, host, agentchat = chat
    host._take(dict(EDIT))
    (card,) = _cards(tab, agentchat)
    assert card.kind.text() == "EDIT QA-142"
    assert [one.text() for one in card.lines] == ["Priority: from High to Medium"]
    assert card.text_of("fields.summary") == "Session ends"
    card.fields["fields.summary"].setText("")
    card.approve_button.click()
    assert card.result.text() == "Summary cannot be empty."
    card.fields["fields.summary"].setText("Session ends too soon")
    card.approve_button.click()
    [answer] = [one for one in host.sent if one["type"] == "integration_answer"]
    assert answer["changes"] == {"fields.summary": "Session ends too soon"}


def test_a_change_names_its_agent_by_the_connection_s_name_once_a_turn_was_granted(qapp, tmp_path):
    host = ready_host(tmp_path)
    host.grant_agent(dict(CLAUDE, name="Claude at work"), "s-1", session={"folder": str(tmp_path)})
    host._take(dict(COMMENT))
    host._take(dict(MOVE, actor="agent:codex/s-2"))
    assert host.integration_changes["e-1"]["agent"] == "Claude at work"
    assert host.integration_changes["e-2"]["agent"] == "codex"


def test_only_the_provider_s_own_changes_are_listed(qapp, tmp_path):
    from cms_gui.pages.integrationchanges import IntegrationChangesTab
    host = ready_host(tmp_path)
    host._take(dict(COMMENT, capability="integrations.teams.qa.post"))
    tab = IntegrationChangesTab("jira")
    tab.set_source(host)
    assert tab.table.rowCount() == 0 and tab.title() == "Agent changes"


POST = {"type": "integration_approval", "execution_id": "e-9", "actor": "agent:claude/s-1",
        "capability": "integrations.teams.release.post", "editable": ["text", "title"],
        "card": {"operation": "post", "key": "Release 1.4", "summary": "",
                 "title": "Post to Release", "changes": [],
                 "notes": ["A post cannot be taken back. It goes to Release (channel)."],
                 "edit": {"title": {"label": "Title", "value": "Release 1.4", "was": "", "multiline": False},
                          "text": {"label": "Text", "value": "Out today", "was": "", "multiline": True,
                                   "required": True}},
                 "connection": "release", "connection_name": "Release"}}


def test_a_post_an_agent_wants_to_send_waits_on_teams_own_page_not_jira_s(qapp, tmp_path):
    """ADR-015 D6: Teams' Waiting tab, after New post; the catalog counts it too."""
    from cms_gui.pages.integrations import IntegrationsPage
    from cms_gui.settings import Settings
    host = ready_host(tmp_path)
    page = IntegrationsPage(Settings())
    try:
        page.set_change_source(host)
        waiting = page.teams_changes
        tabs = page.teams.tabs
        assert tabs.indexOf(waiting) == 1 and tabs.tabText(1) == "Waiting"
        assert waiting.table.horizontalHeaderItem(3).text() == "Post"
        host._take(dict(POST))
        assert tabs.tabText(1) == "Waiting (1)"
        assert page.changes.table.rowCount() == 0          # nothing of it on Jira's page
        assert waiting.table.rowCount() == 1
        assert page.tiles["waiting"].value.text() == "1"
        assert waiting.table.item(0, 3).text() == "Release 1.4"
        assert waiting.table.item(0, 4).text() == "Release"
        waiting.table.selectRow(0)
        assert "Teams: Release" in waiting.card.where.text() and "Jira" not in waiting.card.where.text()
        waiting.card.fields["text"].setPlainText("Out today, see the notes")
        waiting.card.approve_button.click()
        [answer] = [one for one in host.sent if one["type"] == "integration_answer"]
        assert answer["execution_id"] == "e-9" and answer["approve"] is True
        assert answer["changes"]["text"] == "Out today, see the notes"
        assert tabs.tabText(1) == "Waiting"
    finally:
        page.shutdown()
        page.close()
