"""What a planning agent hands the person, in the chat's feed.

A planning turn ends with the agent's questions or with its plan
(``integrations/agents/adapters/claude.py``), and each is a card the person
acts on where it stands:

- **Questions**: each with the agent's options - one or several to choose, and
  a line for an answer of one's own. *Send answers* is the next planning turn.
- **Plan**: the plan as the Markdown it was written in, and a box for
  comments. A part of the plan can be quoted into the box from a selection,
  so a comment says what it is about. *Revise* sends the comments back for
  another plan; *Start: Edit* or *Start: Full* approves it, comments going
  along as notes, and the next turn works.

Only the newest card waits for an answer, and only while nothing else is
going on in the conversation: once something is said, every card before it is
history and says so. A plan revised is folded under the one that replaced it.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QRadioButton,
                               QVBoxLayout, QWidget)

from .. import icons, theme, widgets

#: What the next turn is told, around what the person chose or wrote.
ANSWERS = "Answers to your questions:"
NO_ANSWER = "(no answer - decide yourself and say what you chose)"
REVISE = "Revise the plan:"
APPROVED = "The plan is approved. Carry it out."
NOTES = "Notes:"


def _head(kind, meta=""):
    row = QHBoxLayout()
    row.setSpacing(8)
    label = QLabel(kind)
    label.setProperty("role", "msgkind")
    row.addWidget(label)
    said = QLabel(meta)
    said.setProperty("role", "msgmeta")
    row.addWidget(said)
    row.addStretch(1)
    return row, label, said


def _settled(frame, settled):
    frame.setProperty("settled", "true" if settled else "false")
    frame.style().unpolish(frame)
    frame.style().polish(frame)


class QuestionsCard(QFrame):
    """The agent's questions, each answered by choosing or writing."""

    #: The message that answers them, ready to send.
    answered = Signal(str)

    def __init__(self, items):
        super().__init__()
        self.setProperty("role", "agentcard")
        self.items = [dict(one) for one in items or []]
        self.said = "\n".join(one.get("question", "") for one in self.items)
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 6, 10, 8)
        column.setSpacing(6)
        head, _kind, self.meta = _head("QUESTIONS", "before the plan")
        column.addLayout(head)
        self.choices = []                   # per question: (buttons, other button, other line)
        for number, item in enumerate(self.items, 1):
            question = QLabel("%d. %s" % (number, item.get("question", "")))
            question.setProperty("role", "cardquestion")
            question.setTextFormat(Qt.PlainText)
            question.setWordWrap(True)
            question.setTextInteractionFlags(Qt.TextSelectableByMouse)
            column.addWidget(question)
            several = bool(item.get("multiple"))
            group = QButtonGroup(self)
            group.setExclusive(not several)
            buttons = []
            for option in item.get("options") or []:
                button = (QCheckBox if several else QRadioButton)(option)
                group.addButton(button)
                button.toggled.connect(self._sync)
                column.addWidget(button)
                buttons.append(button)
            other = (QCheckBox if several else QRadioButton)("Other:")
            group.addButton(other)
            line = QLineEdit()
            line.setPlaceholderText("Your own answer")
            line.textEdited.connect(lambda _text, other=other: other.setChecked(True))
            line.textChanged.connect(self._sync)
            other.toggled.connect(self._sync)
            row = QWidget()
            across = QHBoxLayout(row)
            across.setContentsMargins(0, 0, 0, 0)
            across.addWidget(other)
            across.addWidget(line, 1)
            column.addWidget(row)
            self.choices.append((buttons, other, line))
        self.send = QPushButton("Send answers")
        self.send.setProperty("variant", "primary")
        self.send.clicked.connect(self._send)
        column.addWidget(widgets.row(None, self.send))
        self.set_actionable(False)

    def answers(self):
        """What was chosen for each question, in order: a list of strings."""
        found = []
        for (buttons, other, line), _item in zip(self.choices, self.items):
            chosen = [button.text() for button in buttons if button.isChecked()]
            if other.isChecked() and line.text().strip():
                chosen.append(line.text().strip())
            found.append(chosen)
        return found

    def message(self):
        lines = [ANSWERS]
        for number, (item, chosen) in enumerate(zip(self.items, self.answers()), 1):
            lines.append("%d. %s" % (number, item.get("question", "")))
            lines.append("   " + ("; ".join(chosen) if chosen else NO_ANSWER))
        return "\n".join(lines)

    def set_actionable(self, actionable):
        for buttons, other, line in self.choices:
            for button in buttons + [other]:
                button.setEnabled(actionable)
            line.setEnabled(actionable)
        self.send.setVisible(actionable)
        self._sync()

    def settle(self):
        """Something was said since: these are history."""
        self.set_actionable(False)
        self.meta.setText("answered" if any(self.answers()) else "")
        _settled(self, True)

    def _sync(self, *_args):
        self.send.setEnabled(any(self.answers()))

    def _send(self):
        if any(self.answers()):
            self.answered.emit(self.message())


