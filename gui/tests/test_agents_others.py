"""Agents working with other agents (ADR-006 D3), the window's side.

* QcrHost gives a turn the targets its connection allows, as grants on each
  target's resources, and what its conversation is (the policy) - nothing for
  Deny or a connection that allows nobody, and never the agent's own
  connection.
* The connection dialog says what its conversations start with and which
  other connections they may reach; a new connection asks each time and may
  reach every connection already here.
* The chat's footer says the conversation's mode, starts a new one from its
  connection's default, is off when the connection allows nobody, and a change
  in a begun conversation is saved.
"""

import json
import os
import types

import pytest

from cms_gui import qcrhost
from cms_gui.services import ServiceSupervisor

CLAUDE = {"id": "claude", "name": "Claude", "kind": "claude", "qavector_access": {},
          "others_default": "approve", "allowed_agents": ["codex", "claude"]}
CODEX = {"id": "codex", "name": "Codex", "kind": "codex", "qavector_access": {},
         "others_default": "deny", "allowed_agents": []}


def ready_host(tmp_path):
    host = qcrhost.QcrHost(ServiceSupervisor())
    host.state, host.socket_path = qcrhost.READY, str(tmp_path / "qv" / "s.sock")
    os.makedirs(tmp_path / "qv", mode=0o700)
    host.sent = []
    host._proc = types.SimpleNamespace(write=lambda data: host.sent.append(json.loads(bytes(data))))
    return host


def test_a_turn_gets_its_targets_and_its_conversation(qapp, tmp_path):
    host = ready_host(tmp_path)
    session = {"folder": str(tmp_path), "others": "auto", "restrictions": {"network": "on"}}
    access = host.grant_agent(CLAUDE, "s-1", session=session, mode="edit")
    assert access is not None, "other agents alone are enough for a token"
    actor = host.sent[0]
    assert actor["type"] == "actor" and actor["actor"] == "agent:claude/s-1"
    assert sorted((g["permission"], g["resource_id"]) for g in actor["grants"]) == [
        ("agents.delegate", "agent-connection~codex"), ("agents.delegate", "agent-tasks"),
        ("agents.message", "agent-messages~codex")]
    assert actor["agents"] == {"session": "s-1", "connection": "claude", "name": "Claude",
                               "folder": str(tmp_path), "mode": "edit", "others": "auto",
                               "restrictions": {"network": "on"}}
    host.end_agent(access)


@pytest.mark.parametrize("connection, others", [(CLAUDE, "deny"), (CLAUDE, None), (CODEX, "auto")])
def test_deny_or_nobody_allowed_gives_nothing(qapp, tmp_path, connection, others):
    host = ready_host(tmp_path)
    assert host.grant_agent(connection, "s-1", session={"folder": str(tmp_path), "others": others},
                            mode="full") is None
    assert host.sent == []


def test_the_host_hears_where_agents_live_once_ready_and_again_on_change(qapp, tmp_path):
    host = ready_host(tmp_path)
    host.set_agent_storage(str(tmp_path / "sessions"), str(tmp_path / "agents.json"))
    host.refresh_agents()
    sent = [one for one in host.sent if one["type"] == "agent_connections"]
    assert len(sent) == 2 and sent[0]["agents_path"] == str(tmp_path / "agents.json")
    host.answer_agent("e-1", True)
    assert host.sent[-1]["type"] == "agent_answer" and host.sent[-1]["approve"] is True
    seen = []
    host.agent_approval.connect(seen.append)
    host._take({"type": "agent_approval", "execution_id": "e-2", "goal": "Fix it"})
    assert seen == [{"type": "agent_approval", "execution_id": "e-2", "goal": "Fix it"}]


# -- the connection dialog --------------------------------------------------------------
def _dialog(qapp, connection=None, others=(CLAUDE, CODEX)):
    from cms_gui.pages.agents import ConnectionDialog
    return ConnectionDialog(None, connection, kinds=[{"kind": "claude", "name": "Claude Code"},
                                                     {"kind": "codex", "name": "Codex"}], others=others)


