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

from .. import widgets

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
        chosen = self.text.textCursor().selectedText().replace(" ", "\n").strip()
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
        self.said = "Changed %d file%s · %s" % (files, "" if files == 1 else "s",
                                                 counts_text(event.get("added"),
                                                             event.get("removed")))
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