class PlanCard(QFrame):
    """The agent's plan, a box for comments on it, and what to do with it."""

    #: Send the comments back for another plan: the message, ready to send.
    revise = Signal(str)
    #: Approve it: the access to work with ("edit" or "full") and the message.
    approve = Signal(str, str)

    def __init__(self, text, version):
        super().__init__()
        from .agentchat import AnswerText     # the feed's own Markdown, laid out alike
        self.setProperty("role", "agentcard")
        self.said = str(text or "")
        self.version = version
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 6, 10, 8)
        column.setSpacing(6)
        head, self.kind, self.meta = _head("PLAN · VERSION %d" % version)
        self.fold = QPushButton("Show")
        self.fold.setProperty("variant", "ghost")
        self.fold.setVisible(False)
        self.fold.clicked.connect(self._unfold)
        head.addWidget(self.fold)
        column.addLayout(head)
        self.body = QWidget()
        inner = QVBoxLayout(self.body)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(6)
        self.text = AnswerText(self.said)
        inner.addWidget(self.text)
        self.comments = QPlainTextEdit()
        self.comments.setPlaceholderText(
            "Comments on the plan. Select a part of it and press Quote to say "
            "which part a comment is about.")
        self.comments.setFixedHeight(64)
        self.comments.textChanged.connect(self._sync)
        inner.addWidget(self.comments)
        self.quote = QPushButton("Quote selection")
        self.quote.setProperty("variant", "ghost")
        self.quote.setToolTip("Put the part of the plan selected above into the comments")
        self.quote.clicked.connect(self._quote)
        self.revise_button = QPushButton("Revise")
        self.revise_button.setToolTip("Send the comments back; the agent plans again")
        self.revise_button.clicked.connect(self._revise)
        self.start_edit = QPushButton("Start: Edit")
        self.start_edit.setProperty("variant", "primary")
        self.start_edit.setToolTip("Approve the plan: the agent works on it and may change "
                                   "files. The comments go along as notes.")
        self.start_edit.clicked.connect(lambda: self._start("edit"))
        self.start_full = QPushButton("Start: Full")
        self.start_full.setToolTip("Approve the plan: the agent works on it and may also "
                                   "run commands. The comments go along as notes.")
        self.start_full.clicked.connect(lambda: self._start("full"))
        self.actions = widgets.row(self.quote, None, self.revise_button, self.start_edit,
                                   self.start_full)
        inner.addWidget(self.actions)
        column.addWidget(self.body)
        self.set_actionable(False)

    def set_actionable(self, actionable):
        self.comments.setVisible(actionable)
        self.actions.setVisible(actionable)
        self._sync()

    def settle(self, word=""):
        self.set_actionable(False)
        if word:
            self.meta.setText(word)
        _settled(self, True)

    def supersede(self):
        """A newer plan replaced this one: folded, one click from being read."""
        self.settle("revised below")
        self.body.setVisible(False)
        self.fold.setVisible(True)

    def _unfold(self):
        shown = self.body.isHidden()
        self.body.setVisible(shown)
        self.fold.setText("Hide" if shown else "Show")

    def _sync(self, *_args):
        self.revise_button.setEnabled(bool(self.comments.toPlainText().strip()))

    def _quote(self):
        # A selection across paragraphs comes back with Qt's own separator.
        # Written as an escape: the character itself is a line terminator to editors.
        chosen = self.text.textCursor().selectedText().replace("\u2029", "\n").strip()
        if not chosen:
            return
        quoted = "\n".join("> " + line for line in chosen.splitlines())
        typed = self.comments.toPlainText().rstrip()
        self.comments.setPlainText("\n\n".join(part for part in (typed, quoted) if part) + "\n")
        cursor = self.comments.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.comments.setTextCursor(cursor)
        self.comments.setFocus()

    def _revise(self):
        comments = self.comments.toPlainText().strip()
        if comments:
            self.revise.emit("%s\n\n%s" % (REVISE, comments))

    def _start(self, mode):
        comments = self.comments.toPlainText().strip()
        said = APPROVED + ("\n\n%s\n%s" % (NOTES, comments) if comments else "")
        self.approve.emit(mode, said)


