"""Integrations -> Agents -> Tasks: every task agents handed on, in one place (QCR ADR-012).

The list is the core's (``tasks`` over the owner pipe), read from the project's
journal - every conversation's tasks and questions, not only those of chats
that happen to be open, and complete after a restart. One row a task: the
answers to its questions are folded into it.

What waits for the person comes first and is said so: a task waiting for
approval, or one that asks a question. Selecting a task shows its history -
handed on, each turn, each question and answer, each verdict - with what the
person can do about it there: Approve or Deny, Answer, Stop, open either
conversation.
"""

import html
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton, QScrollArea,
                               QSplitter, QVBoxLayout, QWidget)

from .. import icons, theme, widgets
from .agentwords import cost_text, count_text
from .jiraconnections import _cell, _table

#: What goes on still, or waits: a task in one of these has not ended.
ACTIVE = ("approval", "waiting", "running", "dispatched", "accepted", "asks")
#: Which tasks are shown, and which rows each keeps.
FILTERS = (("All", lambda row: True),
           ("Needs you", lambda row: row.get("needs_person")),
           ("Active", lambda row: row.get("state") in ACTIVE),
           ("Ended", lambda row: row.get("state") not in ACTIVE))
#: A task's state, as the person reads it.
STATES = {"approval": "Waits for your approval", "asks": "Asks a question", "waiting": "Waits its turn",
          "running": "Working", "dispatched": "Starting", "accepted": "Starting", "succeeded": "Done",
          "failed": "Failed", "rework": "Sent back for rework", "skipped": "Skipped", "denied": "Denied",
          "canceled": "Canceled", "stopped": "Stopped", "timed_out": "Ran out of time",
          "unknown": "Outcome unknown"}
#: Waits before the list is read again after the journal changed: a task's
#: turn changes its execution many times a second.
REFRESH_MS = 400


def _when(at):
    if not at:
        return ""
    if time.time() - at < 20 * 3600:
        return time.strftime("%H:%M", time.localtime(at))
    return time.strftime("%d %b %H:%M", time.localtime(at))


def _to(row):
    program = row.get("target_name") or row.get("target") or ""
    return "%s (%s)" % (row["role"], program) if row.get("role") and program else (row.get("role") or program)


