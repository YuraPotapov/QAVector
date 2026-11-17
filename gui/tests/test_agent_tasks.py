"""Agents -> Tasks: every task agents handed on, in one place (QCR ADR-012 D4/D5).

* One row a task from the core's list: what waits for the person first, said
  in the warning ink; filters for what needs you, what goes on, what ended.
* Every empty state says why it is empty.
* A picked task shows its history, read from the core, and only the actions
  its state allows: Approve/Deny, Answer, Stop, open either conversation.
"""

import time

import pytest
from PySide6.QtCore import QObject, Signal

from cms_gui import theme
from cms_gui.pages import agenttasks

NOW = time.time()


def row(task, state, needs=False, **more):
    return dict({"task": task, "latest": task, "kind": "task", "state": state, "needs_person": needs,
                 "goal": "Goal of " + task, "name": "", "target": "codex", "target_name": "Codex", "role": "",
                 "model": "gpt-5", "effort": "low", "from_name": "Claude", "from_session": "s-parent",
                 "child_session": "child-" + task, "question": "", "questions": 0, "files_changed": 0,
                 "criteria": 0, "passed": 0, "created_at": NOW - 60, "updated_at": NOW - 10}, **more)


ROWS = [row("done", "succeeded", files_changed=2, updated_at=NOW - 5),
        row("asking", "asks", True, question="Which database?", questions=1, role="Dev", updated_at=NOW - 30),
        row("approve", "approval", True, updated_at=NOW - 40),
        row("going", "running", updated_at=NOW - 1)]
HISTORY = {"ok": True, "usage": {"tokens_total": 12000, "estimate_usd": 0.42}, "entries": [
    {"at": NOW - 60, "kind": "handed", "text": "Goal of asking", "by": "Claude", "to": "Dev (Codex)"},
    {"at": NOW - 50, "kind": "started", "text": "Dev works on it."},
    {"at": NOW - 30, "kind": "asked", "text": "Which database?", "number": 1}]}


class Host(QObject):
    execution_changed = Signal(dict)
    agent_approval = Signal(dict)

    def __init__(self, rows=ROWS):
        super().__init__()
        self.rows, self.asked, self.done = rows, [], []

    def ask(self, kind, done, **fields):
        self.asked.append((kind, fields))
        done({"ok": True, "tasks": list(self.rows)} if kind == "tasks" else HISTORY)

    def reply_task(self, task, answer, by="You"):
        self.done.append(("reply", task, answer))

    def answer_agent(self, execution_id, approve, by="you"):
        self.done.append(("approve", execution_id, approve))

    def stop_task(self, task, by="you"):
        self.done.append(("stop", task))


@pytest.fixture
def tab(qapp):
    made = agenttasks.TasksTab()
    made.resize(1000, 700)
    yield made
    made.close()


def states(tab):
    return [tab.table.item(index, 0).text() for index in range(tab.table.rowCount())]


def pick(tab, task):
    for index in range(tab.table.rowCount()):
        if tab.table.item(index, 0).data(0x0100) == task:
            tab.table.selectRow(index)
            return
    pytest.fail("no row " + task)


def test_what_waits_for_the_person_comes_first_and_filters_keep_their_rows(tab):
    tab.set_host(Host())
    tab.shown()
    assert states(tab) == ["Asks a question", "Waits for your approval", "Working", "Done"]
    assert tab.table.item(0, 0).foreground().color().name() == theme.WARN_INK.lower()
    assert tab.table.item(0, 3).text() == "Dev (Codex)" and tab.table.item(0, 4).text() == "1"
    tab.filter.set_current("Needs you")
    assert states(tab) == ["Asks a question", "Waits for your approval"]
    tab.filter.set_current("Active")
    assert states(tab) == ["Asks a question", "Waits for your approval", "Working"]
    tab.filter.set_current("Ended")
    assert states(tab) == ["Done"]


def test_every_empty_state_says_why(tab):
    tab._say_empty()
    assert "not running" in tab.note.text()
    tab.set_host(Host(rows=[]))
    tab.shown()
    assert tab.note.text().startswith("No agent has handed a task")
    tab.host.rows = [row("done", "succeeded")]
    tab.shown()
    tab.filter.set_current("Needs you")
    assert tab.note.text() == "Nothing waits for you."
    assert not tab.detail.empty.isHidden() and tab.detail.title.isHidden()


def test_a_picked_task_shows_its_history_and_only_what_its_state_allows(tab):
    host = Host()
    tab.set_host(host)
    tab.show()
    tab.shown()
    pick(tab, "asking")
    detail = tab.detail
    assert ("task_history", {"task": "asking"}) in host.asked
    lines = detail.history.findChildren(agenttasks.QLabel)
    said = " ".join(one.text() for one in lines)
    assert "Handed to Dev (Codex) by Claude" in said and "Asked (question 1)" in said
    assert "%s tokens" % agenttasks.count_text(12000) in detail.meta.text()
    assert "est. $0.42" in detail.meta.text()
    assert detail.answer_button.isVisible() and not detail.approve_button.isVisible()
    assert detail.stop_button.isVisible() and "Which database?" in detail.question.text()
    detail.answer_box.setPlainText("Use sqlite.")
    detail.answer_button.click()
    assert host.done[-1] == ("reply", "asking", "Use sqlite.")

    pick(tab, "approve")
    assert detail.approve_button.isVisible() and detail.deny_button.isVisible()
    assert not detail.answer_button.isVisible() and not detail.stop_button.isVisible()
    detail.approve_button.click()
    assert host.done[-1] == ("approve", "approve", True)

    pick(tab, "done")
    assert not any(button.isVisible() for button in (detail.approve_button, detail.answer_button,
                                                     detail.stop_button))
    opened = []
    tab.open_conversation.connect(opened.append)
    detail.open_from.click()
    detail.open_child.click()
    assert opened == ["s-parent", "child-done"]


def test_a_change_in_the_journal_reads_the_list_again_but_only_for_agents(tab):
    host = Host()
    tab.set_host(host)
    tab.show()
    host.execution_changed.emit({"capability_id": "services.odoo.start"})
    assert not tab._timer.isActive()
    host.execution_changed.emit({"capability_id": "agents.codex.delegate"})
    assert tab._timer.isActive()