class ChangesLine(QWidget):
    """What a turn changed in the folder, in a line - and the way to see it."""

    wanted = Signal()

    def __init__(self, event):
        super().__init__()
        from ..diffview import counts_text
        files = event.get("files") or 0
        self.said = " · ".join(part for part in (
            "Changed %d file%s" % (files, "" if files == 1 else "s"),
            counts_text(event.get("added"), event.get("removed"))) if part)
        if event.get("kept") in ("partial", "none"):
            self.said += " · part of the folder is too large to compare"
        across = QHBoxLayout(self)
        across.setContentsMargins(0, 0, 0, 0)
        across.setSpacing(8)
        label = QLabel(self.said)
        label.setProperty("role", "muted")
        across.addWidget(label)
        self.view = QPushButton("View")
        self.view.setProperty("variant", "ghost")
        self.view.setToolTip("What this turn changed, file by file")
        self.view.clicked.connect(self.wanted)
        across.addWidget(self.view)
        across.addStretch(1)


# -- other agents (ADR-006) ----------------------------------------------------------
#: What a waiting request says in its head, by how it stands.
REQUEST_STATES = {"waiting": "waits for you", "approved": "approved - working", "running": "working",
                  "succeeded": "done", "failed": "did not finish", "denied": "denied",
                  "canceled": "canceled", "timed_out": "ran out of time", "unknown": "outcome unknown",
                  "asks": "asks - its question is below"}

#: What the next message is prefixed with when other agents wrote to this conversation.
INBOX = "Messages from other agents, sent to you since your last turn:"