class TasksTab(QWidget):
    HEADERS = ("State", "Task", "From", "To", "Questions", "Files", "Updated")

    #: A conversation the person wants to see: the delegating one or the task's own.
    open_conversation = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.host = None
        self.rows = []
        self.selected = ""
        self._asked = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.shown)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(8)
        self.filter = widgets.Segmented([name for name, _ in FILTERS], "All")
        self.filter.changed.connect(lambda _name: self._fill())
        column.addWidget(widgets.row(self.filter, None))
        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        self.table.setObjectName("agentTasksTable")
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for index, width in ((0, 168), (2, 120), (3, 150), (4, 84), (5, 56), (6, 92)):
            self.table.setColumnWidth(index, width)
        self.table.itemSelectionChanged.connect(self._picked)
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._say_empty)
        self.note = widgets.empty_note(self.table, "")
        self.skeleton = widgets.Skeleton(self.table, "rows")
        panel.layout().addWidget(self.table)
        split.addWidget(panel)
        self.detail = TaskDetail()
        self.detail.open_conversation.connect(self.open_conversation)
        self.detail.acted.connect(self._acted)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        panel.setMinimumHeight(140)
        self.split = split
        self._sized = False
        column.addWidget(split, 1)
        self._fill()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._sized:
            # The task's history is what is read: it gets the larger part at first.
            self._sized = True
            total = max(self.split.height(), 400)
            self.split.setSizes([int(total * 0.42), total - int(total * 0.42)])

    # -- the outside ------------------------------------------------------------
    def set_host(self, host):
        """The QCR host: what is read, and what is told when the journal changes."""
        self.host = host
        self.detail.host = host
        if host is not None:
            host.execution_changed.connect(self._changed)
            # A task that starts waiting for the person: on the board at once.
            host.agent_approval.connect(lambda _found: self._timer.start())

    def shown(self):
        """Read the list again - when the tab comes in front, and after a change."""
        if self.host is None:
            self._fill()
            return
        self._asked += 1
        asked = self._asked
        if not self.rows:
            self.skeleton.start()

        def answered(reply):
            if asked != self._asked:
                return
            self.skeleton.stop()
            if not reply.get("ok"):
                self.note.say((reply.get("error") or {}).get("message") or "The tasks could not be read.")
                return
            self.set_rows(reply.get("tasks") or [])
        self.host.ask("tasks", answered)

    def set_rows(self, rows):
        # What waits for the person first, then the newest.
        self.rows = sorted(rows or [], key=lambda one: (not one.get("needs_person"), -(one.get("updated_at") or 0)))
        self._fill()
        if self.selected:
            self.detail.reload()

    # -- inside -------------------------------------------------------------------
    def _changed(self, execution):
        if str(execution.get("capability_id") or "").startswith("agents.") and self.isVisible():
            self._timer.start()

    def _acted(self):
        self._timer.start()

    def _shown_rows(self):
        keep = dict(FILTERS)[self.filter.current()]
        return [row for row in self.rows if keep(row)]

    def _fill(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        chosen = None
        for row in self._shown_rows():
            index = self.table.rowCount()
            self.table.insertRow(index)
            goal = (row.get("name") + " - " if row.get("name") else "") + (row.get("goal") or "")
            criteria = row.get("criteria") or 0
            values = (STATES.get(row.get("state"), row.get("state") or ""), goal,
                      row.get("from_name") or "", _to(row), str(row.get("questions") or "") or "",
                      str(row.get("files_changed") or "") or "", _when(row.get("updated_at")))
            for column, value in enumerate(values):
                item = _cell(value)
                if column == 0:
                    item.setData(Qt.UserRole, row["task"])
                    if row.get("needs_person"):
                        item.setForeground(QColor(theme.WARN_INK))
                    elif row.get("state") in ("failed", "rework", "timed_out"):
                        item.setForeground(QColor(theme.BAD_INK))
                if column == 1 and criteria:
                    item.setToolTip("%d of %d acceptance criteria verified as passed." % (
                        row.get("passed") or 0, criteria))
                self.table.setItem(index, column, item)
            if row["task"] == self.selected:
                chosen = index
        self.table.blockSignals(False)
        if chosen is not None:
            self.table.selectRow(chosen)
        self.search.apply()
        self._say_empty()
        found = next((row for row in self.rows if row["task"] == self.selected), None)
        self.detail.show_task(found)

    def _say_empty(self):
        if self.host is None:
            self.note.say("QAVector's runtime is not running, so no task can be read.")
        elif not self.rows:
            self.note.say("No agent has handed a task to another yet. Tasks appear here as soon as one does.")
        elif not self._shown_rows():
            self.note.say({"Needs you": "Nothing waits for you.", "Active": "No task is going on now.",
                           "Ended": "No task has ended yet."}.get(self.filter.current(), ""))
        else:
            self.note.say("")
        self.note.say_filtered("Nothing matches the search." if self.search.searching() else "")

    def _picked(self):
        items = self.table.selectedItems()
        task = self.table.item(items[0].row(), 0).data(Qt.UserRole) if items else ""
        if task == self.selected:
            return
        self.selected = task or ""
        self.detail.show_task(next((row for row in self.rows if row["task"] == self.selected), None))


class TaskDetail(QFrame):
    """One task: what it is, what can be done about it, and its history."""

    open_conversation = Signal(str)
    #: Something was done to the task: the list is read again.
    acted = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("agentTaskDetail")
        self.setProperty("role", "taskdetail")
        self.host = None
        #: Who carries out what the person does here: the chat, so that its cards
        #: say the same (the Agents page sets it); else the host itself.
        self.acts = None
        self.task = None
        self._asked = 0
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(6)
        self.title = QLabel("")
        self.title.setProperty("role", "cardquestion")
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        column.addWidget(self.title)
        self.meta = QLabel("")
        self.meta.setProperty("role", "hint")
        self.meta.setWordWrap(True)
        column.addWidget(self.meta)
        self.question = QLabel("")
        self.question.setWordWrap(True)
        self.question.setTextInteractionFlags(Qt.TextSelectableByMouse)
        column.addWidget(self.question)
        self.answer_box = QPlainTextEdit()
        self.answer_box.setObjectName("taskBoardAnswer")
        self.answer_box.setPlaceholderText("Your answer - it goes on in the task's own conversation.")
        self.answer_box.setFixedHeight(60)
        self.answer_box.textChanged.connect(self._ready)
        column.addWidget(self.answer_box)
        self.open_from = QPushButton("Open its chat")
        self.open_from.setProperty("variant", "ghost")
        self.open_from.setToolTip("The conversation that handed this task on.")
        self.open_from.clicked.connect(lambda: self._open("from_session"))
        self.open_child = QPushButton("Open the task's conversation")
        self.open_child.setProperty("variant", "ghost")
        self.open_child.clicked.connect(lambda: self._open("child_session"))
        self.stop_button = QPushButton("Stop")
        self.stop_button.setProperty("variant", "ghost")
        self.stop_button.setToolTip("End this task here: it takes no more answers, and what of it runs is "
                                    "canceled. Its conversation keeps what it found.")
        self.stop_button.clicked.connect(self._stop)
        self.deny_button = QPushButton("Deny")
        self.deny_button.clicked.connect(lambda: self._approve(False))
        self.approve_button = QPushButton("Approve")
        self.approve_button.setProperty("variant", "primary")
        self.approve_button.clicked.connect(lambda: self._approve(True))
        self.answer_button = QPushButton("Answer")
        self.answer_button.setProperty("variant", "primary")
        self.answer_button.clicked.connect(self._answer)
        column.addWidget(widgets.row(self.open_from, self.open_child, None, self.stop_button, self.deny_button,
                                     self.approve_button, self.answer_button))
        self.heading = QLabel("HISTORY")
        self.heading.setProperty("role", "statlabel")
        column.addWidget(self.heading)
        self.history = QWidget()
        self.history.setObjectName("agentTaskHistory")
        self._lines = QVBoxLayout(self.history)
        self._lines.setContentsMargins(0, 0, 0, 0)
        self._lines.setSpacing(4)
        self._lines.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidget(self.history)
        column.addWidget(self.scroll, 1)
        self.empty = QLabel("Choose a task to see its history and what you can do about it.")
        self.empty.setProperty("role", "muted")
        self.empty.setAlignment(Qt.AlignCenter)
        column.addWidget(self.empty)
        widgets.see_through(self)
        self.show_task(None)

    def show_task(self, task):
        changed = (task or {}).get("task") != (self.task or {}).get("task")
        self.task = dict(task) if task else None
        shown = task is not None
        for widget in (self.title, self.meta, self.scroll, self.heading):
            widget.setVisible(shown)
        self.empty.setVisible(not shown)
        state = (task or {}).get("state")
        self.title.setText((task or {}).get("goal") or "")
        self.question.setText(("<b>%s asks:</b> %s" % (html.escape(_to(task)), html.escape(task.get("question") or "")))
                              if state == "asks" else "")
        self.question.setVisible(state == "asks")
        self.answer_box.setVisible(state == "asks")
        self.answer_button.setVisible(state == "asks")
        self.approve_button.setVisible(state == "approval")
        self.deny_button.setVisible(state == "approval")
        self.stop_button.setVisible(state in ("running", "asks", "waiting") and (task or {}).get("kind") == "task")
        self.open_from.setVisible(bool((task or {}).get("from_session")))
        self.open_child.setVisible(bool((task or {}).get("child_session")))
        self._say_meta({})
        if changed:
            self.answer_box.clear()
            self._show_entries([])
        self._ready()
        if shown and changed:
            self.reload()

    def reload(self):
        if self.task is None or self.host is None:
            return
        self._asked += 1
        asked, task = self._asked, self.task["task"]

        def answered(reply):
            if asked != self._asked or self.task is None or self.task["task"] != task:
                return
            if reply.get("ok"):
                self._say_meta(reply.get("usage") or {})
                self._show_entries(reply.get("entries") or [])
        self.host.ask("task_history", answered, task=task)

    # -- inside -------------------------------------------------------------------
    def _say_meta(self, usage):
        task = self.task or {}
        parts = ["%s, handed on by %s" % (_to(task), task.get("from_name") or "an agent") if task else ""]
        with_ = " ".join(part for part in (task.get("model"), task.get("effort")) if part)
        if with_:
            parts.append(with_)
        if task.get("questions"):
            parts.append("%d question%s" % (task["questions"], "" if task["questions"] == 1 else "s"))
        if task.get("criteria"):
            parts.append("%d of %d criteria verified" % (task.get("passed") or 0, task["criteria"]))
        if usage.get("tokens_total"):
            parts.append("%s tokens" % count_text(usage["tokens_total"]))
        if usage.get("estimate_usd"):
            parts.append("est. " + cost_text(usage["estimate_usd"]))
        self.meta.setText(" · ".join(part for part in parts if part))

    def _show_entries(self, entries):
        while self._lines.count() > 1:
            item = self._lines.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for entry in entries:
            self._lines.insertWidget(self._lines.count() - 1, history_line(entry))

    def _open(self, key):
        if self.task and self.task.get(key):
            self.open_conversation.emit(self.task[key])

    def _ready(self):
        self.answer_button.setEnabled(bool(self.answer_box.toPlainText().strip()))

    def _answer(self):
        text = self.answer_box.toPlainText().strip()
        if text and self.task and self.host is not None:
            if self.acts is not None:
                self.acts.reply_task(self.task["latest"], text)
            else:
                self.host.reply_task(self.task["latest"], text, "You")
            self.answer_box.clear()
            self.acted.emit()

    def _approve(self, approve):
        if self.task and self.host is not None:
            if self.acts is not None:
                self.acts.answer_request(self.task["latest"], approve)
            else:
                self.host.answer_agent(self.task["latest"], approve)
            self.acted.emit()

    def _stop(self):
        if self.task and self.host is not None:
            if self.acts is not None:
                self.acts.stop_task(self.task["task"])
            else:
                self.host.stop_task(self.task["task"])
            self.acted.emit()


#: What each kind of history line says before its text, and its mark if any.
HISTORY_WORDS = {"handed": "Handed on", "approval": "Approval", "approved": "Approved", "waiting": "Waits",
                 "started": "Started", "asked": "Asked", "answer": "Answered", "done": "Done",
                 "verified": "Verified", "failed": "Failed", "canceled": "Canceled", "stopped": "Stopped",
                 "timed_out": "Ran out of time", "unknown": "Outcome unknown"}


def history_line(entry):
    """One line of a task's history: when, what, and its words."""
    row = QWidget()
    row.setProperty("seethrough", "true")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    when = QLabel(time.strftime("%H:%M:%S", time.localtime(entry.get("at") or 0)))
    when.setProperty("role", "msgmeta")
    when.setFixedWidth(58)
    when.setAlignment(Qt.AlignTop | Qt.AlignLeft)
    layout.addWidget(when, 0, Qt.AlignTop)
    mark = QLabel()
    mark.setFixedSize(16, 16)
    kind = entry.get("kind")
    if kind in ("asked", "answer"):
        name = "asked" if kind == "asked" else "answered"
        mark.setPixmap(icons.pixmap(name, 16, theme.WARN_INK if kind == "asked" else theme.ACCENT))
    layout.addWidget(mark, 0, Qt.AlignTop)
    words = HISTORY_WORDS.get(kind, kind or "")
    text = str(entry.get("text") or "")
    if kind == "handed":
        words = "Handed to %s by %s" % (entry.get("to") or "an agent", entry.get("by") or "an agent")
    elif kind == "answer" and entry.get("by"):
        words = "%s answered" % ("You" if entry["by"] == "You" else entry["by"])
    elif kind == "asked" and entry.get("number"):
        words = "Asked (question %d)" % entry["number"]
    elif kind == "done":
        extra = []
        if entry.get("files_changed"):
            extra.append("%d file%s changed" % (entry["files_changed"], "" if entry["files_changed"] == 1 else "s"))
        if entry.get("verdict"):
            extra.append("verdict: %s" % entry["verdict"])
        words = "Done" + (" - " + ", ".join(extra) if extra else "")
        text = text[:600] + ("..." if len(text) > 600 else "")
    said = QLabel("<b>%s</b>%s" % (html.escape(words), (": " + html.escape(text)) if text else ""))
    said.setTextFormat(Qt.RichText)
    said.setWordWrap(True)
    said.setTextInteractionFlags(Qt.TextSelectableByMouse)
    if kind in ("failed", "timed_out"):
        said.setProperty("bad", "true")
    layout.addWidget(said, 1)
    return row