def test_a_new_connection_asks_each_time_and_reaches_every_other(qapp, dispose):
    dialog = _dialog(qapp)
    try:
        dialog.name.setText("Another")
        entry = dialog.entry()
        assert entry["others_default"] == "approve" and entry["allowed_agents"] == ["claude", "codex"]
        assert [box.text() for box in dialog.allowed.values()] == ["Claude (Claude Code)", "Codex (Codex)"]
    finally:
        dispose(dialog)


def test_an_existing_connection_keeps_its_choice_and_never_lists_itself(qapp, dispose):
    dialog = _dialog(qapp, CODEX)
    try:
        assert list(dialog.allowed) == ["claude"]
        assert dialog.others_default.currentData() == "deny"
        assert not dialog.allowed["claude"].isEnabled()           # Deny: nobody to tick
        dialog.others_default.setCurrentIndex(dialog.others_default.findData("auto"))
        assert dialog.allowed["claude"].isEnabled() and not dialog.allowed["claude"].isChecked()
        dialog.allowed["claude"].setChecked(True)
        assert dialog.entry()["allowed_agents"] == ["claude"] and dialog.entry()["others_default"] == "auto"
    finally:
        dispose(dialog)


def test_alone_says_there_is_nobody_yet(qapp, dispose):
    dialog = _dialog(qapp, None, others=())
    try:
        assert dialog.allowed == {} and dialog.entry()["allowed_agents"] == []
    finally:
        dispose(dialog)


# -- the chat's footer ------------------------------------------------------------------
def _composer(qapp):
    from cms_gui.pages.agentchat import Composer
    composer = Composer()
    composer.set_kinds([{"kind": "claude", "name": "Claude Code", "modes": ["read", "edit", "full"]},
                        {"kind": "codex", "name": "Codex", "modes": ["read", "edit", "full"]}])
    composer.set_connections([CLAUDE, CODEX])
    return composer


def test_a_new_conversation_starts_from_its_connections_default(qapp, dispose):
    composer = _composer(qapp)
    try:
        composer.set_session(None, {"connection": "claude"})
        assert composer.settings()["others"] == "approve"
        assert composer.others.button("Auto").isEnabled()
        composer.set_session(None, {"connection": "codex"})
        assert composer.settings()["others"] == "deny"
        assert not composer.others.button("Auto").isEnabled(), "Codex allows nobody"
    finally:
        dispose(composer)


def test_a_begun_conversation_shows_its_own_and_says_a_change(qapp, dispose):
    composer = _composer(qapp)
    heard = []
    composer.others_changed.connect(heard.append)
    try:
        composer.set_session({"id": "s", "connection": "claude", "folder": "/tmp", "others": "auto",
                              "source": {"label": "Chat"}})
        assert composer.settings()["others"] == "auto"
        composer.others.button("Deny").click()
        assert heard == ["deny"] and composer.settings()["others"] == "deny"
        # A conversation from before ADR-006 has no mode of its own: Deny, whatever the default.
        composer.set_session({"id": "old", "connection": "claude", "folder": "/tmp", "source": {"label": "Chat"}})
        assert composer.settings()["others"] == "approve"
    finally:
        dispose(composer)


# -- the chat's cards: requests and the inbox (ADR-006 D3/D4/D9) -------------------------
class FakeHost:
    """What the chat asks the QCR host for: answers, messages, acknowledgements."""

    def __init__(self, inbox=()):
        self.answers, self.acks, self.inbox = [], [], list(inbox)

    def answer_agent(self, execution_id, approve, by="you"):
        self.answers.append((execution_id, approve))

    def messages(self, done, **filters):
        done([one for one in self.inbox if all(one.get(key) == value for key, value in filters.items())])

    def acknowledge_message(self, message_id, by="you"):
        self.acks.append(message_id)

    def grant_agent(self, *args, **kwargs):
        return None


@pytest.fixture
def chat(qapp, tmp_path):
    from PySide6.QtCore import QObject, Signal
    from cms_gui.pages import agentchat
    from cms_gui.settings import Settings
    made = []

    class Turn(QObject):
        event = Signal(dict)
        answered = Signal(dict)

        def __init__(self, core, session_id, document, parent=None):
            super().__init__(parent)
            self.document = document
            made.append(self)

        def start(self):
            pass
    tab = agentchat.ChatTab(Settings())
    tab.turn_factory = Turn
    tab.core = lambda: object()
    tab.set_connections([CLAUDE, CODEX])
    previous = agentchat.ChatTab.qcr_access
    tab.made = made
    yield tab, agentchat
    agentchat.ChatTab.qcr_access = previous
    tab.close()