class AgentRequestCard(QFrame):
    """A task this conversation's agent hands another agent, or a message it
    sends - waiting for the person's yes when the conversation says Approve,
    then telling how it went (ADR-006 D3)."""

    approve = Signal(str)
    deny = Signal(str)
    open = Signal(str)
    dismiss = Signal(str)
    #: The person stops a task that goes on through answers (ADR-011).
    stop = Signal(str)

    def __init__(self, request, dismissible=True):
        super().__init__()
        self.setProperty("role", "agentcard")
        self.request = dict(request)
        #: In a conversation an ended task stays as its record.
        self.dismissible = dismissible
        self.execution_id = self.request.get("execution_id") or ""
        message = self.request.get("kind") == "message"
        program = self.request.get("target_name") or self.request.get("target") or ""
        target = program or "another agent"
        if self.request.get("role"):
            # A role the person made: named first, its program after when known (ADR-010).
            target = "%s (%s)" % (self.request["role"], program) if program else self.request["role"]
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 6, 10, 8)
        column.setSpacing(6)
        title = ("QUESTION FOR %s" if self.request.get("expects_reply") else "MESSAGE TO %s") if message \
            else "TASK FOR %s"
        head, self.kind, self.meta = _head(title % target.upper())
        column.addLayout(head)
        self.text = QLabel(self.request.get("body") if message else self.request.get("goal") or "")
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        column.addWidget(self.text)
        details = []
        if not message and self.request.get("mode"):
            details.append("Mode: %s" % self.request["mode"].capitalize())
        # What the asking agent chose for the other's turn; none is its defaults.
        if not message or self.request.get("expects_reply"):
            for key, word in (("model", "Model"), ("effort", "Effort")):
                if self.request.get(key):
                    details.append("%s: %s" % (word, self.request[key]))
        if not message:
            for key, word in (("files", "file"), ("artifacts", "earlier run")):
                count = int(self.request.get(key) or 0)
                if count:
                    details.append("%d %s%s" % (count, word, "s" if count != 1 else ""))
        self.details = QLabel(" · ".join(details))
        self.details.setProperty("role", "hint")
        self.details.setVisible(bool(details))
        column.addWidget(self.details)
        self.result = QLabel("")
        self.result.setProperty("role", "hint")
        self.result.setWordWrap(True)
        self.result.setVisible(False)
        column.addWidget(self.result)
        self.deny_button = QPushButton("Deny")
        self.deny_button.setToolTip("Nothing runs; the agent hears it was denied.")
        self.deny_button.clicked.connect(lambda: self.deny.emit(self.execution_id))
        self.approve_button = QPushButton("Approve")
        self.approve_button.setProperty("variant", "primary")
        self.approve_button.setToolTip("Let it go to %s." % target)
        self.approve_button.clicked.connect(lambda: self.approve.emit(self.execution_id))
        self.open_button = QPushButton("Open conversation")
        self.open_button.setProperty("variant", "ghost")
        self.open_button.clicked.connect(lambda: self.open.emit(self.request.get("session_id") or ""))
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.setProperty("variant", "ghost")
        self.dismiss_button.clicked.connect(lambda: self.dismiss.emit(self.execution_id))
        self.stop_button = QPushButton("Stop")
        self.stop_button.setProperty("variant", "ghost")
        self.stop_button.setToolTip("End this task here: it takes no more answers, and what of it runs is "
                                    "canceled. Its conversation keeps what it found.")
        self.stop_button.clicked.connect(lambda: self.stop.emit(self.execution_id))
        self.actions = widgets.row(self.open_button, None, self.dismiss_button, self.stop_button,
                                   self.deny_button, self.approve_button)
        column.addWidget(self.actions)
        widgets.see_through(self)
        self.set_state(self.request.get("state") or "waiting")

    def set_state(self, state, session_id="", detail="", questions=0):
        self.request["state"] = state
        if session_id:
            self.request["session_id"] = session_id
        # How long it has gone on through answers: counted, for the person to judge.
        self.meta.setText(REQUEST_STATES.get(state, state) + (
            " - %d question%s so far" % (questions, "" if questions == 1 else "s") if questions else ""))
        self.stop_button.setVisible(state in ("running", "asks") and self.request.get("kind") != "message")
        waiting = state == "waiting"
        ended = state not in ("waiting", "approved", "running", "asks")
        self.deny_button.setVisible(waiting)
        self.approve_button.setVisible(waiting)
        self.open_button.setVisible(bool(self.request.get("session_id")))
        self.dismiss_button.setVisible(ended and self.dismissible)
        self.result.setText(detail)
        self.result.setVisible(bool(detail))
        _settled(self, ended)

def _marked_head(name, kind, meta=""):
    """A card's heading with its direction in front: what came in, or what went out."""
    head, label, said = _head(kind, meta)
    mark = QLabel()
    mark.setObjectName("mark_" + name)
    mark.setPixmap(icons.pixmap(name, 16, theme.WARN_INK if name == "asked" else theme.ACCENT))
    mark.setFixedSize(16, 16)
    head.insertWidget(0, mark)
    return head, label, said


