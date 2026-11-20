"""A Jira issue handed to an agent: the bridge between the two integrations.

Jira's page and the agents' pages, each over a fake core; the bridge is what
the Integrations page adds to Jira's opened issue. Nothing runs an agent.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QSettings, Qt                              # noqa: E402
from PySide6.QtTest import QTest                                     # noqa: E402
from PySide6.QtWidgets import QDialog                                # noqa: E402

from cms_gui.pages.integrations import IntegrationsPage             # noqa: E402
from cms_gui.settings import Settings                                # noqa: E402
import test_agents_page as agents_fakes                              # noqa: E402
import test_integrations_page as jira_fakes                          # noqa: E402
from test_agentchat import FakeTurn                                  # noqa: E402

REF = "jira:product-team:QA-139"


class BothCore(jira_fakes.FakeCore):
    """Jira's canned issues, and the agents' canned conversations."""

    def __init__(self, agents=None, **jira):
        super().__init__(**jira)
        self.agent_side = agents or agents_fakes.FakeCore()

    def agents(self, *args, **kwargs):
        return self.agent_side.agents(*args, **kwargs)


def _talk(session_id, title, **extra):
    """A conversation about QA-139, as the core lists it."""
    return agents_fakes._session(session_id, title, record=False,
                                 source={"label": "Jira QA-139", "detail": "Session expires",
                                         "ref": REF, "record": False}, **extra)


@pytest.fixture
def settings(tmp_path):
    made = Settings()
    made._qs = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
    return made


@pytest.fixture
def make(qapp, settings, dispose, tmp_path):
    pages = []
    FakeTurn.made = []

    def build(agents=None):
        settings.save_integrations_state({"agents": {"folders": [str(tmp_path)],
                                                     "connection": "claude"}})
        core = BothCore(agents=agents)
        page = IntegrationsPage(settings)
        page.agents.chat.turn_factory = FakeTurn
        page.resize(1200, 800)
        page.set_core(core)
        page.open_provider("jira")
        page.show()
        qapp.processEvents()
        pages.append(page)
        page.jira.issues.select_key("QA-139")
        qapp.processEvents()
        return page, core

    yield build
    for page in pages:
        dispose(page)


# -- what an opened issue shows ----------------------------------------------------------
def test_an_opened_issue_lists_the_conversations_about_it_and_no_others(make):
    agents = agents_fakes.FakeCore(sessions=[
        _talk("20261111-172345-3fa91c", "Fix the expiry"),
        agents_fakes._session("20261110-090000-bbbbbb", "Something else")])
    page, core = make(agents)
    asked = [document for args, document in agents.calls if args[0] == "sessions"]
    assert {"ref": REF} in asked
    detail = page.jira.issues.detail
    assert [detail.tabs.tabText(index) for index in range(detail.tabs.count())] == [
        "Issue", "Agent work 1", "Summary", "Changes"]
    work = page.delegation.work
    assert work.table.item(0, 0).text() == "Fix the expiry"
    assert work.table.item(0, 2).text() == "Done"
    assert not page.delegation.button.isHidden() and page.delegation.button.isEnabled()


def test_a_conversation_opens_in_place_read_only_and_goes_on_in_the_chat(make):
    session = _talk("20261111-172345-3fa91c", "Fix the expiry", phase="plan", plan=True)
    agents = agents_fakes.FakeCore(sessions=[session], events={session["id"]: [
        {"type": "user", "text": "Plan it", "plan": True, "seq": 1, "turn": 1},
        {"type": "plan", "text": "# Plan", "seq": 2, "turn": 1},
        {"type": "completed", "text": "", "seq": 3, "turn": 1}]})
    page, core = make(agents)
    work = page.delegation.work
    work._toggle(0, True)
    detail = work.detail(session["id"])
    assert detail.feed.kinds() == ["you", "plan", "end"]
    (card,) = detail.feed.cards()
    assert card.actions.isHidden()                  # acted on in the chat, not here
    assert work.table.item(0, 2).text() == "Plan ready"
    detail.open.click()
    assert page.stack.currentWidget() is page.agents
    assert page.agents.chat.current["id"] == session["id"]


def test_the_chat_reached_from_an_issue_has_the_way_back_in_its_breadcrumb(make):
    session = _talk("20261111-172345-3fa91c", "Fix the expiry")
    page, core = make(agents_fakes.FakeCore(sessions=[session], events={session["id"]: []}))
    page.delegation.work._toggle(0, True)
    page.delegation.work.detail(session["id"]).open.click()
    crumbs = page.agents.crumbs
    assert page.stack.currentWidget() is page.agents
    assert crumbs.trail() == ["Jira", "QA-139"]
    assert [one.text() for one in crumbs.parts] == ["Jira  /", "QA-139  /"]
    # Jira has moved on since; the issue part opens the issue again.
    page.jira.issues.select_key("QA-142")
    crumbs.parts[1].click()
    assert page.stack.currentWidget() is page.jira
    assert page.jira.issues.detail.issue["key"] == "QA-139"
    # Reached any other way, Agents has no trail.
    page.open_provider("agents")
    assert crumbs.trail() == [] and page.jira.crumbs.trail() == []
    page.delegation.open_agents(session["id"])
    assert crumbs.trail() == ["Jira", "QA-139"]
    crumbs.parts[0].click()
    assert page.stack.currentWidget() is page.jira


@pytest.mark.parametrize("edit_before_send", [False, True])
def test_delegate_prepares_a_chat_draft_and_waits_for_send(make, tmp_path, settings,
                                                        edit_before_send):
    page, core = make()
    chat = page.agents.chat
    chat.composer.set_draft("An unrelated unsent message")
    bridge = page.delegation
    bridge.button.click()
    panel = bridge.panel
    assert panel.heading.text() == "Delegate QA-139" and panel.earlier.isHidden()
    assert panel.plan.isChecked() and panel.chosen()["folder"] == str(tmp_path)
    assert panel.text.toPlainText() == ""
    if edit_before_send:
        panel.text.setPlainText("Check the login flow.")
    task = panel.text.toPlainText()
    panel.go.click()
    args, document = next(call for call in core.agent_side.calls if call[0][0] == "session-new")
    assert document["source"] == {"label": "Jira QA-139", "detail": "Session expires",
                                  "ref": REF, "task_refs": [REF]}
    assert (document["plan"], document["folder"], document["title"]) == (
        True, str(tmp_path), "QA-139 · Session expires")
    assert FakeTurn.made == [] and not chat.running()
    assert chat.composer.draft() == task
    assert chat.feed.kinds() == []
    assert page.stack.currentWidget() is page.agents and not panel.isVisible()
    # Carried on in the chat: a conversation a ref opened is no record.
    assert not page.agents.chat.composer.text.isHidden()
    assert page.agents.chat.composer.closed.isHidden()
    remembered = settings.integrations_state()["delegation"]
    assert remembered["folders"] == {"QA": str(tmp_path)} and remembered["plan"] is True
    # The issue's list hears of it: the conversation is there.
    assert bridge.work.table.rowCount() == 1
    # Both drafts survive leaving and returning before anything is sent.
    session_id = chat.current["id"]
    chat.new_conversation()
    assert chat.composer.draft() == "An unrelated unsent message"
    chat.open_session(session_id)
    assert chat.composer.draft() == task and FakeTurn.made == []
    assert list(chat.composer._task_tags) == [REF]
    assert chat.composer.send_button.isEnabled()
    if edit_before_send:
        task += "\n\nCheck the regression first."
        chat.composer.set_draft(task)
    chat.composer.send()
    (turn,) = FakeTurn.made
    assert turn.session_id == session_id
    assert turn.document["plan"] is True and turn.document["message"] == task
    assert chat.composer.draft() == ""

    turn.refuse("task_read", "Could not read the attached Jira task.")
    assert chat.current["id"] == session_id and chat.composer.draft() == task
    assert chat.composer.send_button.isEnabled() and list(chat.composer._task_tags) == [REF]
    chat.composer._task_tags[REF].linkActivated.emit("#")
    assert page.stack.currentWidget() is page.jira
    assert page.jira.issues.detail.issue["key"] == "QA-139"


def test_delegate_offers_earlier_conversations_with_an_empty_input(make):
    earlier = _talk("20261110-090000-aaaaaa", "First try", phase="plan")
    agents = agents_fakes.FakeCore(sessions=[earlier], events={earlier["id"]: [
        {"type": "user", "text": "Plan it", "seq": 1},
        {"type": "plan", "text": "# Plan\n1. Guard it.", "seq": 2}]})
    page, core = make(agents)
    bridge = page.delegation
    bridge.button.click()
    panel = bridge.panel
    assert not panel.earlier.isHidden()
    assert panel.text.toPlainText() == ""
    carried = []
    bridge.open_agents = carried.append
    row = panel.earlier_rows.itemAt(0).widget()
    assert row.title.text() == "First try"
    assert row.state.text() == "Plan ready"
    assert row.when.text()
    row.continue_button.click()
    assert carried == [earlier["id"]]


def test_delegate_keeps_its_draft_when_agent_popup_or_owned_dialog_takes_focus(make, qapp):
    agents = agents_fakes.FakeCore(connections=[agents_fakes._connection(),
                                               agents_fakes._connection("other", "Other agent")])
    page, _ = make(agents)
    page.delegation.button.click()
    qapp.processEvents()
    panel = page.delegation.panel
    panel.text.setPlainText("Keep my edited task")
    panel.connection.showPopup()
    qapp.processEvents()
    # Deliver the native focus notification explicitly too: offscreen Qt
    # does not always transfer native focus to combo popups as X11 does.
    popup_window = panel.connection.view().window()
    panel._focus_moved(popup_window.windowHandle())
    assert panel.isVisible()
    QTest.keyClick(panel.connection.view(), Qt.Key_Down)
    QTest.keyClick(panel.connection.view(), Qt.Key_Return)
    qapp.processEvents()
    assert panel.isVisible()
    assert panel.chosen()["connection"] == "other"
    assert panel.text.toPlainText() == "Keep my edited task"
    dialog = QDialog(panel)
    dialog.show()
    qapp.processEvents()
    panel._focus_moved(dialog.windowHandle())
    assert panel.isVisible()
    dialog.close()
    qapp.processEvents()
    panel._focus_moved(page.windowHandle())
    assert not panel.isVisible()
    assert not FakeTurn.made


def test_the_summary_adds_up_the_conversations_and_what_they_changed(make):
    one = _talk("20261111-172345-3fa91c", "Fix the expiry", cost_usd=0.25,
                tokens={"input": 10, "cache_read": 100000, "cache_write": 4000, "output": 2000},
                context={"used": 24813, "window": 200000},
                turns=[{"n": 1, "state": "completed", "cost_usd": 0.1, "started": 0.0,
                        "ended": 60.0},
                       {"n": 2, "state": "completed", "cost_usd": 0.15, "started": 100.0,
                        "ended": 130.0}])
    two = _talk("20261110-090000-aaaaaa", "Review", cost_usd=0.05,
                turns=[{"n": 1, "state": "completed", "cost_usd": 0.05, "started": 0.0,
                        "ended": 20.0}])
    changed = {"ok": True, "files": [{"path": "calc.py", "status": "modified", "added": 3,
                                      "removed": 1, "binary": False}],
               "patch": "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
                        "@@ -1 +1 @@\n-a\n+b\n", "added": 3, "removed": 1}
    agents = agents_fakes.FakeCore(sessions=[one, two], changes=lambda args, doc: changed)
    page, core = make(agents)
    detail = page.jira.issues.detail
    detail.show_tab(page.delegation.summary)
    summary = page.delegation.summary
    assert summary.figures.text() == "2 conversations · 3 turns · 1 min 50 s · est. $0.30"
    assert "Tokens: 106k in all - 104k in (100k cached) · 2k out." in summary.detail.text()
    assert "Files changed: 1 (+3 -1)" in summary.detail.text()
    assert [summary.table.item(0, column).text() for column in range(6)] == [
        "Fix the expiry", "2", "106k", "25k of 200k", "$0.25", "Done"]
    detail.show_tab(page.delegation.changes_tab)
    section = page.delegation.changes_tab.sections[one["id"]]
    assert section.title() == "Fix the expiry · 1 file +3 -1"
    section.set_expanded(True)
    assert [file.entry["path"] for file in section.view.sections] == ["calc.py"]


def test_the_summary_says_what_was_handed_on_apart_from_the_issue_s_own(make):
    """A task handed to another agent is a conversation of its own, with its own
    ref: counted by the core (ADR-008), said apart, never added into the figures."""
    one = _talk("20261111-172345-3fa91c", "Fix the expiry", cost_usd=0.25,
                turns=[{"n": 1, "state": "completed", "cost_usd": 0.25, "started": 0.0, "ended": 60.0}])
    child = agents_fakes._session("20261111-180000-bbbbbb", "Run the tests", record=True,
                                  source={"label": "Delegated", "ref": "qcr-task:e-1", "record": True,
                                          "parent": one["id"]},
                                  turns=[{"n": 1, "state": "completed", "cost_usd": 0.1, "started": 70.0,
                                          "ended": 80.0},
                                         {"n": 2, "state": "completed", "cost_usd": None, "started": 90.0,
                                          "ended": 95.0}])
    page, core = make(agents_fakes.FakeCore(sessions=[one, child]))
    detail = page.jira.issues.detail
    detail.show_tab(page.delegation.summary)
    summary = page.delegation.summary
    assert summary.figures.text() == "1 conversation · 1 turn · 1 min · est. $0.25"
    assert ("Handed on to other agents: 1 conversation, 2 turns, est. $0.10 (1 turn reported no cost)."
            in summary.detail.text())
    assert "not what is charged" in summary.detail.text()


def test_nothing_to_show_says_so_and_another_issue_starts_afresh(make):
    page, core = make()
    bridge = page.delegation
    assert bridge.work.note.text() == "No agent has worked on QA-139 yet. Delegate... hands it to one."
    assert page.jira.issues.detail.tabs.tabText(1) == "Agent work"
    page.jira.issues.detail.clear()
    assert not bridge.button.isEnabled() and bridge.ref == ""