def _cards(tab, kind):
    return tab.strip.findChildren(kind)


SESSION = {"id": "s-1", "connection": "claude", "folder": "/tmp", "mode": "edit", "others": "approve",
           "source": {"label": "Chat"}, "turns": []}


def test_a_request_waits_for_a_yes_then_follows_its_task(chat):
    tab, agentchat = chat
    host = FakeHost()
    agentchat.ChatTab.qcr_access = host
    tab._show(dict(SESSION), [], whole=True)
    tab.agent_request({"type": "agent_approval", "execution_id": "e-1", "session": "s-1", "target": "codex",
                       "target_name": "Codex", "goal": "Fix the login test", "mode": "edit", "files": 2})
    (card,) = _cards(tab, agentchat.agentcards.AgentRequestCard)
    assert card.kind.text() == "TASK FOR CODEX" and card.meta.text() == "waits for you"
    assert card.details.text() == "Mode: Edit · 2 files"
    assert card.approve_button.isVisibleTo(tab) and card.deny_button.isVisibleTo(tab)
    card.approve_button.click()
    assert host.answers == [("e-1", True)]
    tab.agent_execution({"execution_id": "e-1", "capability_id": "agents.codex.delegate",
                         "actor_id": "agent:claude/s-1", "state": "running"})
    tab.agent_execution({"execution_id": "e-1", "capability_id": "agents.codex.delegate",
                         "actor_id": "agent:claude/s-1", "state": "succeeded",
                         "output": {"session_id": "child", "answer": "Fixed it.",
                                    "acceptance": [{"criterion": "passes", "status": "unverified"}]}})
    (card,) = _cards(tab, agentchat.agentcards.AgentRequestCard)
    assert card.meta.text() == "done" and not card.approve_button.isVisibleTo(tab)
    assert card.open_button.isVisibleTo(tab) and card.dismiss_button.isVisibleTo(tab)
    assert "Fixed it." in card.result.text() and "not verified" in card.result.text()
    card.dismiss_button.click()
    assert not _cards(tab, agentchat.agentcards.AgentRequestCard) and not tab.strip.isVisibleTo(tab)


def test_a_denial_and_an_auto_task_of_another_conversation(chat):
    tab, agentchat = chat
    host = FakeHost()
    agentchat.ChatTab.qcr_access = host
    tab._show(dict(SESSION), [], whole=True)
    tab.agent_request({"execution_id": "e-2", "session": "s-1", "kind": "message", "target": "codex",
                       "target_name": "Codex", "body": "Port is 8070.", "expects_reply": False})
    (card,) = _cards(tab, agentchat.agentcards.AgentRequestCard)
    assert card.kind.text() == "MESSAGE TO CODEX" and not card.details.isVisibleTo(tab)
    card.deny_button.click()
    assert host.answers == [("e-2", False)]
    # Auto: no question was asked, yet the conversation it came from shows the task;
    # another conversation's task is not shown here.
    tab.agent_execution({"execution_id": "e-3", "capability_id": "agents.codex.delegate",
                         "actor_id": "agent:claude/s-1", "state": "dispatched", "input": {"goal": "Check it"}})
    tab.agent_execution({"execution_id": "e-4", "capability_id": "agents.codex.delegate",
                         "actor_id": "agent:claude/other", "state": "running", "input": {"goal": "Elsewhere"}})
    texts = [one.text.text() for one in _cards(tab, agentchat.agentcards.AgentRequestCard)]
    assert texts == ["Port is 8070.", "Check it"]