class TaskQuestionCard(QFrame):
    """What a delegated task asks this conversation (ADR-011): answered here by
    the person, or by the agent that handed the task on. One card a question."""

    #: (task execution id, answer)
    reply = Signal(str, str)

    def __init__(self, question):
        super().__init__()
        self.setProperty("role", "agentcard")
        self.question = dict(question)
        self.execution_id = self.question.get("execution_id") or ""
        who = self.question.get("who") or "The other agent"
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 6, 10, 8)
        column.setSpacing(6)
        head, self.kind, self.meta = _marked_head("asked", "QUESTION FROM %s" % who.upper())
        column.addLayout(head)
        self.text = QLabel(self.question.get("question") or "")
        self.text.setProperty("role", "cardquestion")
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        column.addWidget(self.text)
        self.goal = QLabel("Task: " + (self.question.get("goal") or ""))
        self.goal.setProperty("role", "hint")
        self.goal.setWordWrap(True)
        self.goal.setVisible(bool(self.question.get("goal")))
        column.addWidget(self.goal)
        self.answer_box = QPlainTextEdit()
        self.answer_box.setObjectName("taskAnswer")
        self.answer_box.setPlaceholderText("Your answer to %s - or let the agent that handed it on answer." % who)
        self.answer_box.setFixedHeight(64)
        self.answer_box.textChanged.connect(self._ready)
        column.addWidget(self.answer_box)
        self.answer_button = QPushButton("Answer")
        self.answer_button.setProperty("variant", "primary")
        self.answer_button.setToolTip("Goes on in the same conversation: %s keeps what it has already read." % who)
        self.answer_button.clicked.connect(self._send)
        self.actions = widgets.row(None, self.answer_button)
        column.addWidget(self.actions)
        widgets.see_through(self)
        stopped = bool(self.question.get("stopped"))
        self.set_open(not self.question.get("answered") and not stopped, stopped=stopped)

    def set_open(self, waiting, stopped=False):
        """Waiting for an answer, or answered - its answer is the next card -
        or not answered: the task was stopped."""
        number = self.question.get("number") or 1
        said = "waits for an answer" if waiting else ("not answered - the task was stopped" if stopped
                                                       else "answered")
        self.meta.setText("%s - question %d" % (said, number))
        self.answer_box.setVisible(waiting)
        self.actions.setVisible(waiting)
        self._ready()
        _settled(self, not waiting)

    def _ready(self):
        self.answer_button.setEnabled(bool(self.answer_box.toPlainText().strip()))

    def _send(self):
        text = self.answer_box.toPlainText().strip()
        if text:
            self.answer_button.setEnabled(False)
            self.reply.emit(self.execution_id, text)


class TaskAnswerCard(QFrame):
    """What a delegated task was told in answer: by the person, or by the agent
    that handed it on (ADR-011). Its stripe is the accent: it went out from here."""

    def __init__(self, answer):
        super().__init__()
        self.setProperty("role", "agentcard")
        self.setProperty("variant", "answer")
        self.answer = dict(answer)
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 6, 10, 8)
        column.setSpacing(6)
        by = self.answer.get("by") or "You"
        who = self.answer.get("who") or "the other agent"
        head, self.kind, self.meta = _marked_head("answered", "ANSWER TO %s" % who.upper(),
                                                  "from " + ("you" if by == "You" else by))
        column.addLayout(head)
        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        column.addWidget(self.text)
        self.note = QLabel("")
        self.note.setProperty("role", "hint")
        self.note.setWordWrap(True)
        column.addWidget(self.note)
        widgets.see_through(self)
        _settled(self, True)
        self.set_answer(self.answer)

    def set_answer(self, answer):
        """The answer that went - another's, when the person's came too late."""
        self.answer = dict(answer)
        by = self.answer.get("by") or "You"
        self.meta.setText("from " + ("you" if by == "You" else by))
        self.text.setText(self.answer.get("answer") or "")
        late = self.answer.get("late") or ""
        self.note.setText(("Your answer was not sent - it was answered first: %s" % late) if late else "")
        self.note.setVisible(bool(late))


class InboxCard(QFrame):
    """A message another agent sent this conversation, or its connection: it goes
    with the person's next message, unless dismissed (ADR-006 D4)."""

    dismiss = Signal(str)

    def __init__(self, message):
        super().__init__()
        self.setProperty("role", "agentcard")
        self.message = dict(message)
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 6, 10, 8)
        column.setSpacing(6)
        sender = self.message.get("sender_connection") or "another agent"
        kind = "REPLY FROM %s" if self.message.get("kind") == "reply" else "MESSAGE FROM %s"
        head, self.kind, self.meta = _head(kind % sender.upper(), "goes with your next message")
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.setProperty("variant", "ghost")
        self.dismiss_button.setToolTip("Mark it read without passing it on.")
        self.dismiss_button.clicked.connect(lambda: self.dismiss.emit(self.message.get("message_id") or ""))
        head.addWidget(self.dismiss_button)
        column.addLayout(head)
        self.text = QLabel(self.message.get("body") or "")
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        column.addWidget(self.text)
        widgets.see_through(self)


#: How an agent's change to an integration stands, as its card says it (ADR-007).
CHANGE_STATES = {"waiting": "waits for you", "approved": "approved - sending", "running": "sending",
                 "succeeded": "done", "failed": "not made", "denied": "denied",
                 "canceled": "canceled", "timed_out": "not answered in time",
                 "unknown": "outcome unknown"}


def change_title(card):
    """What a change does, as its provider says it: ``Comment on QA-142``."""
    return str(card.get("title") or "%s %s" % (str(card.get("operation") or "change").capitalize(),
                                               card.get("key") or "")).strip()


class IntegrationChangeCard(QFrame):
    """A change an agent proposes to an integration - a Jira comment, a move to
    another status, an edit - waiting for the person. What it would change is
    as the provider says it (``changes``: from - to); its text fields
    (``edit``) can be changed before Approve, and what is approved is what is
    written (ADR-007 D7)."""

    approve = Signal(str)
    deny = Signal(str)
    dismiss = Signal(str)
    #: ``(execution id, field, text)`` while the person changes an editable field.
    edited = Signal(str, str, str)

    def __init__(self, change, show_agent=False, dismissible=True):
        super().__init__()
        self.setProperty("role", "agentcard")
        self.change = dict(change)
        #: In a conversation an ended change stays as its record; a list may let it go.
        self.dismissible = dismissible
        self.execution_id = self.change.get("execution_id") or ""
        card = self.change.get("card") or {}
        draft = self.change.get("draft") or {}
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 6, 10, 8)
        column.setSpacing(6)
        head, self.kind, self.meta = _head(change_title(card).upper())
        column.addLayout(head)
        where = [card.get("summary") or "", "Jira: %s" % (card.get("connection_name") or card.get("connection") or "")]
        if show_agent and self.change.get("agent"):
            where.append("asked by %s" % self.change["agent"])
        self.where = QLabel(" · ".join(one for one in where if one))
        self.where.setProperty("role", "hint")
        self.where.setWordWrap(True)
        column.addWidget(self.where)
        #: One line per change that is not text to edit: "Status: from In Progress to Review".
        self.lines = []
        for one in card.get("changes") or []:
            said = ("%s: from %s to %s" % (one.get("label"), one.get("from"), one.get("to")) if one.get("from")
                    else "%s: %s" % (one.get("label"), one.get("to")))
            line = QLabel(said)
            line.setWordWrap(True)
            line.setTextInteractionFlags(Qt.TextSelectableByMouse)
            column.addWidget(line)
            self.lines.append(line)
        #: field -> its editor; a required one cannot be approved empty.
        self.fields, self._required, self.shown = {}, set(), {}
        for name, spec in (card.get("edit") or {}).items():
            label = QLabel(str(spec.get("label") or name).upper())
            label.setProperty("role", "kicker")
            column.addWidget(label)
            value = draft.get(name, spec.get("value") or "")
            if spec.get("multiline", True):
                editor = QPlainTextEdit()
                editor.setPlainText(value)
                editor.setMaximumHeight(140)
                editor.textChanged.connect(lambda name=name, editor=editor: self.edited.emit(
                    self.execution_id, name, editor.toPlainText()))
            else:
                editor = QLineEdit(value)
                editor.textChanged.connect(lambda text, name=name: self.edited.emit(self.execution_id, name, text))
            editor.setAccessibleName(str(spec.get("label") or name))
            column.addWidget(editor)
            # Once answered, the text as it went - no empty box around it.
            shown = QLabel("")
            shown.setWordWrap(True)
            shown.setTextInteractionFlags(Qt.TextSelectableByMouse)
            shown.setVisible(False)
            column.addWidget(shown)
            self.shown[name] = shown
            if spec.get("was"):
                was = QLabel("Now: %s" % spec["was"])
                was.setProperty("role", "hint")
                was.setWordWrap(True)
                column.addWidget(was)
            self.fields[name] = editor
            if spec.get("required"):
                self._required.add(name)
        self.notes = QLabel("\n".join(card.get("notes") or []))
        self.notes.setProperty("role", "hint")
        self.notes.setWordWrap(True)
        self.notes.setVisible(bool(card.get("notes")))
        column.addWidget(self.notes)
        self.result = QLabel("")
        self.result.setProperty("role", "hint")
        self.result.setWordWrap(True)
        self.result.setVisible(False)
        column.addWidget(self.result)
        self.deny_button = QPushButton("Deny")
        self.deny_button.setToolTip("Nothing is changed in Jira; the agent hears it was denied.")
        self.deny_button.clicked.connect(lambda: self.deny.emit(self.execution_id))
        self.approve_button = QPushButton("Approve")
        self.approve_button.setProperty("variant", "primary")
        self.approve_button.setToolTip("Make this change in Jira, as it reads now. It cannot be taken back here.")
        self.approve_button.clicked.connect(self._approve)
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.setProperty("variant", "ghost")
        self.dismiss_button.clicked.connect(lambda: self.dismiss.emit(self.execution_id))
        column.addWidget(widgets.row(None, self.dismiss_button, self.deny_button, self.approve_button))
        widgets.see_through(self)
        self.set_state(self.change.get("state") or "waiting", self.change.get("detail") or "")

    def text_of(self, name):
        editor = self.fields[name]
        return editor.toPlainText() if isinstance(editor, QPlainTextEdit) else editor.text()

    def _approve(self):
        empty = [name for name in self._required if not self.text_of(name).strip()]
        if empty:
            label = ((self.change.get("card") or {}).get("edit") or {}).get(empty[0], {}).get("label") or empty[0]
            self.result.setText("%s cannot be empty." % label)
            self.result.setVisible(True)
            return
        self.approve.emit(self.execution_id)

    def set_state(self, state, detail=""):
        self.change["state"] = state
        self.meta.setText(CHANGE_STATES.get(state, state))
        waiting = state == "waiting"
        self.deny_button.setVisible(waiting)
        self.approve_button.setVisible(waiting)
        self.dismiss_button.setVisible(self.dismissible and state in (
            "succeeded", "failed", "denied", "canceled", "timed_out", "unknown"))
        for name, editor in self.fields.items():
            editor.setReadOnly(not waiting)
            editor.setVisible(waiting)
            self.shown[name].setText(self.text_of(name) or "(empty)")
            self.shown[name].setVisible(not waiting)
        self.result.setText(detail)
        self.result.setVisible(bool(detail))
        _settled(self, not waiting and state not in ("approved", "running"))