def test_what_other_agents_wrote_goes_with_the_next_message(chat):
    tab, agentchat = chat
    host = FakeHost(inbox=[
        {"message_id": "m-1", "recipient_session": "s-1", "recipient_connection": "claude", "kind": "reply",
         "sender_connection": "codex", "body": "Port 8070.", "state": "delivered", "created_at": 1},
        {"message_id": "m-2", "recipient_session": None, "recipient_connection": "claude", "kind": "message",
         "sender_connection": "codex", "body": "Tests are red.", "state": "delivered", "created_at": 2},
        {"message_id": "m-3", "recipient_session": "s-9", "recipient_connection": "claude", "kind": "reply",
         "sender_connection": "codex", "body": "Not for this one.", "state": "delivered", "created_at": 3}])
    agentchat.ChatTab.qcr_access = host
    tab._show(dict(SESSION), [], whole=True)
    cards = _cards(tab, agentchat.agentcards.InboxCard)
    assert [card.text.text() for card in cards] == ["Port 8070.", "Tests are red."]
    assert cards[0].kind.text() == "REPLY FROM CODEX" and cards[0].meta.text() == "goes with your next message"
    cards[1].dismiss_button.click()
    assert host.acks == ["m-2"]
    tab._start(dict(SESSION), "carry on", document={"message": "carry on"})
    said = tab.made[0].document["message"]
    assert said.startswith(agentchat.agentcards.INBOX) and "reply from codex: Port 8070." in said
    assert said.endswith("carry on") and "Tests are red." not in said
    assert host.acks == ["m-2", "m-1"] and not _cards(tab, agentchat.agentcards.InboxCard)


# -- conversations another one started (ADR-006): beside it, and saying so ---------------
def test_children_stand_right_below_their_parent_and_say_whose(qapp):
    from cms_gui.pages import agentchat
    parent = {"id": "p", "title": "Login is flaky", "source": {"label": "Chat"}}
    other = {"id": "o", "title": "Other", "source": {"label": "Chat"}}
    task = {"id": "t", "title": "Fix the test", "connection_name": "Codex",
            "source": {"label": "Delegated", "parent": "p", "by": "Claude", "record": True}}
    asked = {"id": "q", "title": "Which port?", "connection_name": "Codex",
             "source": {"label": "Message", "parent": "p", "by": "Claude", "record": True}}
    orphan = {"id": "x", "title": "Old", "connection_name": "Codex",
              "source": {"label": "Delegated", "parent": "gone", "by": "Claude", "record": True}}
    ordered = agentchat.ordered_with_children([task, other, asked, parent, orphan])
    assert [one["id"] for one in ordered] == ["o", "p", "t", "q", "x"]
    assert agentchat.child_title(task) == "Task for Codex: Fix the test"
    assert agentchat.child_title(asked) == "Question for Codex: Which port?"
    assert agentchat.child_title(parent) == "Login is flaky"
    assert agentchat.ordered_with_children([parent, task]) and agentchat.is_child(task)
    assert agentchat.record_text(task) == "A task Claude delegated to Codex. It is shown here, not carried on."


def test_a_task_that_starts_running_lists_its_conversation_at_once(chat, monkeypatch):
    tab, agentchat = chat
    loads = []
    monkeypatch.setattr(tab, "load_sessions", lambda then=None: loads.append(1))
    # Every step of the child comes as a change of a running execution: read once.
    for state in ("dispatched", "running", "running", "running", "succeeded", "succeeded"):
        tab.agent_execution({"execution_id": "e-9", "capability_id": "agents.codex.delegate",
                             "actor_id": "agent:claude/s-1", "state": state, "input": {"goal": "x"}})
    assert len(loads) == 2


def test_the_head_says_what_the_last_turn_ran_with(qapp):
    from cms_gui.pages import agentchat
    assert agentchat.ran_with_text({"turns": []}) == ""
    assert agentchat.ran_with_text({"turns": [{"model": "", "effort": "", "model_used": "gpt-x",
                                               "effort_used": "medium"}]}) == "gpt-x · effort medium"
    assert agentchat.ran_with_text({"turns": [{"model": "sonnet", "effort": "low"}]}) == "sonnet · effort low"


def test_an_existing_connection_keeps_its_agent_before_the_list_arrives(qapp, dispose):
    from cms_gui.pages.agents import ConnectionDialog
    dialog = ConnectionDialog(None, dict(CODEX), kinds=[])        # the core has not answered yet
    try:
        assert dialog.entry()["kind"] == "codex"
        dialog.set_kinds([{"kind": "claude", "name": "Claude Code"}, {"kind": "codex", "name": "Codex"}])
        assert dialog.entry()["kind"] == "codex" and dialog.kind.currentText() == "Codex"
    finally:
        dispose(dialog)