#: What starts the turn QAVector takes for a conversation whose tasks ended (ADR-009 D4).
WAKE_HEAD = "[QAVector: what you handed on to other agents has ended]"
WAKE_WHY = {"all": "everything you asked to be woken for has ended",
            "any": "one of the tasks you asked about has ended",
            "failure": "one of them did not succeed",
            "round": "everything you handed on in your last turn has ended"}
WAKE_STATES = {"succeeded": "done", "failed": "failed", "skipped": "skipped - what it waited for failed",
               "rework": "sent back for rework",
               "denied": "denied by the person", "canceled": "canceled", "timed_out": "ran out of time",
               "unknown": "outcome unknown", "asks": "asks you a question",
               "stopped": "stopped by the person"}
#: What the woken agent is told to do with a question (ADR-011 D4).
WAKE_REPLY = ('  Answer it with agents.tasks.reply {"task": "%s", "answer": "..."}: it goes on in its own '
              'conversation with what it has read. Do not delegate it again; ask the person only what you '
              'cannot decide.')
WAKE_TODO = ("Check each acceptance criterion against evidence and record it with agents.tasks.verify. "
             "Then carry on with what the person asked - hand on more work if it is needed - and tell "
             "the person what came of it.")


def wake_message(found):
    """What the woken turn is told: why, each ended item, what still runs, what to do."""
    lines = [WAKE_HEAD, "Why now: %s." % WAKE_WHY.get((found.get("condition") or {}).get("why"), "they ended"), ""]
    for one in found.get("items") or []:
        who = one.get("target_name") or one.get("target") or "another agent"
        if one.get("role"):
            who = "%s (%s)" % (one["role"], who)
        with_ = " ".join(part for part in (one.get("model"), one.get("effort")) if part)
        head = "- %s%s (%s for %s%s): %s." % (
            one.get("name") + " - " if one.get("name") else "", (one.get("goal") or "")[:200],
            "question" if one.get("kind") == "question" else "task", who, ", " + with_ if with_ else "",
            WAKE_STATES.get(one.get("state"), one.get("state") or ""))
        lines.append(head)
        if one.get("state") == "asks":
            lines.append("  It asks: " + (one.get("question") or ""))
            lines.append(WAKE_REPLY % one.get("execution_id"))
            lines.append("  Execution: %s." % one.get("execution_id"))
            continue
        if one.get("answer"):
            lines.append("  Answer: " + one["answer"])
        if one.get("error") and one.get("state") != "succeeded":
            lines.append("  Why: " + one["error"])
        if one.get("verdict") == "no_verdict":
            lines.append("  It gave no verdict, so it counts as sent back.")
        for issue in one.get("issues") or []:
            lines.append("  Issue: " + issue)
        criteria = [str(c.get("criterion") or "") for c in one.get("acceptance") or [] if isinstance(c, dict)]
        if criteria:
            lines.append("  Acceptance criteria, not verified yet: " + "; ".join(criteria))
        if one.get("files_changed"):
            lines.append("  Files changed: %d." % one["files_changed"])
        lines.append("  Execution: %s." % one.get("execution_id"))
    if found.get("running"):
        lines.append("Still running: %s." % ", ".join(found["running"]))
    lines += ["", WAKE_TODO]
    return "\n".join(lines)


class WakeCard(QFrame):
    """Tasks a conversation handed on have ended, and it waits for the person's
    word to go on with their results (ADR-009 D3/D6)."""

    go = Signal(str)
    dismiss = Signal(str)

    def __init__(self, key, found, reason="ask"):
        super().__init__()
        self.setProperty("role", "agentcard")
        self.key = key
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 6, 10, 8)
        column.setSpacing(6)
        items = found.get("items") or []
        head, self.kind, self.meta = _head("TASKS FINISHED", "waits for you")
        column.addLayout(head)
        self.lines = []
        for one in items:
            who = one.get("target_name") or one.get("target") or "another agent"
            line = QLabel("%s - %s: %s" % (one.get("name") or (one.get("goal") or "")[:60], who,
                                           WAKE_STATES.get(one.get("state"), one.get("state") or "")))
            line.setWordWrap(True)
            column.addWidget(line)
            self.lines.append(line)
        notes = []
        if found.get("running"):
            notes.append("Still running: %s." % ", ".join(found["running"]))
        if reason == "limit":
            notes.append("It went on by itself as far as allowed after failed rounds; it waits for you now.")
        self.note = QLabel("\n".join(notes))
        self.note.setProperty("role", "hint")
        self.note.setWordWrap(True)
        self.note.setVisible(bool(notes))
        column.addWidget(self.note)
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.setProperty("variant", "ghost")
        self.dismiss_button.setToolTip("Leave it; the results stay on their cards.")
        self.dismiss_button.clicked.connect(lambda: self.dismiss.emit(self.key))
        self.go_button = QPushButton("Continue")
        self.go_button.setProperty("variant", "primary")
        self.go_button.setToolTip("The agent goes on with these results.")
        self.go_button.clicked.connect(lambda: self.go.emit(self.key))
        column.addWidget(widgets.row(None, self.dismiss_button, self.go_button))
        widgets.see_through(self)


def inbox_prefix(messages):
    """What the next message starts with: each waiting message, who sent it."""
    lines = [INBOX]
    for one in messages:
        kind = "reply" if one.get("kind") == "reply" else "message"
        lines.append("- %s from %s: %s" % (kind, one.get("sender_connection") or "another agent",
                                         one.get("body") or ""))
    return "\n".join(lines) + "\n\n"
