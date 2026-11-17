"""The Chat tab of Integrations -> Agents: conversations with an agent.

On the left, the project's conversations; on the right, the one that is open -
what was said, what the agent did, and the box to say the next thing in. A
conversation is with one connection, in one folder, and each turn says how much
the agent may do there: read, edit, or everything.

**Typing is all it takes.** There is no "new conversation" form: the box is
there when the tab opens, and the first message makes the conversation. Enter
sends, Shift+Enter starts a line - the composer under Stages works the same way.

**What is shown is what is on disk.** The core writes every event down before
it says it (``integrations/agents/runner.py``), so the feed is a view of the
record and nothing else. Closing the window mid-turn loses nothing, and a
conversation opened again reads exactly as it did.

**Nothing is sent twice, and nothing typed is lost.** A message written while
the agent is answering waits its turn. If that turn fails or is stopped, what
was waiting goes back into the box rather than after it: a person decides what
is said next to an agent that just did something unexpected.
"""

import os
import time

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QDesktopServices, QFont, QIcon, QPixmap, QTextCharFormat, QTextCursor,
                           QTextDocument, QTextFormat)
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFrame,
                               QHBoxLayout,
                               QHeaderView, QLabel, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QTableWidgetItem, QTextBrowser,
                               QVBoxLayout, QWidget)

from .. import icons, restrictions as restrictions_mod, stages, theme, widgets
from ..diffview import ChangesDialog
from ..agentturn import AgentTurn
from ..agentmodel import ModelPicker
from . import agentcards
from .agentcards import ChangesLine, IntegrationChangeCard, PlanCard, QuestionsCard
from .agentwords import (ago_text, clock_text, context_text, cost_text, end_text, folder_text,
                         refused_text, tokens_text, took_text)

#: How much the agent may do in a turn, as the composer offers it. (label, mode)
MODES = (("Read", "read"), ("Edit", "edit"), ("Full", "full"))
MODE_MEANS = ("Read: it reads the folder and changes nothing. Edit: it also changes "
              "files. Full: it also runs commands - everything this account can do "
              "inside the folder's restrictions.")
#: What a conversation may do with other agents (ADR-006 D3), as the footer says it.
OTHERS = (("Auto", "auto"), ("Approve", "approve"), ("Deny", "deny"))
#: Who said a message QAVector sent for the conversation (ADR-009 D4).
FROM_QAVECTOR = "QAVECTOR · TASKS FINISHED"
#: ADR-009 D6: turns QAVector takes by itself per message of the person -
#: failed rounds, and in all - before it asks instead.
WAKE_FAILED_ROUNDS, WAKE_ROUNDS = 3, 10
#: How a conversation goes on when the tasks it handed on end (ADR-009 D3).
WAKE = (("Continue", "continue"), ("Ask me", "ask"), ("Off", "off"))
WAKE_MEANS = ("When the tasks this conversation handed to other agents end - Continue: it goes on by "
              "itself with their results. Ask me: a card waits for you. Off: the results are only on "
              "their cards.")
OTHERS_MEANS = ("Other agents - Auto: it hands tasks to and messages the agents its connection "
                "allows, without asking. Approve: each one waits here for your yes. Deny: it works "
                "alone.")
OTHERS_NONE = ("This connection allows no other agents. Tick them in its settings "
               "(Integrations > Agents > Connections) to let a conversation work with them.")
PLAN_MEANS = ("The agent first looks through the folder, changing nothing, and answers "
              "with its questions or a plan. The work starts when you approve the plan, "
              "with the access you choose then.")

#: How many of a conversation's newest events are read when it is opened, and
#: how many conversations the list reads.
EVENTS_SHOWN = 400
SESSIONS_SHOWN = 200

#: How many folders the composer remembers.
RECENT_FOLDERS = 8

#: How tall the box may grow, in lines, before it scrolls.
MAX_LINES = 6

#: What the feed says when nothing has been said.
NEW = ("Say what you need. The agent works in the folder chosen below, and may "
       "do there what the mode beside it says.")

#: What a conversation started by something other than this page is called for.
CHAT = "Chat"


def _is_chat(session):
    """Whether a conversation can be carried on here. The core says, as
    ``record``: a conversation another part of QAVector opened to talk in - a
    task handed over from Jira - goes on in the chat like any other; one that
    only records turns taken elsewhere - a cycle step's - is read."""
    session = session or {}
    if "record" in session:
        return not session["record"]
    source = session.get("source") or {}
    if "record" in source:
        return not source["record"]
    # Saved before a source could say so: a ref is what opens a conversation
    # to talk in, and only the chat's own were carried on before that.
    return bool(source.get("ref")) or source.get("label", CHAT) == CHAT


# -- the feed -------------------------------------------------------------------
class MessageCard(QFrame):
    """What a person said: a card, so it is told from what the agent said."""

    removed = Signal()
    interrupt_requested = Signal()

    def __init__(self, text, when="", state="", removable=False, attachments=None, who="YOU"):
        super().__init__()
        self.setProperty("role", "message")
        if who != "YOU":
            self.setProperty("from", "qavector")     # its own stripe and wash (theme: AUTO)
        self.said = str(text or "")
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 6, 10, 7)
        column.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(8)
        kind = QLabel(who)
        kind.setProperty("role", "msgkind")
        head.addWidget(kind)
        self.meta = QLabel(when)
        self.meta.setProperty("role", "msgmeta")
        head.addWidget(self.meta)
        head.addStretch(1)
        self.remove = None
        if removable:
            self.interrupt = QPushButton("Interrupt & send")
            self.interrupt.setProperty("variant", "ghost")
            self.interrupt.setToolTip("Stop the current turn, then send this message in the same chat. Existing changes remain.")
            self.interrupt.clicked.connect(self.interrupt_requested)
            head.addWidget(self.interrupt)
            self.remove = QPushButton("Remove")
            self.remove.setProperty("variant", "ghost")
            self.remove.setCursor(Qt.PointingHandCursor)
            self.remove.setToolTip("Take it out of the queue; it is not sent.")
            self.remove.clicked.connect(self.removed)
            head.addWidget(self.remove)
        self.state = QLabel({"sending": "SENDING", "queued": "QUEUED"}.get(state, ""))
        self.state.setProperty("role", "msgstate")
        # Outlined, as a clarification still on its way is under Stages.
        self.state.setProperty("state", "pending" if state else "")
        self.state.setVisible(bool(state))
        head.addWidget(self.state)
        column.addLayout(head)
        self.text = QLabel(self.said)
        self.text.setProperty("role", "msgtext")
        # What somebody wrote is text, always: never markup.
        self.text.setTextFormat(Qt.PlainText)
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        column.addWidget(self.text)
        self.text.setVisible(bool(self.said))
        self.files = []
        for one in attachments or []:
            name = one.get("name", "") if isinstance(one, dict) else os.path.basename(one)
            label = QLabel("File: " + name)
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setProperty("role", "msgmeta")
            column.addWidget(label)
            self.files.append(label)


class AnswerText(QTextBrowser):
    """What the agent said, laid out as the Markdown it writes.

    As tall as what it holds and never scrolling by itself: the feed scrolls.
    It renders text and nothing else - raw HTML is shown as written, and an
    image names a file or an address this never opens.
    """

    def __init__(self, text):
        super().__init__()
        self.setProperty("role", "answer")
        self.said = str(text or "")
        self.setFrameShape(QFrame.NoFrame)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setReadOnly(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.viewport().setAutoFillBackground(False)
        self.document().setDocumentMargin(0)
        self.document().setDefaultStyleSheet(
            "p, li { line-height: 140%%; } pre, code { font-family: %s; }" % theme.MONO_CSS)
        self.document().setMarkdown(
            self.said, QTextDocument.MarkdownDialectGitHub | QTextDocument.MarkdownNoHTML)
        self._tame_headings()
        self.anchorClicked.connect(self._open)
        self._fit()

    def _tame_headings(self):
        """Headings an agent writes - a plan's sections - a step above the
        text, not a page's title: a feed holds many of them. Markdown gives
        each level a size of its own, which no stylesheet overrides."""
        block = self.document().begin()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            if level:
                cursor = QTextCursor(block)
                cursor.movePosition(QTextCursor.StartOfBlock)
                cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                shape = QTextCharFormat()
                shape.setProperty(QTextFormat.FontSizeAdjustment, 1 if level == 1 else 0)
                shape.setFontWeight(QFont.DemiBold)
                cursor.mergeCharFormat(shape)
            block = block.next()

    def loadResource(self, kind, url):          # noqa: N802 - Qt's own spelling
        return None                             # never a request, never a file read

    def resizeEvent(self, event):               # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def wheelEvent(self, event):                # noqa: N802
        event.ignore()                          # the feed's to scroll

    def _fit(self):
        document = self.document()
        document.setTextWidth(max(1, self.viewport().width()))
        # The text, and whatever the stylesheet puts round it: measured as the
        # text alone, the box was a line short and scrolled its first line
        # out of sight.
        edges = self.contentsMargins()
        wanted = int(document.size().height()) + edges.top() + edges.bottom() + 2
        if wanted != self.height():
            self.setFixedHeight(wanted)
        self.verticalScrollBar().setValue(0)

    @staticmethod
    def _open(url):
        # A page on the web and nothing else: not a file, not a program.
        if QUrl(url).scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)


class _Row(QWidget):
    """One thing in the feed that is not a stage: the same marker column, so
    the whole conversation reads as one sequence down the page."""

    def __init__(self, mark, body):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 3, 6, 3)
        row.setSpacing(0)
        self.marker = stages._Marker(mark, False, False)
        row.addWidget(self.marker, 0)
        row.addWidget(body, 1)
        self.body = body


class ChatFeed(QScrollArea):
    """A conversation, top to bottom, following the newest as it arrives.

    Appended to, never rebuilt while a turn runs - the lesson ``StageList``
    learned. Rows are kept as ``(kind, widget)``; a tool's row is replaced in
    place when the tool comes back, so a call and its outcome are one line.
    """

    #: The person took the queued message at this index out of the queue.
    unqueued = Signal(int)
    interrupt_requested = Signal(int)
    #: A planning card was acted on: answers sent, a plan sent back with
    #: comments - each the message to send as the next planning turn.
    answered = Signal(str)
    revise = Signal(str)
    #: A plan was approved: the access to work with, and the message.
    approve = Signal(str, str)
    #: What a turn changed was asked for: its number.
    changes_wanted = Signal(object)
    #: The person on a change the agent proposed to an integration (ADR-007):
    #: Approve / Deny it, and what they typed into one of its fields.
    change_approved = Signal(str)
    change_denied = Signal(str)
    change_edited = Signal(str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._rows = []                 # (kind, widget) of what is on record
        self._times = []                # when each row's event happened, beside _rows
        self._now = None                # the time of the event being taken
        self._placed = {}               # group -> {key: card} of cards placed by time
        self._waiting = []              # rows of what is not on record yet
        self._tools = {}                # tool call id -> (index in _rows, stage, tool name)
        self._mode = "read"             # the access mode of the turn being shown
        self._began = None              # when the turn being shown began
        self._last_text = ""            # the last thing the agent said in it
        self._cards = []                # planning cards, in order: the newest waits
        self._actionable = False
        self._follow = True
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self._moved)
        bar.rangeChanged.connect(self._grew)
        self._body = QWidget()
        self._column = QVBoxLayout(self._body)
        self._column.setContentsMargins(6, 6, 6, 6)
        self._column.setSpacing(0)
        self._empty = QLabel(NEW)
        self._empty.setProperty("role", "hint")
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setContentsMargins(24, 24, 24, 24)
        self._column.addWidget(self._empty)
        self._column.addStretch(1)
        self.setWidget(self._body)

    # -- what it holds ----------------------------------------------------------
    def clear(self):
        for _kind, widget in self._rows:
            self._drop(widget)
        self._rows, self._tools, self._cards, self._times, self._placed = [], {}, [], [], {}
        self._began, self._last_text = None, ""
        self.set_waiting("", [])
        self._follow = True
        self._sync()

    def load(self, events):
        """A conversation read from its record, all at once."""
        self.clear()
        for event in events or []:
            self._take(event)
        self._sync()

    def append(self, event):
        """One more event of the turn going on now."""
        self._take(event)
        self._sync()

    def set_waiting(self, sending, queued, interrupting=False):
        """What has been typed and is not on record yet: the message being
        handed over, and the ones waiting for the agent to finish."""
        for widget in self._waiting:
            self._drop(widget)
        self._waiting = []
        if sending:
            document = sending if isinstance(sending, dict) else {"message": sending}
            self._add_waiting(MessageCard(document.get("message"), state="sending",
                                          attachments=document.get("attachments"),
                                          who=FROM_QAVECTOR if document.get("trigger") else "YOU"))
        for index, text in enumerate(queued or []):
            document = text if isinstance(text, dict) else {"message": text}
            card = MessageCard(document.get("message"), state="queued", removable=True,
                               attachments=document.get("attachments"))
            card.removed.connect(lambda index=index: self.unqueued.emit(index))
            card.interrupt_requested.connect(lambda index=index: self.interrupt_requested.emit(index))
            card.remove.setEnabled(not interrupting)
            card.interrupt.setEnabled(not interrupting)
            if interrupting and index == 0:
                card.state.setText("STOPPING TO SEND")
            self._add_waiting(card)
        self._sync()

    def set_empty_text(self, text):
        self._empty.setText(text)

    # -- for a test to read -----------------------------------------------------
    def kinds(self):
        return [kind for kind, _widget in self._rows]

    def said(self):
        """What each row says, in order: a message, an answer, a stage's title."""
        found = []
        for kind, widget in self._rows:
            if kind in ("you", "answer", "plan", "questions", "changes"):
                found.append(widget.body.said)
            elif kind in ("change", "request", "inbox"):
                found.append(widget.body.kind.text())
            else:
                found.append(widget.stage.get("title", ""))
        return found

    def waiting(self):
        return [(row.body.state.text(), row.body.said) for row in self._waiting]

    def at_bottom(self):
        return self._follow

    def cards(self):
        return list(self._cards)

    def set_actionable(self, actionable):
        """Whether the newest planning card waits for the person - the
        conversation waits for its answer and nothing else is going on."""
        self._actionable = bool(actionable)
        for card in self._cards:
            card.set_actionable(False)
        if self._cards and self._actionable and not self._cards[-1].property("settled") == "true":
            self._cards[-1].set_actionable(True)

    # -- events into rows -------------------------------------------------------
    def _take(self, event):
        kind = event.get("type")
        at = event.get("at")
        self._now = at if type(at) in (int, float) else time.time()
        if kind == "user":
            self._began, self._last_text = event.get("at"), ""
            self._mode = event.get("mode") or self._mode
            for card in self._cards:
                if card.property("settled") != "true":
                    card.settle()
            self._add("you", _Row({"kind": "result"},
                                  MessageCard(event.get("text"), clock_text(event.get("at")),
                                              attachments=event.get("attachments"),
                                              who=FROM_QAVECTOR if event.get("trigger") else "YOU")))
        elif kind == "thinking":
            self._add("thinking", stages.StageRow(
                {"kind": "thinking", "title": "Thinking", "detail": event.get("text") or ""}))
        elif kind == "text":
            self._answer(event.get("text"))
        elif kind == "tool.started":
            stage = {"kind": "tool", "title": event.get("title") or event.get("name") or "Tool",
                     "detail": event.get("detail") or "", "status": "running"}
            self._tools[event.get("id")] = (len(self._rows), stage, event.get("name") or "")
            self._add("tool", stages.StageRow(stage))
        elif kind == "tool.completed":
            self._returned(event)
        elif kind in ("completed", "failed", "stopped", "interrupted"):
            self._ended(event)
        elif kind == "questions":
            card = QuestionsCard(event.get("items"))
            card.answered.connect(self.answered)
            self._card("questions", card)
        elif kind == "plan":
            version = 1 + sum(1 for one in self._cards if isinstance(one, PlanCard))
            for one in self._cards:
                if isinstance(one, PlanCard) and not one.body.isHidden():
                    one.supersede()
            card = PlanCard(event.get("text"), version)
            card.revise.connect(self.revise)
            card.approve.connect(self.approve)
            self._card("plan", card)
        elif kind == "changes":
            self._changed(event)

    def _card(self, kind, card):
        self._cards.append(card)
        self._add(kind, _Row({"kind": "text"}, card))

    def _changed(self, event):
        if event.get("error"):
            self._add("changes-error", stages.StageRow(
                {"kind": "caution", "title": "What changed was not kept",
                 "detail": str(event["error"])}))
            return
        if not event.get("files"):
            return                      # nothing changed: nothing to say
        line = ChangesLine(event)
        turn = event.get("turn")
        line.wanted.connect(lambda turn=turn: self.changes_wanted.emit(turn))
        self._add("changes", _Row({"kind": "tool_result"}, line))

    def _answer(self, text):
        text = str(text or "").strip()
        if not text:
            return
        self._last_text = text
        self._add("answer", _Row({"kind": "text"}, AnswerText(text)))

    def _returned(self, event):
        found = self._tools.pop(event.get("id"), None)
        if found is None:
            return
        index, stage, name = found
        if event.get("refused"):
            # Not a failure of the tool: the conversation did not allow it.
            # Said in the access mode's terms rather than the program's, whose
            # "requires approval" asks for something nobody here can give.
            why = refused_text(self._mode, name, event.get("reason"))
            self._replace(index, stages.StageRow(dict(
                stage, status="done", kind="caution",
                detail="\n".join(part for part in (stage["detail"], why) if part))))
            return
        stage = dict(stage, status="done" if event.get("ok", True) else "failed")
        if not event.get("ok", True) and event.get("detail"):
            # What came back is worth a line only when it is why it failed.
            stage["detail"] = "\n".join(part for part in (stage["detail"], event["detail"])
                                        if part)
        self._replace(index, stages.StageRow(stage))

    def _ended(self, event):
        # A call the agent never heard back from is not still running.
        for index, stage, _name in list(self._tools.values()):
            detail = "\n".join(part for part in (stage.get("detail"),
                                                 "No result was received for this tool.") if part)
            self._replace(index, stages.StageRow(dict(stage, status="unknown", kind="caution",
                                                      detail=detail)))
        self._tools = {}
        kind = event.get("type")
        if kind == "completed":
            # The CLI's last line repeats what the agent said last. Shown only
            # when it says something the feed does not already end with.
            text = str(event.get("text") or "").strip()
            if text and text != self._last_text:
                self._answer(text)
            stage = {"kind": "result", "title": "Finished",
                     "detail": end_text(event, self._began)}
        elif kind == "failed":
            stage = {"kind": "result", "title": "Failed", "status": "failed",
                     "detail": str(event.get("error") or "")}
        elif kind == "stopped":
            stage = {"kind": "caution", "title": "Stopped",
                     "detail": "The agent was stopped. What it did until then stands."}
        else:
            stage = {"kind": "caution", "title": "Interrupted",
                     "detail": event.get("error") or "The turn ended without a recorded result."}
        self._add("end", stages.StageRow(stage))

    # -- cards placed by time: what the agent asked of others (ADR-006/007) -------
    def sync_cards(self, group, entries):
        """Cards that are not events of the record - a change proposed to Jira,
        a task handed to another agent, a message one wrote here - each where
        it happened: after the last row from before it. ``entries`` are
        ``(key, at, make, update)``: a card shown already is updated, never
        rebuilt (what is typed into it stays); one no longer given goes."""
        placed = self._placed.setdefault(group, {})
        given = set()
        for key, at, make, update in sorted(entries or [], key=lambda one: one[1] or 0):
            given.add(key)
            card = placed.get(key)
            if card is not None:
                update(card)
                continue
            card = make()
            placed[key] = card
            index = next((i for i, when in enumerate(self._times) if when > (at or 0)), len(self._rows))
            self._insert(index, group, _Row({"kind": "text"}, card), at or 0)
        for key in [one for one in placed if one not in given]:
            self._remove(placed.pop(key))
        self._sync()

    def set_changes(self, changes):
        """The conversation's proposed changes to integrations (ADR-007)."""
        def make(change):
            card = IntegrationChangeCard(change, dismissible=False)
            card.approve.connect(self.change_approved)
            card.deny.connect(self.change_denied)
            card.edited.connect(self.change_edited)
            return card

        def update(change):
            def told(card):
                if card.change.get("state") != change.get("state") or \
                        card.result.text() != (change.get("detail") or ""):
                    card.set_state(change.get("state") or "waiting", change.get("detail") or "")
            return told
        self.sync_cards("change", [(one.get("execution_id"), one.get("at") or 0,
                                    lambda one=one: make(one), update(one)) for one in changes or []])

    def change_cards(self):
        return list(self._placed.get("change", {}).values())

    def placed(self, group):
        return list(self._placed.get(group, {}).values())

    # -- rows in the column -----------------------------------------------------
    def _add(self, kind, widget):
        self._column.insertWidget(1 + len(self._rows), widget)
        self._rows.append((kind, widget))
        self._times.append(self._now if self._now is not None else time.time())

    def _insert(self, index, kind, widget, at):
        """A row put between others: the tools still running below it move down one."""
        self._column.insertWidget(1 + index, widget)
        self._rows.insert(index, (kind, widget))
        self._times.insert(index, at)
        self._tools = {key: ((place + 1 if place >= index else place), stage, name)
                       for key, (place, stage, name) in self._tools.items()}

    def _remove(self, card):
        index = next((i for i, (_kind, row) in enumerate(self._rows) if getattr(row, "body", None) is card), None)
        if index is None:
            return
        _kind, row = self._rows.pop(index)
        self._times.pop(index)
        self._tools = {key: ((place - 1 if place > index else place), stage, name)
                       for key, (place, stage, name) in self._tools.items()}
        for name in ("approve", "deny", "open", "dismiss", "edited", "go"):
            signal = getattr(card, name, None)
            if signal is not None:
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
        self._drop(row)

    def _add_waiting(self, card):
        row = _Row({"status": "running"}, card)
        self._column.insertWidget(1 + len(self._rows) + len(self._waiting), row)
        self._waiting.append(row)

    def _replace(self, index, widget):
        kind, old = self._rows[index]
        self._column.replaceWidget(old, widget)
        self._rows[index] = (kind, widget)
        self._drop(old)

    def _drop(self, widget):
        # Hidden first, and for good: a row put in the layout is shown when
        # the event loop next gets to it, and one taken out before that - a
        # tool's row replaced by its result while a conversation is laid out -
        # would be shown without a parent, a window of its own, until deleted.
        widget.hide()
        self._column.removeWidget(widget)
        widget.setParent(None)
        widget.deleteLater()

    def _sync(self):
        """The line joins every row to the next and stops at both ends."""
        everything = [widget for _kind, widget in self._rows] + self._waiting
        for index, widget in enumerate(everything):
            widget.marker.set_ends(first=index == 0, last=index == len(everything) - 1)
        self._empty.setVisible(not everything)
        if self._follow:
            bar = self.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _moved(self, value):
        self._follow = value >= self.verticalScrollBar().maximum() - stages.SLACK

    def _grew(self, _lowest, highest):
        if self._follow:
            self.verticalScrollBar().setValue(highest)


# -- the composer ------------------------------------------------------------------
#: How a conversation another one started is named in the list (ADR-006).
CHILD_WORDS = {"Delegated": "Task for %s", "Message": "Question for %s"}


def is_child(session):
    source = session.get("source") or {}
    return bool(source.get("parent")) and source.get("label") in CHILD_WORDS


def child_title(session):
    """A delegated or asked conversation says so, and for whom, before its title."""
    title = session.get("title") or "Untitled"
    if not is_child(session):
        return title
    source = session["source"]
    who = session.get("connection_name") or session.get("connection") or "an agent"
    return "%s: %s" % (CHILD_WORDS[source["label"]] % who, title)


#: How far a child's row stands in from its parent's, in pixels.
CHILD_INDENT = 12


def _indent_icon():
    """Nothing to see, only room: what pushes a child's row in from its parent's."""
    pixmap = QPixmap(CHILD_INDENT, 1)
    pixmap.fill(Qt.transparent)
    return QIcon(pixmap)


def ordered_with_children(sessions):
    """The list as it comes, each conversation's children right below it,
    newest first; a child whose parent is not listed stays where it is."""
    listed = {one.get("id") for one in sessions}
    children = {}
    for one in sessions:
        if is_child(one) and one["source"]["parent"] in listed:
            children.setdefault(one["source"]["parent"], []).append(one)
    ordered = []
    for one in sessions:
        if is_child(one) and one["source"]["parent"] in listed:
            continue
        ordered.append(one)
        ordered.extend(children.get(one.get("id"), []))
    return ordered


def ran_with_text(session):
    """The model and effort the last turn ran with, as the program said them;
    what was asked for when it said nothing; "" before any turn."""
    turns = session.get("turns") or []
    if not turns:
        return ""
    last = turns[-1]
    model = last.get("model_used") or last.get("model") or ""
    effort = last.get("effort_used") or last.get("effort") or ""
    return " · ".join(part for part in (model, "effort " + effort if effort else "") if part)


def record_text(session):
    """What stands in for the composer in a conversation that is a record."""
    source = session.get("source") or {}
    who = session.get("connection_name") or session.get("connection") or "an agent"
    if source.get("label") == "Delegated" and source.get("by"):
        return "A task %s delegated to %s. It is shown here, not carried on." % (source["by"], who)
    if source.get("label") == "Message" and source.get("by"):
        return "%s answering a question from %s. It is shown here, not carried on." % (who, source["by"])
    return "This conversation was held by %s. It is shown here, not carried on." % source.get("label")


def _caption(text):
    label = QLabel(text)
    label.setProperty("role", "composerlabel")
    label.setFixedWidth(44)
    return label


class MessageEdit(QPlainTextEdit):
    """Local files dropped or pasted into the message become attachments."""

    files_dropped = Signal(list)

    composing = False

    def inputMethodEvent(self, event):
        self.composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def canInsertFromMimeData(self, source):
        if source.hasUrls() and all(url.isLocalFile() for url in source.urls()):
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        if source.hasUrls() and all(url.isLocalFile() for url in source.urls()):
            self.files_dropped.emit([url.toLocalFile() for url in source.urls()])
            return
        super().insertFromMimeData(source)


class Composer(QFrame):
    """Whom to ask, where, how much it may do - and the box to ask in."""

    submitted = Signal(str)
    interrupt_requested = Signal()
    content_changed = Signal()
    stop_requested = Signal()
    restrictions_requested = Signal()
    #: Somebody wants to choose which skills the agent is handed.
    skills_requested = Signal()
    #: A choice was made that is worth remembering for the next conversation.
    chosen = Signal()
    #: What the conversation may do with other agents was changed (ADR-006 D3).
    others_changed = Signal(str)
    wake_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "composer")
        self._connections = []
        self._session = None
        self._restrictions = {}
        self._attachments = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(6)

        self.context_summary = widgets.ElidedLabel(mode=Qt.ElideMiddle)
        self.context_summary.setProperty("role", "muted")
        self.settings_button = QPushButton("Settings")
        self.settings_button.setProperty("variant", "ghost")
        icons.button(self.settings_button, "disclosure_closed")
        self.settings_button.setLayoutDirection(Qt.RightToLeft)    # chevron after
        self.settings_button.setCheckable(True)
        self.settings_button.setToolTip("Agent, model, skills, working folder and restrictions")
        self.context_row = widgets.row(self.context_summary, self.settings_button)
        self.context_row.layout().setStretchFactor(self.context_summary, 1)
        # That the agent is at work, between the things it shows in the feed:
        # a turn can think for minutes with nothing new above, and a still
        # page read as a stopped one. A breathing dot and how long the turn
        # has taken, quiet beside what the turn runs with.
        self._working_since = None
        self.working_dot = widgets.PulseDot()
        self.working_dot.setToolTip("The agent is working on this turn")
        self.working = QLabel()
        self.working.setProperty("role", "muted")
        self.working.setToolTip("The agent is working on this turn")
        self._working_clock = QTimer(self)
        self._working_clock.setInterval(1000)
        self._working_clock.timeout.connect(self._tick)
        for index, widget in enumerate((self.working_dot, self.working)):
            self.context_row.layout().insertWidget(index, widget, 0, Qt.AlignVCenter)
            widget.setVisible(False)
        outer.addWidget(self.context_row)
        self.options = QWidget()
        options_layout = QVBoxLayout(self.options)
        options_layout.setContentsMargins(0, 2, 0, 4)
        options_layout.setSpacing(6)
        outer.addWidget(self.options)
        self.settings_button.toggled.connect(self.set_options_open)

        # Both pickers hold names from a person's own data - a connection, a
        # path - and a combo is as wide as its longest item unless told
        # otherwise: a long path here held the whole window wider than a
        # laptop's screen. So each is sized to a few characters, capped, and
        # the folder takes whatever room the row has left.
        self.connection = widgets.capped(QComboBox(), 180)
        self.connection.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.connection.setMinimumContentsLength(10)
        self.connection.setAccessibleName("Agent")
        self.connection.setPlaceholderText("No connection yet")
        self.connection.activated.connect(self._connection_picked)
        self.folder = widgets.capped(QComboBox(), 460)
        self.folder.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.folder.setMinimumContentsLength(14)
        self.folder.setEditable(True)
        self.folder.setInsertPolicy(QComboBox.NoInsert)
        self.folder.setAccessibleName("Folder")
        self.folder.lineEdit().setPlaceholderText("The folder the agent works in")
        self.folder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.folder.editTextChanged.connect(self._folder_changed)
        self.browse = QPushButton("Browse…")
        self.browse.setProperty("variant", "ghost")
        self.browse.clicked.connect(self._browse)
        # What the folder holds the agent to stands beside what the turn lets
        # it do: the two together are what it may do.
        self.restrict = restrictions_mod.FolderButton()
        self.restrict.clicked.connect(self.restrictions_requested)

        self.mode = widgets.Segmented([label for label, _mode in MODES])
        self.mode.setToolTip(MODE_MEANS)
        self.mode.changed.connect(lambda _label: self.chosen.emit())
        self.others = widgets.Segmented([label for label, _value in OTHERS])
        self.others.setToolTip(OTHERS_MEANS)
        self.others.changed.connect(lambda label: self.others_changed.emit(dict(OTHERS)[label]))
        self.wake = widgets.Segmented([label for label, _value in WAKE], "Ask me")
        self.wake.setToolTip(WAKE_MEANS)
        self.wake.changed.connect(lambda label: self.wake_changed.emit(dict(WAKE)[label]))
        self.plan = QCheckBox("Plan first")
        self.plan.setToolTip(PLAN_MEANS)
        self.plan.toggled.connect(lambda _on: self.chosen.emit())
        self.model = ModelPicker()
        self.model.setMinimumWidth(160)
        self.model.setMaximumWidth(240)
        self.model.currentIndexChanged.connect(lambda _index: self._summarize())
        # How hard the model is asked to think: the levels the connection's
        # program has, as the core says them (``kinds``); blank is the
        # connection's own, else the program's.
        self._kinds = []
        self._effort_wanted = ""
        self.effort = widgets.capped(QComboBox(), 150)
        self.effort.setAccessibleName("Effort")
        self.effort.currentIndexChanged.connect(lambda _index: self._summarize())
        self.stop = QPushButton("Stop")
        self.stop.setToolTip("Stop the agent. What it has done until now stands.")
        self.stop.clicked.connect(self.stop_requested)
        self.stop.setVisible(False)
        # The library skills the agent is handed in this conversation.
        self._skills = []
        self.skills_button = QPushButton("Skills")
        self.skills_button.setToolTip("Hand the agent skills from the library")
        self.skills_button.clicked.connect(self.skills_requested)
        self.how = widgets.row(_caption("AGENT"), self.connection, 6,
                               _caption("MODEL"), self.model, 6, _caption("EFFORT"), self.effort,
                               None, self.skills_button)
        # Browse as wide as its word; the room the capped folder leaves goes before
        # Restrictions, the way the row above leaves it before Skills.
        self.browse.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.where = widgets.row(_caption("FOLDER"), self.folder, self.browse, None, self.restrict)
        self.where.layout().setStretchFactor(self.folder, 1)      # up to its cap, then the gap takes the rest
        options_layout.addWidget(self.how)
        options_layout.addWidget(self.where)
        # A short word, as the captions above are: what it means is its tooltip.
        tasks = _caption("TASKS")
        tasks.setToolTip(WAKE_MEANS)
        self.after = widgets.row(tasks, self.wake, None)
        options_layout.addWidget(self.after)

        self.files_area = QScrollArea()
        self.files_area.setWidgetResizable(True)
        self.files_area.setFrameShape(QFrame.NoFrame)
        self.files_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.files_body = QWidget()
        self.files_column = QVBoxLayout(self.files_body)
        self.files_column.setContentsMargins(0, 0, 0, 0)
        self.files_column.setSpacing(2)
        self.files_area.setWidget(self.files_body)
        self.files_area.hide()
        outer.addWidget(self.files_area)

        self.text = MessageEdit()
        self.text.files_dropped.connect(self.add_attachments)
        self.text.setTabChangesFocus(True)
        self.text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.text.setPlaceholderText("Message - Enter sends, Shift+Enter starts a new line")
        self.text.installEventFilter(self)
        self.text.textChanged.connect(self._fit)
        outer.addWidget(self.text)

        self.attach = QPushButton("Attach files…")
        self.attach.setProperty("variant", "ghost")
        self.attach.setToolTip("Choose files, or drop them into the message. Up to 20 files / 20 MB.")
        self.attach.clicked.connect(self._pick_attachments)
        self.send_button = QPushButton("Send")
        self.send_button.setProperty("variant", "primary")
        self.send_button.clicked.connect(self.send)
        self.interrupt = QPushButton("Interrupt & send")
        self.interrupt.setToolTip("Stop the current turn, then send this message in the same chat. Existing changes remain.")
        self.interrupt.clicked.connect(self.interrupt_requested)
        self.interrupt.hide()
        for button in (self.attach, self.stop, self.send_button):
            button.setStyleSheet("min-height: 30px; max-height: 30px; padding-top: 0px; padding-bottom: 0px;")
        for button in (self.stop, self.send_button):
            button.setFixedWidth(80)
        self.footer = widgets.row(self.attach, 6, _caption("ACCESS"), self.mode, 10, self.plan,
                                  10, _caption("OTHERS"), self.others, None, self.interrupt, self.stop, self.send_button)
        outer.addWidget(self.footer)
        self.text.textChanged.connect(self._sync_send)
        self._sync_send()

        self.note = QLabel("")
        self.note.setProperty("role", "composernote")
        self.note.setWordWrap(True)
        self.note.setTextFormat(Qt.PlainText)
        self.note.setVisible(False)
        outer.addWidget(self.note)

        #: What stands in for all of the above when nothing can be written.
        self.closed = QLabel("")
        self.closed.setProperty("role", "hint")
        self.closed.setWordWrap(True)
        self.closed.setVisible(False)
        outer.addWidget(self.closed)
        self._fit()
        self._sync_restrictions()
        self.set_options_open(False)
        # The rows of choices stand on the composer's ground, not on patches
        # of the page's.
        widgets.see_through(self)

    # -- what there is to choose from ---------------------------------------------
    def set_connections(self, connections):
        self._connections = list(connections or [])
        self._fill_connections(self.connection.currentData())

    def _fill_connections(self, wanted):
        session = self._session
        self.connection.blockSignals(True)
        self.connection.clear()
        for one in self._connections:
            self.connection.addItem(one.get("name") or one.get("id"), one.get("id"))
        if session is not None and self.connection.findData(session.get("connection")) < 0:
            # Whom it was with, even though the connection is gone since.
            self.connection.addItem("%s (removed)" % (session.get("connection_name")
                                                      or session.get("connection")),
                                    session.get("connection"))
        index = self.connection.findData(session.get("connection") if session is not None
                                         else wanted)
        self.connection.setCurrentIndex(index if index >= 0
                                        else (0 if self.connection.count() else -1))
        self.connection.blockSignals(False)
        self._say_model()

    def set_folders(self, folders):
        """The folders offered: the ones conversations were held in lately."""
        current = self.folder.currentText()
        self.folder.blockSignals(True)
        self.folder.clear()
        for one in folders or []:
            # From home, as a person writes a path: the end of it is what
            # tells two projects apart, and the box shows a line's beginning.
            self.folder.addItem(folder_text(one))
        self.folder.setEditText(current)
        self.folder.blockSignals(False)

    def set_session(self, session, defaults=None):
        """Show the choices of ``session`` - or, for None, of a conversation
        not begun yet, starting from ``defaults``."""
        self._session = session
        defaults = defaults or {}
        chosen = session if session is not None else defaults
        self._fill_connections(chosen.get("connection"))
        self.folder.blockSignals(True)
        self.folder.setEditText(folder_text(chosen.get("folder") or ""))
        self.folder.blockSignals(False)
        labels = {mode: label for label, mode in MODES}
        self.mode.set_current(labels.get(chosen.get("mode"), MODES[0][0]), notify=False)
        self.plan.blockSignals(True)
        self.plan.setChecked(bool(chosen.get("plan")))
        self.plan.blockSignals(False)
        self._others_wanted = chosen.get("others")
        self._wake_wanted = chosen.get("wake")
        self.model.set_value(chosen.get("model") or "")
        # Another conversation's: what it was set to, not what was on screen.
        self._effort_wanted = chosen.get("effort") or ""
        self.effort.clear()
        self.set_skills(chosen.get("skills") or [])
        self._restrictions = dict(chosen.get("restrictions") or {})
        # A conversation is with one agent in one folder: its CLI keeps it
        # under the folder it began in.
        begun = session is not None
        self.connection.setEnabled(not begun)
        self.folder.setEnabled(not begun)
        self.browse.setEnabled(not begun)
        readonly = begun and not _is_chat(session)
        for widget in (self.context_row, self.text, self.footer):
            widget.setVisible(not readonly)
        self.set_options_open(not readonly and not bool(chosen.get("folder")))
        self.closed.setVisible(readonly)
        if readonly:
            self.closed.setText(record_text(session))
        self.say("")
        self._say_model()
        self._sync_restrictions()
        self._summarize()

    def set_options_open(self, opened):
        self.settings_button.setChecked(bool(opened))
        self.options.setVisible(bool(opened))
        icons.button(self.settings_button,
                     "disclosure_open" if opened else "disclosure_closed")

    def _summarize(self):
        if not hasattr(self, "_skills"):
            return
        parts = [self.connection.currentText() or "Choose an agent",
                 folder_text(self.folder.currentText()) or "Choose a folder"]
        # What the turn will run with - chosen here, or the connection's default.
        chosen = self.connection.currentData()
        connection = next((one for one in getattr(self, "_connections", ()) if one.get("id") == chosen), {})
        if self.model.value() or connection.get("model"):
            parts.append(self.model.currentText())
        effort = (self.effort.currentData() if hasattr(self, "effort") else "") or connection.get("effort")
        if effort:
            parts.append("effort " + effort)
        if self._skills:
            parts.append("%d skill%s" % (len(self._skills), "s" if len(self._skills) != 1 else ""))
        if self._restrictions:
            parts.append("Restricted")
        self.context_summary.setText(" · ".join(parts))
        self.context_summary.setToolTip("\n".join(parts))

    def set_restrictions(self, raw):
        self._restrictions = dict(raw or {})
        self._sync_restrictions()
        self._summarize()

    def restrictions(self):
        return dict(self._restrictions)

    def set_skills(self, names):
        """Which library skills the agent is handed, said on the button."""
        self._skills = [str(one) for one in names or []]
        self.skills_button.setText("Skills: %d" % len(self._skills) if self._skills
                                   else "Skills")
        self.skills_button.setToolTip(
            "The agent is handed: %s" % ", ".join(self._skills) if self._skills
            else "Hand the agent skills from the library")
        self._summarize()

    def skills(self):
        return list(self._skills)

    def settings(self):
        """What the next turn is asked with."""
        folder = self.folder.currentText().strip()
        return {"connection": self.connection.currentData() or "",
                "folder": os.path.abspath(os.path.expanduser(folder)) if folder else "",
                "mode": dict(MODES)[self.mode.current()],
                "model": self.model.value(), "effort": self.effort.currentData() or "",
                "plan": self.plan.isChecked(), "others": dict(OTHERS)[self.others.current()],
                "wake": dict(WAKE)[self.wake.current()]}

    def set_work(self, mode, plan):
        """What the next turn is asked with, as a card in the feed decided it."""
        labels = {value: label for label, value in MODES}
        if mode in labels:
            self.mode.set_current(labels[mode], notify=False)
        self.plan.blockSignals(True)
        self.plan.setChecked(bool(plan))
        self.plan.blockSignals(False)

    def _connection_picked(self, _index):
        # A new conversation starts with what its connection says for other agents.
        self._others_wanted = None
        self._wake_wanted = None
        if self._session is None:
            self.model.set_value("")
            self._effort_wanted = ""
            self.effort.clear()
        self._say_model()
        self.chosen.emit()

    def _say_wake(self, connection):
        """How the conversation goes on when its tasks end: its own once begun,
        else its connection's default (ADR-009 D3)."""
        wanted = (self._session or {}).get("wake") if self._session is not None \
            else getattr(self, "_wake_wanted", None)
        values = [value for _label, value in WAKE]
        if wanted not in values:
            wanted = connection.get("wake_default") if connection.get("wake_default") in values else "ask"
        self.wake.set_current({value: label for label, value in WAKE}[wanted], notify=False)

    def _say_others(self, connection):
        """The conversation's mode for other agents: its own once begun, else its
        connection's default; off when the connection allows nobody."""
        session = self._session
        wanted = getattr(self, "_others_wanted", None)
        if session is not None:
            wanted = session.get("others")
        values = [value for _label, value in OTHERS]
        if wanted not in values:
            wanted = connection.get("others_default") if connection.get("others_default") in values else "deny"
        labels = {value: label for label, value in OTHERS}
        self.others.set_current(labels[wanted], notify=False)
        allowed = bool(connection.get("allowed_agents"))
        for label, _value in OTHERS:
            self.others.set_enabled(label, allowed, "" if allowed else OTHERS_NONE)
        self.others.setToolTip(OTHERS_MEANS if allowed else OTHERS_NONE)

    def _say_model(self):
        chosen = self.connection.currentData()
        connection = next((one for one in self._connections if one.get("id") == chosen), {})
        # The connection's own default reads as itself ("opus"); where it comes
        # from is said by the tooltip, not crowded into the field.
        kind = next((one for one in self._kinds if one.get("kind") == connection.get("kind")), {})
        inherited = (connection.get("model") or kind.get("model_default_label")
                     or "Use %s settings" % (kind.get("name") or "the program"))
        self.model.set_context((chosen, connection.get("kind"), connection.get("binary")), inherited,
                               "The connection's default model." if connection.get("model")
                               else "Leave the model choice to the program's own settings.")
        self._fill_efforts(connection)
        self._say_others(connection)
        self._say_wake(connection)
        if kind:
            modes = kind.get("modes", [value for _, value in MODES])
            for label, value in MODES:
                self.mode.set_enabled(label, value in modes,
                                      "This provider does not support this mode." if value not in modes else "")
            if "plans" in kind:             # older cores did not advertise it
                self.plan.setEnabled(bool(kind["plans"]))
                if not kind["plans"]:
                    self.plan.setChecked(False)
            self.mode.setToolTip(kind.get("mode_description") or MODE_MEANS)
            features = kind.get("capabilities") or {}
            if "skills" in features:
                self.skills_button.setEnabled(bool(features["skills"]))
        self._summarize()

    def set_kinds(self, kinds):
        """The agent programs the core knows - for the effort levels each has."""
        self._kinds = list(kinds or [])
        self._say_model()

    def _fill_efforts(self, connection):
        kind = next((one for one in self._kinds if one.get("kind") == connection.get("kind")), {})
        wanted = self._effort_wanted
        if self.effort.count():
            wanted = self.effort.currentData() if self.effort.currentData() is not None else wanted
        levels = list(kind.get("efforts") or [])
        if wanted and wanted not in levels:
            levels.append(wanted)               # chosen before, kept as it was
        self.effort.blockSignals(True)
        self.effort.clear()
        self.effort.addItem(connection.get("effort") or "Default", "")
        self.effort.setItemData(0, "The connection's default effort." if connection.get("effort")
                                else "Leave it to the program's own settings.", Qt.ToolTipRole)
        for level in levels:
            self.effort.addItem(level, level)
        self.effort.setCurrentIndex(max(0, self.effort.findData(wanted)))
        self.effort.blockSignals(False)
        self.effort.setToolTip("How hard the model is asked to think. Default leaves it to "
                               "the connection, else to the program's own settings.")
        self.effort.setEnabled(bool(levels))

    def _folder_changed(self, _text):
        self._sync_restrictions()
        self._summarize()

    def _browse(self):
        chosen = widgets.pick_path(self, "The folder the agent works in",
                                   self.folder.currentText().strip() or "~", directory=True)
        if chosen:
            self.folder.setEditText(chosen)
            self.chosen.emit()

    def _sync_restrictions(self):
        folder = self.folder.currentText().strip()
        self.restrict.set_state(
            restrictions_mod.resolve([("project", self._restrictions)]),
            enabled=bool(folder), why="Choose the folder first: restrictions hold the "
                                      "agent to it.")
        # Keep the controls aligned; the full current policy is in the tooltip.
        self.restrict.setText("Restrictions")

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange) and hasattr(self, "mode"):
            self.mode.set_current(self.mode.current(), notify=False)
            if hasattr(self, "others"):
                self.others.set_current(self.others.current(), notify=False)

    # -- writing ----------------------------------------------------------------
    def set_running(self, running, since=None):
        """``since``: when the turn began, on ``time.monotonic()``'s clock -
        what "Working" counts from; now, when not said."""
        running = bool(running)
        if running:
            self._working_since = since if since is not None else (
                self._working_since or time.monotonic())
        else:
            self._working_since = None
        for widget in (self.working_dot, self.working):
            widget.setVisible(running)
        self._animate()
        self._tick()
        self.stop.setVisible(bool(running))
        self.interrupt.setVisible(bool(running))
        self.stop.setEnabled(True)
        self.send_button.setText("Queue" if running else "Send")
        self.text.setPlaceholderText(
            "Queue a follow-up, or use Interrupt & send to change the current work"
            if running else "Message - Enter sends, Shift+Enter starts a new line")

    def working_text(self):
        return self.working.text() if self._working_since is not None else ""

    def _tick(self):
        if self._working_since is None:
            self.working.setText("")
            return
        took = took_text(self._working_since, time.monotonic())
        self.working.setText("Working · %s  ·" % took)

    def _animate(self):
        """The dot breathes and the clock runs only while they can be seen:
        an animation nobody sees is a timer waking the process for nothing."""
        on = self._working_since is not None and self.isVisible()
        if on:
            self.working_dot.start()
            if not self._working_clock.isActive():
                self._working_clock.start()
        else:
            self.working_dot.stop()
            self._working_clock.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._tick()
        self._animate()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._animate()

    def attachments(self):
        return list(self._attachments)

    def set_attachments(self, paths):
        self._attachments = list(dict.fromkeys(paths or []))
        while self.files_column.count():
            item = self.files_column.takeAt(0)
            item.widget().deleteLater()
        for path in self._attachments:
            label = widgets.ElidedLabel(mode=Qt.ElideMiddle)
            label.setText(os.path.basename(path))
            label.setToolTip(path)
            remove = QPushButton("×")
            remove.setProperty("variant", "ghost")
            remove.setAccessibleName("Remove " + os.path.basename(path))
            remove.setToolTip("Remove attachment")
            remove.clicked.connect(lambda _checked=False, path=path: self.set_attachments(
                [one for one in self._attachments if one != path]))
            row = widgets.row(label, remove)
            row.layout().setStretchFactor(label, 1)
            self.files_column.addWidget(row)
        self.files_area.setFixedHeight(min(100, max(32, len(self._attachments) * 34)))
        self.files_area.setVisible(bool(self._attachments))
        self._sync_send()

    def add_attachments(self, paths):
        chosen = list(self._attachments)
        total = 0
        try:
            for path in paths:
                path = os.path.abspath(os.path.expanduser(path))
                if path not in chosen:
                    chosen.append(path)
            if len(chosen) > 20:
                raise ValueError("Attach at most 20 files per message.")
            for path in chosen:
                if not os.path.isfile(path):
                    raise ValueError("Attach a regular file: %s" % path)
                total += os.path.getsize(path)
            if total > 20 * 1024 * 1024:
                raise ValueError("Attachments must total at most 20 MB per message.")
        except (OSError, ValueError) as exc:
            self.say(str(exc), bad=True)
            return
        self.set_attachments(chosen)
        self.say("")

    def _pick_attachments(self):
        paths = widgets.pick_paths(self, "Attach files to the message", self.folder.currentText())
        if paths:
            self.add_attachments(paths)

    def _sync_send(self):
        self.send_button.setEnabled(bool(self.draft().strip() or self._attachments))
        self.content_changed.emit()

    def say(self, text, bad=False):
        self.note.setText(text or "")
        self.note.setVisible(bool(text))
        if self.note.property("bad") != ("true" if bad else "false"):
            self.note.setProperty("bad", "true" if bad else "false")
            self.note.style().unpolish(self.note)
            self.note.style().polish(self.note)

    def draft(self):
        return self.text.toPlainText()

    def set_draft(self, text):
        self.text.setPlainText(text or "")
        cursor = self.text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.text.setTextCursor(cursor)

    def _fit(self):
        """One line to begin with, growing to :data:`MAX_LINES` as it fills."""
        layout = self.text.document().documentLayout()
        lines = max(1, min(MAX_LINES, int(round(layout.documentSize().height()))))
        spacing = self.text.fontMetrics().lineSpacing()
        margin = self.text.document().documentMargin()
        edges = self.text.contentsMargins()
        self.text.setFixedHeight(int(lines * spacing + 2 * margin + edges.top()
                                     + edges.bottom() + 2))

    def eventFilter(self, watched, event):      # noqa: N802 - Qt's own spelling
        """Enter sends; Shift+Enter is left to the box, which starts a line."""
        if (watched is self.text and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and not event.modifiers() & ~Qt.KeypadModifier):
            if self.text.composing:
                return False
            self.send()
            return True
        return super().eventFilter(watched, event)

    def send(self):
        text = self.text.toPlainText()
        if not self.text.isEnabled() or (not text.strip() and not self._attachments):
            return False
        self.submitted.emit(text)
        return True


# -- the tab ------------------------------------------------------------------------
class ChatTab(QWidget):
    """The project's conversations, and the one that is open."""

    HEADERS = ("Conversation", "Last")

    #: Nothing can be asked without a connection: take the person to make one.
    connections_wanted = Signal()
    #: The library lacks the skill somebody wants: take them to add one.
    skills_wanted = Signal()
    #: A conversation was begun, or one of its turns ended: its id. For
    #: whatever shows conversations elsewhere and wants to read them again.
    conversation_changed = Signal(str)

    #: What reads a turn as it happens. A test puts a stand-in here.
    turn_factory = AgentTurn
    #: The QCR host (set by the main window): a turn of a connection whose
    #: "QAVector access" grants something is given QAVector through MCP.
    qcr_access = None

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setObjectName("agentChat")
        self.setAcceptDrops(True)
        self.settings = settings
        #: The view this tab is part of: it asks the core through ``host.ask``.
        self.host = None
        self.sessions = []
        self.connections = []
        self.current = None             # the conversation that is open, or None
        self.turns = {}                 # session id -> the turn being taken
        self._turn_began = {}           # session id -> when it began (monotonic)
        self._sending = {}              # session id -> what is being handed over
        self._queued = {}               # session id -> what waits for the agent
        self._fresh = set()             # sessions made for a turn not yet recorded
        self._drafts = {}               # session id ("" for new) -> what was typed
        self._draft_files = {}
        self._changes_cache = {}
        self._changes_pending = {}
        self._changes_final = set()
        self._changes_dialog_session = None
        self._changes_timer = QTimer(self)
        self._changes_timer.setInterval(2000)
        self._changes_timer.timeout.connect(self._poll_changes)
        self._changes_timer.start()
        self._stopping = set()
        self._redirecting = set()
        self._creating = False
        self._view_serial = 0           # discard reads after navigation to New
        self._reading = None           # events arriving while history is read
        self._shown_seq = 0
        self._terminal_seen = set()
        self._new_restrictions = {}
        self._new_skills = []           # chosen for the conversation not begun
        self.library, self.own = [], {}  # the skills library, as last read
        self.skills_panel = None
        self._loaded = False
        self._whole = True              # whether the feed holds the whole record
        #: Tasks and messages this window's agents send other agents (ADR-006),
        #: by execution id; and what other agents sent the conversation open.
        self._requests = {}
        self._inbox = []
        #: ADR-009: per conversation, the turns QAVector took by itself since the
        #: person last spoke, and what waits for the person's word to go on.
        self._woke = {}                 # session id -> {"failed": n, "all": n}
        self._wakes = {}                # session id -> {key: (found, document, reason)}
        self._listed_states = {}        # execution id -> the state the list was read for

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 14, 0, 0)
        self.splitter = widgets.FoldingSplitter(Qt.Horizontal)
        outer.addWidget(self.splitter)

        # -- the conversations
        left = QWidget()
        column = QVBoxLayout(left)
        column.setContentsMargins(0, 0, 8, 0)
        column.setSpacing(8)
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = widgets.Table(0, len(self.HEADERS), reclick_clears=False)
        # Only a child's row has an icon: room that stands it in from its parent's.
        self.table.setIconSize(QSize(CHILD_INDENT, 1))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        self.table.setColumnWidth(1, 96)
        self.table.itemSelectionChanged.connect(self._picked)
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._say_empty)
        self.note = widgets.empty_note(self.table, "No conversations in this project yet.")
        self.skeleton = widgets.Skeleton(self.table, "rows")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)
        self.new_button = QPushButton("+ New")
        self.new_button.setToolTip("Begin another conversation")
        self.new_button.clicked.connect(self.new_conversation)
        column.addWidget(widgets.row(self.new_button, None))
        self.splitter.addWidget(left)

        # -- the one that is open
        right = QWidget()
        column = QVBoxLayout(right)
        column.setContentsMargins(8, 0, 0, 0)
        column.setSpacing(4)
        self.title = widgets.ElidedLabel(mode=Qt.ElideRight)
        self.title.setProperty("role", "chattitle")
        # What is done to the conversation that is open sits with its name,
        # not under the list of all of them.
        self.rename_button = QPushButton("Rename…")
        self.rename_button.setProperty("variant", "ghost")
        self.rename_button.clicked.connect(self.rename)
        self.delete_button = QPushButton("Delete…")
        self.delete_button.setProperty("variant", "ghost")
        self.delete_button.clicked.connect(self.delete)
        head = widgets.row(self.title, self.rename_button, self.delete_button)
        head.layout().setStretchFactor(self.title, 1)
        column.addWidget(head)
        self.sub = widgets.ElidedLabel(mode=Qt.ElideMiddle)
        self.sub.setProperty("role", "muted")
        # How full the agent's context is and what the conversation cost - the
        # two things worth knowing before saying more to it.
        self.usage = QLabel("")
        self.usage.setProperty("role", "muted")
        self.changes_button = QPushButton("Changes")
        self.changes_button.setProperty("variant", "ghost")
        self.changes_button.setToolTip("What changed in the folder during this conversation, "
                                       "file by file")
        self.changes_button.clicked.connect(lambda: self.show_changes())
        self.changes_dialog = None
        sub_row = widgets.row(self.sub, self.usage, self.changes_button)
        sub_row.layout().setStretchFactor(self.sub, 1)
        column.addWidget(sub_row)
        self.earlier = QPushButton("Show the whole conversation")
        self.earlier.setProperty("variant", "ghost")
        self.earlier.setToolTip("Only its newest part is shown. Read the rest too.")
        self.earlier.clicked.connect(self._read_whole)
        self.earlier.setVisible(False)
        column.addWidget(self.earlier, 0, Qt.AlignLeft)
        frame = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.feed = ChatFeed()
        self.feed.unqueued.connect(self._unqueue)
        self.feed.interrupt_requested.connect(self.interrupt_queued)
        self.feed.answered.connect(lambda said: self._card_turn(said, plan=True))
        self.feed.revise.connect(lambda said: self._card_turn(said, plan=True))
        self.feed.approve.connect(lambda mode, said: self._card_turn(said, plan=False, mode=mode))
        self.feed.changes_wanted.connect(self.show_changes)
        # A change the agent proposed to Jira is answered where it stands in the conversation.
        self.feed.change_approved.connect(lambda found: ChatTab.qcr_access.answer_integration(found, True))
        self.feed.change_denied.connect(lambda found: ChatTab.qcr_access.answer_integration(found, False))
        self.feed.change_edited.connect(lambda found, field, value:
                                        ChatTab.qcr_access.set_integration_draft(found, field, value))
        self.feed_skeleton = widgets.Skeleton(self.feed, "lines")
        frame.layout().addWidget(self.feed, 1)
        self.composer = Composer()
        self.composer.submitted.connect(self.submit)
        self.composer.interrupt_requested.connect(self.interrupt_submit)
        self.composer.stop_requested.connect(self.stop)
        self.composer.restrictions_requested.connect(self.edit_restrictions)
        self.composer.skills_requested.connect(self.choose_skills)
        self.composer.chosen.connect(self._remember_choices)
        self.composer.others_changed.connect(self._others_chosen)
        self.composer.wake_changed.connect(self._wake_chosen)
        self.composer.model.requested.connect(self._load_models)
        # Other agents (ADR-006): what waits for the person's yes, how the
        # conversation's tasks went, and what other agents wrote to it - cards
        # in the conversation itself, where each happened (see _sync_others).
        frame.layout().addWidget(self.composer)
        # What the composer's place says while there is nobody to ask.
        self.setup = QFrame()
        self.setup.setProperty("role", "composer")
        across = QHBoxLayout(self.setup)
        across.setContentsMargins(12, 10, 12, 10)
        said = QLabel("No agent connection yet. A connection is an agent's program on "
                      "this computer, such as Claude Code.")
        said.setProperty("role", "hint")
        said.setWordWrap(True)
        across.addWidget(said, 1)
        self.setup_button = QPushButton("Add a connection")
        self.setup_button.setProperty("variant", "primary")
        self.setup_button.clicked.connect(self.connections_wanted)
        across.addWidget(self.setup_button)
        frame.layout().addWidget(self.setup)
        column.addWidget(frame, 1)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([300, 760])
        self.splitter.remember_in(settings, "integrations/agents")
        self._show_new()
        self._sync()

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if (urls and all(url.isLocalFile() for url in urls)
                and not self._creating and _is_chat(self.current) and self.connections):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        self.dragEnterEvent(event)
        if event.isAccepted():
            self.composer.add_attachments([url.toLocalFile() for url in event.mimeData().urls()])

    # -- what is remembered ------------------------------------------------------
    def _memory(self):
        return dict(self.settings.integrations_state().get("agents") or {})

    def _remember(self, **changes):
        state = dict(self.settings.integrations_state())
        state["agents"] = dict(state.get("agents") or {}, **changes)
        self.settings.save_integrations_state(state)

    def _remember_choices(self):
        if self.current is None:
            chosen = self.composer.settings()
            self._remember(connection=chosen["connection"], mode=chosen["mode"],
                           plan=chosen["plan"])

    def _defaults(self):
        """What a conversation not begun yet starts from: the last choices."""
        memory = self._memory()
        folders = [one for one in memory.get("folders") or [] if isinstance(one, str)]
        return {"connection": memory.get("connection") or "", "mode": memory.get("mode") or "read",
                "plan": bool(memory.get("plan")),
                "folder": folders[0] if folders else "", "model": "",
                "restrictions": self._new_restrictions, "skills": self._new_skills}

    # -- the outside ------------------------------------------------------------
    def ask(self, kind, work, done):
        if self.host is not None:
            self.host.ask(kind, work, done)

    def core(self):
        return self.host.core if self.host is not None else None

    def _load_models(self, done):
        connection_id = self.composer.connection.currentData()
        if not connection_id or self.core() is None:
            done({"ok": False, "error": "Choose an agent connection first."})
            return
        self.ask("chat-models", lambda core, cancel: core.agents(
            "models", connection_id, timeout=30, cancel=cancel), done)

    def set_connections(self, connections):
        self.connections = list(connections or [])
        self.composer.set_connections(self.connections)
        if self.current is None:
            self.composer.set_session(None, dict(self._defaults(),
                                                 **self._typed_choices()))
        self._sync()

    def _typed_choices(self):
        """Choices already made for the conversation not begun, kept across a
        refresh of what there is to choose from."""
        chosen = self.composer.settings()
        folder = self.composer.folder.currentText().strip()
        found = {"mode": chosen["mode"], "model": chosen["model"]}
        if folder:
            found["folder"] = folder
        if chosen["connection"]:
            found["connection"] = chosen["connection"]
        return found

    def reset(self):
        """Another project: nothing shown here belongs to it."""
        self.shutdown()
        self.sessions, self.current = [], None
        self._sending, self._queued, self._drafts = {}, {}, {}
        self._draft_files = {}
        self._changes_cache = {}
        self._changes_pending = {}
        self._changes_final = set()
        self._stopping = set()
        self._creating = False
        self._fresh = set()
        self._new_restrictions = {}
        self._new_skills = []
        if self.skills_panel is not None:
            self.skills_panel.hide()
        self._loaded = False
        self.skeleton.stop()
        self.feed_skeleton.stop()
        self._fill()
        self._show_new()

    def shown(self):
        """The tab is in front: read the conversations, once - and the agent
        programs' effort levels, a look along PATH that starts nothing."""
        if not self._loaded:
            self.load_sessions()
        if not self.composer._kinds:
            self.ask("chat-kinds", lambda core, cancel: core.agents("kinds", cancel=cancel),
                     lambda payload: payload.get("ok") and self.composer.set_kinds(
                         payload.get("kinds") or []))

    def running(self):
        return bool(self.turns)

    def has_draft(self):
        """Whether something typed would be lost by leaving."""
        self._drafts[self._key()] = self.composer.draft()
        self._draft_files[self._key()] = self.composer.attachments()
        return (any(text.strip() for text in self._drafts.values())
                or any(self._draft_files.values()) or any(self._queued.values()))

    def shutdown(self):
        self._changes_pending = {}
        if self.changes_dialog is not None:
            self.changes_dialog.close()
        self._redirecting.clear()
        self._stopping.update(self.turns)
        for turn in list(self.turns.values()):
            turn.shutdown()
        self.turns = {}

    def _key(self, session=None):
        session = self.current if session is None else session
        return session["id"] if session else ""

    def _showing(self, session_id):
        return self.current is not None and self.current.get("id") == session_id

    # -- the list ---------------------------------------------------------------
    def load_sessions(self, then=None):
        if self.core() is None:
            return
        serial = self._view_serial
        self.skeleton.start()

        def done(payload):
            self.skeleton.stop()
            first = not self._loaded
            self._loaded = True
            if not payload.get("ok"):
                self.composer.say(payload.get("error") or "The conversations could not "
                                  "be read.", bad=True)
                return
            self.sessions = list(payload.get("sessions") or [])
            self._fill()
            if first and self.current is None and serial == self._view_serial:
                wanted = self._memory().get("session") or ""
                if any(one.get("id") == wanted for one in self.sessions):
                    self.open_session(wanted)
            if then is not None:
                then()

        self.ask("sessions", lambda core, cancel: core.agents(
            "sessions", document={"limit": SESSIONS_SHOWN}, cancel=cancel), done)

    def _fill(self):
        chosen = self._key()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for one in ordered_with_children(self.sessions):
            row = self.table.rowCount()
            self.table.insertRow(row)
            source = one.get("source") or {}
            title = QTableWidgetItem(child_title(one))
            if is_child(one):
                title.setIcon(_indent_icon())
            title.setData(Qt.UserRole, one.get("id"))
            title.setToolTip("\n".join(part for part in (
                one.get("title"), "%s · %s" % (one.get("connection_name") or one.get("connection"),
                                               folder_text(one.get("folder"))),
                " - ".join(part for part in (source.get("label"), source.get("detail"))
                           if part)) if part))
            answering = one.get("id") in self.turns
            last = QTableWidgetItem("Answering" if answering else ago_text(one.get("updated")))
            for item in (title, last):
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 0, title)
            self.table.setItem(row, 1, last)
            if one.get("id") == chosen:
                self.table.selectRow(row)
        self.table.blockSignals(False)
        self.search.apply()
        self._sync()

    def _put(self, session):
        """``session`` as the core now has it: first in the list, as the most
        recently used is."""
        self.sessions = [session] + [one for one in self.sessions
                                     if one.get("id") != session.get("id")]
        if self._showing(session.get("id")):
            self.current = session
            self._head()                # a first turn gives it its name
            self._sync_cards()
        self._fill()

    def _picked(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        wanted = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
        if not self._showing(wanted):
            self.open_session(wanted)

    def _say_empty(self):
        self.note.say_filtered("No conversation matches the search."
                               if self.search.searching() else "")

    def _sync(self):
        chosen = self.current is not None
        self.rename_button.setEnabled(chosen)
        self.delete_button.setEnabled(chosen and self._key() not in self.turns)
        for button in (self.rename_button, self.delete_button):
            button.setVisible(chosen)       # nothing to rename before it exists
        nobody = not self.connections and (self.current is None)
        self.setup.setVisible(nobody)
        self.composer.setVisible(not nobody)
        self.composer.set_running(self._key() in self.turns,
                                  self._turn_began.get(self._key()))
        self.composer.stop.setEnabled(self._key() not in self._stopping or
                                      self._key() in self._redirecting)
        self.composer.interrupt.setEnabled(self._key() not in self._stopping)
        self.table.setEnabled(not self._creating)
        self.new_button.setEnabled(not self._creating)
        self.composer.text.setEnabled(not self._creating)
        self.composer.footer.setEnabled(not self._creating)

    # -- the conversation --------------------------------------------------------
    def _leave(self):
        """Keep what was typed for the conversation being left."""
        self._drafts[self._key()] = self.composer.draft()
        self._draft_files[self._key()] = self.composer.attachments()

    def _show_new(self):
        self._view_serial += 1
        self._reading = None
        self._shown_seq = 0
        self.feed_skeleton.stop()
        self.current = None
        self._whole = True
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.blockSignals(False)
        self.title.setText("New conversation")
        self.sub.setText("Nothing has been said yet")
        # Nothing used, nothing changed: not the last conversation's figures.
        self.usage.setText("")
        self.usage.setToolTip("")
        self.changes_button.setVisible(False)
        self.earlier.setVisible(False)
        self.feed.set_empty_text(NEW)
        self.feed.clear()
        self.composer.set_folders(self._memory().get("folders") or [])
        self.composer.set_session(None, self._defaults())
        self.composer.set_draft(self._drafts.get("", ""))
        self.composer.set_attachments(self._draft_files.get("", []))
        self._sync()

    def new_conversation(self):
        if self._creating:
            return
        self._leave()
        self._remember(session="")
        self._show_new()
        self.composer.text.setFocus()

    def open_session(self, session_id, limit=EVENTS_SHOWN):
        """Read one conversation from its record and show it."""
        if not session_id or self.core() is None or self._creating:
            return
        self._view_serial += 1
        serial = self._view_serial
        arriving = []
        self._reading = (session_id, arriving)
        self.feed_skeleton.start()

        def done(payload):
            if serial != self._view_serial:
                return
            self.feed_skeleton.stop()
            self._reading = None
            if not payload.get("ok"):
                self.composer.say(payload.get("error") or "The conversation could not "
                                  "be read.", bad=True)
                if payload.get("kind") == "not_found":
                    self.sessions = [one for one in self.sessions
                                     if one.get("id") != session_id]
                    self._fill()
                return
            self._leave()
            events = list(payload.get("events") or [])
            last_seq = max((event.get("seq", 0) for event in events), default=0)
            events.extend(event for event in arriving if event.get("seq", 0) > last_seq)
            self._show(payload.get("session") or {}, events, whole=not limit)

        self.ask("session", lambda core, cancel: core.agents(
            "session", session_id, document={"limit": limit}, cancel=cancel), done)

    def _read_whole(self):
        if self.current is not None:
            self.open_session(self.current["id"], limit=0)

    def _show(self, session, events, whole=False):
        self._view_serial += 1
        self._reading = None
        self._shown_seq = max((event.get("seq", 0) for event in events), default=0)
        self.current = session
        self._whole = whole or not events or int(events[0].get("seq") or 1) <= 1
        self._remember(session=session.get("id") or "")
        self._head()
        self.earlier.setVisible(not self._whole)
        self.feed.set_empty_text("Nothing has been said in this conversation yet.")
        self.feed.load(events)
        self.integration_changes_changed()
        self._sync_wakes()
        if (session.get("state") == "interrupted" and events
                and events[-1].get("type") not in ("completed", "failed", "stopped")):
            self.feed.append({"type": "interrupted"})
        self._waiting()
        self.composer.set_folders(self._memory().get("folders") or [])
        self.composer.set_session(session)
        self.composer.set_draft(self._drafts.get(session.get("id") or "", ""))
        self.composer.set_attachments(self._draft_files.get(session.get("id") or "", []))
        self._sync_cards()
        self._inbox = []
        self._sync_others()
        self._refresh_inbox()
        self._fill()
        self._refresh_changes()

    def _sync_cards(self):
        """The newest planning card waits for the person only while the
        conversation does: it is carried on here, waits for answers or a word
        on the plan, and the agent is not at work."""
        session = self.current
        self.feed.set_actionable(
            session is not None and _is_chat(session) and self._key() not in self.turns
            and session.get("phase") in ("questions", "plan"))

    def _card_turn(self, text, plan, mode=None):
        """A card in the feed was acted on: the next turn, as it said."""
        if (self.current is None or not _is_chat(self.current) or self.core() is None
                or self._key() in self.turns or self._creating):
            return
        self.composer.set_work(mode, plan)
        document = self._message(text, [])
        document["plan"] = bool(plan)
        if mode:
            document["mode"] = mode
        self.feed.set_actionable(False)
        self._start(self.current, text, document=document)

    def show_changes(self, turn=None):
        """What changed in the folder: the whole conversation's, or a turn's,
        in a window beside the chat."""
        session = self.current
        if session is None:
            return
        session_id = session["id"]
        turns = [one.get("n") for one in session.get("turns") or []
                 if (one.get("changes") or {}).get("files") or one.get("state") == "running"]

        def read(number, done):
            self._read_changes(session_id, number, done)

        if self.changes_dialog is None:
            self.changes_dialog = ChangesDialog(parent=self)
        self._changes_dialog_session = session_id
        self.changes_dialog.show_for(session.get("title") or "Untitled", turns, read, turn)

    def _read_changes(self, session_id, number, done):
        """Coalesce a dialog and button reading the same live snapshot."""
        key = (session_id, number)
        if key in self._changes_pending:
            self._changes_pending[key].append(done)
            return
        pending = self._changes_pending
        pending[key] = [done]

        def ready(payload):
            if pending is not self._changes_pending:
                return
            if number is None and session_id in self.turns and payload.get("ok"):
                # The first request may arrive before the core has persisted
                # the new turn. Keep the window following it in that gap.
                payload = dict(payload, live=True)
            if number is None:
                # The button needs totals, not a potentially multi-megabyte
                # patch for every conversation visited in this window.
                self._changes_cache[session_id] = {name: payload[name] for name in (
                    "ok", "added", "removed", "live", "pending", "error", "unkept") if name in payload}
                if self._showing(session_id):
                    self._changes_label()
            for callback in pending.pop(key, []):
                callback(payload)

        self.ask("changes:%s:%s" % key, lambda core, cancel: core.agents(
            "changes", session_id, document={"turn": number} if number else {},
            cancel=cancel), ready)

    def _poll_changes(self):
        session = self.current or {}
        # An open whole-session diff already polls; its answer also updates
        # the button, so do not snapshot the folder a second time.
        if (self.changes_dialog is not None and self.changes_dialog.isVisible()
                and self._changes_dialog_session == self._key()
                and self.changes_dialog.scope.currentData() is None):
            return
        found = self._changes_cache.get(self._key()) or {}
        remote = (session.get("running") or session.get("state") == "running") and (
            not found.get("ok") or found.get("live"))
        if self.isVisible() and (self._key() in self.turns or remote):
            self._refresh_changes()

    def _refresh_changes(self, final=False):
        if self.current is None or self.core() is None:
            return
        session_id = self.current["id"]
        if not (self.current.get("turns") or self.current.get("running") or session_id in self.turns):
            return
        if (session_id, None) in self._changes_pending:
            if final:
                self._changes_final.add(session_id)
            return

        def done(payload):
            if self._showing(session_id):
                if session_id in self._changes_final:
                    self._changes_final.discard(session_id)
                    self._refresh_changes()

        self._read_changes(session_id, None, done)

    def _changes_label(self):
        session = self.current or {}
        found = self._changes_cache.get(session.get("id")) or {}
        visible = bool(session.get("turns") or session.get("running") or self._key() in self.turns)
        self.changes_button.setVisible(visible)
        available = found.get("ok") and not found.get("pending")
        self.changes_button.setText("Changes +%d -%d" % (
            found.get("added") or 0, found.get("removed") or 0) if available else "Changes")
        self.changes_button.setToolTip(found.get("error") or (
            "Current changes in the conversation's folder. Includes edits made by others."
            + (" Some files could not be compared." if found.get("unkept") else "")))

    def _head(self):
        session = self.current or {}
        self.title.setText(session.get("title") or "Untitled")
        source = (session.get("source") or {}).get("label") or CHAT
        self.sub.setText(" · ".join(part for part in (
            session.get("connection_name") or session.get("connection"),
            folder_text(session.get("folder")), "" if source == CHAT else source) if part))
        context = context_text(session.get("context"))
        # The program's own estimate, priced as its API would be (ADR-008 D3).
        cost = cost_text(session.get("cost_usd")) if session.get("turns") else ""
        cost = "est. " + cost if cost else ""
        self.usage.setText(" · ".join(part for part in (
            ran_with_text(session), "context " + context if context else "", cost) if part))
        tokens = tokens_text(session.get("tokens"))
        self.usage.setToolTip("Tokens so far: %s" % tokens if tokens else "")
        self._changes_label()

    def _waiting(self):
        key = self._key()
        self.feed.set_waiting(self._sending.get(key, ""), self._queued.get(key, []),
                              interrupting=key in self._redirecting)

    # -- saying something --------------------------------------------------------
    def submit(self, text):
        """What Enter does: begin a conversation, take a turn, or wait for one."""
        if self._creating or self.core() is None or not _is_chat(self.current):
            return
        self.composer.say("")
        files = self.composer.attachments()
        if not text.strip() and not files:
            return
        if self.current is None:
            self._begin(text, files)
        elif self._key() in self.turns:
            document = self._message(text, files)
            self._queued.setdefault(self._key(), []).append(document)
            self.composer.set_draft("")
            self.composer.set_attachments([])
            self._waiting()
        else:
            document = self._message(text, files)
            self.composer.set_draft("")
            self.composer.set_attachments([])
            self._start(self.current, text, document=document)

    def _message(self, text, files):
        chosen = self.composer.settings()
        document = {"message": text, "mode": chosen["mode"], "model": chosen["model"],
                    "effort": chosen["effort"], "plan": chosen["plan"]}
        if files:
            document["attachments"] = list(files)
        return document

    def _begin(self, text, files=None):
        chosen = self.composer.settings()
        if not chosen["connection"]:
            self.composer.say("Choose the agent to ask.", bad=True)
            return
        if not chosen["folder"]:
            # Never a guess: an agent let loose in the wrong folder is the
            # mistake this box exists to prevent.
            self.composer.say("Choose the folder the agent works in.", bad=True)
            self.composer.set_options_open(True)
            self.composer.folder.setFocus()
            return
        document = dict(chosen, restrictions=self.composer.restrictions(),
                        skills=self.composer.skills())
        message = self._message(text, files)
        self._creating = True
        self._sync()

        def done(payload):
            self._creating = False
            self._sync()
            if not payload.get("ok"):
                self.composer.say(payload.get("error") or "The conversation could not "
                                  "be started.", bad=True)
                self.composer.text.setFocus()
                return
            session = payload.get("session") or {}
            folders = [session.get("folder")] + [one for one in self._memory().get("folders")
                                                 or [] if one != session.get("folder")]
            self._remember(folders=folders[:RECENT_FOLDERS], connection=session.get("connection"),
                           mode=session.get("mode"))
            self._drafts.pop("", None)
            self._draft_files.pop("", None)
            self._new_restrictions = {}
            self._new_skills = []
            self._fresh.add(session.get("id"))
            self.sessions.insert(0, session)
            self._show(session, [], whole=True)
            self.composer.set_draft("")
            self.composer.set_attachments([])
            self.composer.text.setFocus()
            self._start(session, text, document=message)

        self.ask("session-new", lambda core, cancel: core.agents(
            "session-new", document=document), done)

    def delegate(self, chosen, source, message, done=None, *, send=True):
        """Open a conversation another part of QAVector hands a task to, and
        say its first message, or leave it as an editable draft when
        ``send=False``. A draft starts no agent turn.

        ``chosen`` is what the composer would say: ``connection``, ``folder``,
        ``mode``, ``plan``, ``model``, ``effort`` and ``title``, plus optional
        restrictions, skills, attachments and other-agent policy; ``source``
        who handed it over, with the ``ref`` it finds its conversations by.
        Whatever is typed in the
        composer for a new conversation stays as it was. ``done`` hears the
        conversation, or an answer that is not ``ok``.
        """
        if self._creating or self.core() is None:
            return False
        document = {"connection": chosen.get("connection") or "",
                    "folder": chosen.get("folder") or "", "mode": chosen.get("mode") or "read",
                    "plan": bool(chosen.get("plan")), "model": chosen.get("model") or "",
                    "effort": chosen.get("effort") or "", "title": chosen.get("title") or "",
                    "restrictions": dict(chosen.get("restrictions") or {}),
                    "skills": list(chosen.get("skills") or []),
                    "source": dict(source or {})}
        if "others" in chosen:
            document["others"] = chosen["others"]
        first = {"message": message, "mode": document["mode"], "model": document["model"],
                 "effort": document["effort"],
                 "plan": document["plan"]}
        if chosen.get("attachments"):
            first["attachments"] = list(chosen["attachments"])
        self._creating = True
        self._sync()

        def made(payload):
            self._creating = False
            self._sync()
            if not payload.get("ok"):
                if done is not None:
                    done(payload)
                return
            session = payload.get("session") or {}
            folders = [session.get("folder")] + [one for one in self._memory().get("folders")
                                                 or [] if one != session.get("folder")]
            self._remember(folders=folders[:RECENT_FOLDERS])
            self._fresh.add(session.get("id"))
            self.sessions.insert(0, session)
            self._leave()
            if not send:
                self._drafts[session["id"]] = message
                self._draft_files[session["id"]] = list(chosen.get("attachments") or [])
            self._show(session, [], whole=True)
            if send:
                self._start(session, message, document=first)
            self.composer.text.setFocus()
            self.conversation_changed.emit(session.get("id") or "")
            if done is not None:
                done(payload)

        self.ask("session-new", lambda core, cancel: core.agents(
            "session-new", document=document), made)
        return True

    def _start(self, session, text, document=None):
        session_id = session["id"]
        if not (document or {}).get("trigger"):
            # The person spoke: QAVector may go on by itself again (ADR-009 D6).
            self._woke.pop(session_id, None)
        if document is None:
            chosen = self.composer.settings() if self._showing(session_id) else {}
            document = {"message": text}
            for key in ("mode", "model", "effort", "plan"):
                if key in chosen:
                    document[key] = chosen[key]
        waiting = [one for one in self._inbox if self._showing(session_id)]
        if waiting and ChatTab.qcr_access is not None:
            # What other agents wrote goes with the person's message, said as theirs (ADR-006 D4).
            document = dict(document, message=agentcards.inbox_prefix(waiting) + str(document.get("message") or ""))
            for one in waiting:
                ChatTab.qcr_access.acknowledge_message(one.get("message_id"))
            self._inbox = []
            self._sync_others()
        access = None
        if ChatTab.qcr_access is not None:
            connection = next((one for one in self.connections if one.get("id") == session.get("connection")), None)
            others = (self.composer.settings().get("others") if self._showing(session_id)
                      else None) or session.get("others")
            access = ChatTab.qcr_access.grant_agent(connection, session_id, session=dict(
                session, others=others), mode=document.get("mode") or session.get("mode"))
            if access is not None:
                # Paths only: the token is in its own 0600 file, never in the turn's document.
                document = dict(document, qavector={"socket": access["socket"], "token_file": access["token_file"]})
        turn = self.turn_factory(self.core(), session_id, document, self)
        if access is not None:
            turn.answered.connect(lambda _answer, access=access: ChatTab.qcr_access.end_agent(access))
        turn.event.connect(lambda event, session_id=session_id: self._heard(session_id, event))
        turn.answered.connect(lambda answer, session_id=session_id:
                              self._answered(session_id, answer))
        self.turns[session_id] = turn
        self._turn_began[session_id] = time.monotonic()
        self._terminal_seen.discard(session_id)
        if self._showing(session_id):
            self._sync_cards()
        self._sending[session_id] = dict(document)
        if self._showing(session_id):
            self._waiting()
        self._fill()
        turn.start()
        if self._showing(session_id):
            self._changes_label()
            self._refresh_changes()
        if (self.changes_dialog is not None and self.changes_dialog.isVisible()
                and self._changes_dialog_session == session_id
                and self.changes_dialog.scope.currentData() is None):
            self.changes_dialog.load(refresh=True)

    def _heard(self, session_id, event):
        if self._reading is not None and self._reading[0] == session_id:
            self._reading[1].append(event)
        if event.get("type") in ("completed", "failed", "stopped"):
            self._terminal_seen.add(session_id)
        if event.get("type") == "user":
            self._sending.pop(session_id, None)     # on record now: no longer waiting
            self._fresh.discard(session_id)
        if self._showing(session_id):
            if event.get("type") == "user":
                self._waiting()
            seq = event.get("seq", 0)
            if not seq or seq > self._shown_seq:
                self.feed.append(event)
                self._shown_seq = max(self._shown_seq, seq)

    def _answered(self, session_id, answer):
        self.conversation_changed.emit(session_id)
        turn = self.turns.pop(session_id, None)
        self._turn_began.pop(session_id, None)
        if turn is not None:
            turn.deleteLater()
        unsent = self._sending.pop(session_id, None)
        queued = self._queued.pop(session_id, [])
        stopped = session_id in self._stopping
        redirecting = session_id in self._redirecting
        self._redirecting.discard(session_id)
        self._stopping.discard(session_id)
        fresh = session_id in self._fresh
        self._fresh.discard(session_id)
        showing = self._showing(session_id)
        if isinstance(answer.get("session"), dict):
            self._put(answer["session"])
        state = (answer.get("turn") or {}).get("state")
        reported = session_id in self._terminal_seen
        self._terminal_seen.discard(session_id)
        if showing and not reported and (state or answer.get("record_unknown")):
            self.feed.append({"type": state or "interrupted",
                              "error": answer.get("error", "")})
        if queued and ((state == "completed" and not stopped) or
                       (redirecting and state in ("completed", "stopped"))):
            # It finished, and somebody had more to say: said now, in order.
            session = next((one for one in self.sessions if one.get("id") == session_id), None)
            if session is not None:
                self._queued[session_id] = queued[1:]
                self._start(session, queued[0]["message"], document=queued[0])
                return
        # Anything else, and what was not said is the person's to say again.
        back = [doc for doc in ([unsent] if state is None else []) + queued if doc]
        unborn = fresh and state is None and not answer.get("record_unknown")
        key = "" if unborn else session_id
        if back:
            # In front of whatever has been typed since - on screen when this
            # conversation is the one open, kept aside when it is not.
            typed = self.composer.draft() if showing else self._drafts.get(key, "")
            self._drafts[key] = "\n\n".join(part for part in
                                           [doc["message"] for doc in back] + [typed]
                                           if part.strip())
            typed_files = (self.composer.attachments() if showing
                           else self._draft_files.get(key, []))
            self._draft_files[key] = list(dict.fromkeys(
                [path for doc in back for path in doc.get("attachments", [])] + typed_files))
        if unborn:
            # Made for a first message that was never said: not a conversation.
            self._forget(session_id)
            if showing:
                self._show_new()
        elif showing:
            self._waiting()
            if back:
                self.composer.set_draft(self._drafts[key])
                self.composer.set_attachments(self._draft_files[key])
        if showing and state is None:
            self.composer.say(answer.get("error") or "The message could not be sent.",
                              bad=True)
        self._fill()
        if showing:
            self._refresh_changes(final=True)

    def _forget(self, session_id):
        """Take back a conversation that never began: what was chosen for it
        is what the next one starts from."""
        for one in self.sessions:
            if one.get("id") == session_id:
                self._new_restrictions = dict(one.get("restrictions") or {})
                self._new_skills = list(one.get("skills") or [])
        self.sessions = [one for one in self.sessions if one.get("id") != session_id]
        self.ask("session-delete", lambda core, cancel: core.agents(
            "session-delete", session_id), lambda _payload: None)

    def _unqueue(self, index):
        if self._key() in self._redirecting:
            return
        queued = self._queued.get(self._key(), [])
        if 0 <= index < len(queued):
            del queued[index]
            self._waiting()

    def interrupt_submit(self):
        key = self._key()
        if key not in self.turns or key in self._stopping:
            return
        if not self.composer.draft().strip() and not self.composer.attachments():
            return
        self.submit(self.composer.draft())
        self.interrupt_queued(len(self._queued.get(key, [])) - 1)

    def interrupt_queued(self, index):
        key = self._key()
        queued = self._queued.get(key, [])
        if key not in self.turns or key in self._stopping or not 0 <= index < len(queued):
            return
        queued.insert(0, queued.pop(index))
        self._redirecting.add(key)
        self._stopping.add(key)
        self._waiting()
        self.composer.interrupt.setEnabled(False)
        self.composer.say("Stopping the current turn; your message will be sent next.")
        self.turns[key].stop()

    def stop(self):
        self._redirecting.discard(self._key())
        turn = self.turns.get(self._key())
        if turn is not None:
            self._stopping.add(self._key())
            self.composer.stop.setEnabled(False)
            turn.stop()

    # -- what a conversation is called, and removing one --------------------------
    def rename(self):
        session = self.current
        if session is None:
            return
        title = widgets.ask_text(self, "Rename conversation", "What is it called?",
                                 session.get("title") or "", agree="Rename")
        if title is None or title == (session.get("title") or ""):
            return

        def done(payload):
            if payload.get("ok"):
                self._put(payload.get("session") or session)
                if self._showing(session["id"]):
                    self._head()
            else:
                self.composer.say(payload.get("error") or "Not renamed.", bad=True)

        self.ask("session-set", lambda core, cancel: core.agents(
            "session-set", session["id"], document={"title": title}), done)

    def delete(self):
        session = self.current
        if session is None or session["id"] in self.turns:
            return
        if not widgets.confirm(
                self, "Delete conversation", "Delete %s?" % (session.get("title") or "it"),
                "Everything kept of it goes: what was said, and what the agent did as "
                "it is recorded here. Files the agent changed are left as they are.",
                agree="Delete", kind="warn"):
            return

        def done(payload):
            if payload.get("ok"):
                self.sessions = [one for one in self.sessions if one.get("id") != session["id"]]
                self._drafts.pop(session["id"], None)
                self._draft_files.pop(session["id"], None)
                self._fill()
                if self._showing(session["id"]):
                    self._remember(session="")
                    self._show_new()
            else:
                self.composer.say(payload.get("error") or "Not deleted.", bad=True)

        self.ask("session-delete", lambda core, cancel: core.agents(
            "session-delete", session["id"]), done)

    # -- other agents (ADR-006) ---------------------------------------------------
    def agent_request(self, request):
        """The host asks the person about a task or a message (Approve)."""
        execution_id = request.get("execution_id")
        if not execution_id:
            return
        known = self._requests.get(execution_id) or {}
        self._requests[execution_id] = dict(known, **request, state="waiting")
        self._requests[execution_id].setdefault("at", time.time())
        self._sync_others()

    def agent_execution(self, execution):
        """Any execution the host reports: an agent's task or message from a
        conversation here becomes its card, and follows how it goes."""
        capability = str(execution.get("capability_id") or "")
        actor = str(execution.get("actor_id") or "")
        if capability.endswith(".message") and execution.get("state") == "succeeded":
            self._refresh_inbox()
        if capability.startswith("agents.") and capability.endswith((".delegate", ".message")):
            # Its conversation is made when it starts running: listed then, and once
            # more when it ends - not on each of its steps, which come as changes too.
            state = execution.get("state")
            if self._listed_states.get(execution.get("execution_id")) != state:
                self._listed_states[execution.get("execution_id")] = state
                if state in ("running", "succeeded", "failed", "canceled", "timed_out"):
                    self.load_sessions()
        if not (capability.startswith("agents.") and capability.endswith((".delegate", ".message"))
                and actor.startswith("agent:") and "/" in actor):
            return
        execution_id = execution.get("execution_id")
        request = self._requests.get(execution_id)
        if request is None:
            request = self._request_of(execution)
            self._requests[execution_id] = request
        before = (request.get("state"), request.get("session_id"), request.get("detail"))
        state = execution.get("state")
        error = execution.get("error") or {}
        output = execution.get("output") or {}
        if state in ("succeeded", "failed", "canceled", "timed_out", "unknown"):
            request["state"] = "denied" if error.get("code") == "denied" else state
        elif state == "running" or (state == "dispatched" and request.get("state") != "waiting"):
            request["state"] = "running" if request.get("state") != "approved" else "approved"
        if output.get("session_id"):
            request["session_id"] = output["session_id"]
        request["detail"] = self._request_detail(request, output, error)
        if (request.get("state"), request.get("session_id"), request.get("detail")) != before \
                or before == ("running", None, None):
            self._sync_others()          # not on each step of the task: only when its card changes

    def _request_of(self, execution):
        """A card for a task or message that never waited (Auto)."""
        capability = execution["capability_id"]
        parts = capability.split(".")
        role = parts[2] if len(parts) == 4 and parts[1] == "roles" else ""
        target = "" if role else parts[1]
        name = next((one.get("name") for one in self.connections if one.get("id") == target), target)
        request = dict(execution.get("input") or {})
        return {"execution_id": execution["execution_id"], "session": execution["actor_id"].split("/", 1)[1],
                "kind": "message" if capability.endswith(".message") else "task", "target": target,
                "target_name": name, "goal": request.get("goal") or "", "body": request.get("body") or "",
                "expects_reply": bool(request.get("expects_reply")), "mode": request.get("mode") or "",
                "model": request.get("model") or "", "effort": request.get("effort") or "",
                "files": len(request.get("files") or []), "artifacts": len(request.get("artifacts") or []),
                "state": "running", "at": execution.get("created_at") or time.time(), "role": role}

    @staticmethod
    def _request_detail(request, output, error):
        if error.get("code") == "denied":
            return ""
        if error:
            return error.get("message") or error.get("code") or ""
        if request.get("kind") == "message":
            reply = (output.get("reply") or {}).get("body")
            return ("Answer: " + reply[:400]) if reply else ("Delivered." if output else "")
        answer = str(output.get("answer") or "")
        if not output:
            return ""
        criteria = output.get("acceptance") or []
        said = answer[:400] + ("..." if len(answer) > 400 else "")
        if criteria:
            said += "\n%d acceptance %s, not verified yet." % (
                len(criteria), "criterion" if len(criteria) == 1 else "criteria")
        return said

    def _answer_request(self, execution_id, approve):
        request = self._requests.get(execution_id)
        if request is None or ChatTab.qcr_access is None:
            return
        ChatTab.qcr_access.answer_agent(execution_id, approve)
        request["state"] = "approved" if approve else "denied"
        self._sync_others()

    def _refresh_inbox(self):
        """What other agents wrote to the conversation open, or to its
        connection, and nobody has read yet."""
        session = self.current
        host = ChatTab.qcr_access
        if session is None or host is None or not _is_chat(session):
            return
        session_id, connection = session.get("id"), session.get("connection")
        found = {}

        def take(messages):
            if not self._showing(session_id):
                return
            for one in messages:
                if one.get("recipient_session") in (None, "", session_id):
                    found[one.get("message_id")] = one
            self._inbox = sorted(found.values(), key=lambda one: one.get("created_at") or 0)
            self._sync_others()

        host.messages(take, recipient_session=session_id, state="delivered")
        host.messages(take, recipient_connection=connection, state="delivered")

    def _dismiss_message(self, message_id):
        if ChatTab.qcr_access is not None:
            ChatTab.qcr_access.acknowledge_message(message_id)
        self._inbox = [one for one in self._inbox if one.get("message_id") != message_id]
        self._sync_others()

    def _sync_others(self):
        """The cards of the conversation open, in it: its tasks and messages
        for other agents (waiting for a yes, then how they went), and what
        other agents wrote to it and is not read yet (ADR-006)."""
        session_id = (self.current or {}).get("id")

        def made(request):
            def make():
                card = agentcards.AgentRequestCard(request, dismissible=False)
                card.approve.connect(lambda execution_id: self._answer_request(execution_id, True))
                card.deny.connect(lambda execution_id: self._answer_request(execution_id, False))
                card.open.connect(lambda found: found and self.open_session(found))
                return card
            return make

        def told(request):
            return lambda card: card.set_state(request.get("state") or "waiting", request.get("session_id") or "",
                                               request.get("detail") or "")
        self.feed.sync_cards("request", [
            (key, request.get("at") or 0, made(request), told(request))
            for key, request in self._requests.items() if session_id and request.get("session") == session_id])

        def inbox_card(message):
            def make():
                card = agentcards.InboxCard(message)
                card.dismiss.connect(self._dismiss_message)
                return card
            return make
        self.feed.sync_cards("inbox", [
            (message.get("message_id"), message.get("created_at") or 0, inbox_card(message), lambda card: None)
            for message in self._inbox])

    # -- woken when what it handed on ends (ADR-009) ------------------------------------
    def round_finished(self, found):
        """What a conversation handed on has ended, as it asked: go on by itself,
        or wait for the person's word, as the conversation says."""
        session_id = found.get("session")
        session = next((one for one in self.sessions if one.get("id") == session_id), None)
        if session is None:
            return
        mode = session.get("wake") if session.get("wake") in ("continue", "ask", "off") else "ask"
        if mode == "off":
            return                      # the results are on their cards
        document = {"message": agentcards.wake_message(found), "trigger": "tasks_finished"}
        counted = self._woke.setdefault(session_id, {"failed": 0, "all": 0})
        within = counted["failed"] < WAKE_FAILED_ROUNDS and counted["all"] < WAKE_ROUNDS
        if mode == "continue" and within:
            counted["all"] += 1
            counted["failed"] += 1 if found.get("failed") else 0
            self._go_on(session, document)
            return
        key = "%s/%d" % ((found.get("condition") or {}).get("id") or "", len(self._wakes.get(session_id, {})))
        self._wakes.setdefault(session_id, {})[key] = (found, document, "ask" if mode == "ask" else "limit")
        self._sync_wakes()

    def _go_on(self, session, document):
        """The woken turn: now, or first in line behind the turn running."""
        session_id = session["id"]
        if session_id in self.turns:
            self._queued.setdefault(session_id, []).insert(0, document)
            if self._showing(session_id):
                self._waiting()
            return
        self._start(session, document["message"], document=document)

    def _wake_answered(self, key, go):
        session_id = (self.current or {}).get("id")
        found = self._wakes.get(session_id, {}).pop(key, None)
        self._sync_wakes()
        if found is not None and go:
            # The person's word: QAVector may go on by itself again from here.
            self._woke.pop(session_id, None)
            self._go_on(self.current, found[1])

    def _sync_wakes(self):
        session_id = (self.current or {}).get("id")

        def made(key, found, reason):
            def make():
                card = agentcards.WakeCard(key, found, reason)
                card.go.connect(lambda key: self._wake_answered(key, True))
                card.dismiss.connect(lambda key: self._wake_answered(key, False))
                return card
            return make
        self.feed.sync_cards("wake", [
            (key, time.time(), made(key, found, reason), lambda card: None)
            for key, (found, _document, reason) in self._wakes.get(session_id, {}).items()])

    def integration_changes_changed(self):
        """An agent's change to Jira waits, was answered or ended (ADR-007): its
        card, in the conversation whose agent proposed it, where it happened."""
        session_id = (self.current or {}).get("id")
        changes = (getattr(ChatTab.qcr_access, "integration_changes", None) or {}).values()
        self.feed.set_changes([one for one in changes if session_id and one.get("session") == session_id])

    def _others_chosen(self, value):
        """A begun conversation keeps what it may do with other agents; a new one
        takes the choice with it when it starts."""
        session = self.current
        if session is None or session.get("others") == value:
            return

        def done(payload):
            if payload.get("ok"):
                self._put(payload.get("session") or dict(session, others=value))
            else:
                self.composer.say(payload.get("error") or "The choice for other agents was not "
                                  "saved.", bad=True)

        session["others"] = value             # the next turn asks with it at once
        self.ask("session-set", lambda core, cancel: core.agents(
            "session-set", session["id"], document={"others": value}), done)

    def _wake_chosen(self, value):
        """A begun conversation keeps how it goes on when its tasks end."""
        session = self.current
        if session is None or session.get("wake") == value:
            return

        def done(payload):
            if payload.get("ok"):
                self._put(payload.get("session") or dict(session, wake=value))
            else:
                self.composer.say(payload.get("error") or "The choice was not saved.", bad=True)

        session["wake"] = value
        self.ask("session-set", lambda core, cancel: core.agents(
            "session-set", session["id"], document={"wake": value}), done)

    # -- restrictions -------------------------------------------------------------
    def edit_restrictions(self):
        """What the folder holds the agent to - the window every Project
        folder's lock opens."""
        folder = self.composer.settings()["folder"]
        if not folder:
            return
        session = self.current
        window = restrictions_mod.RestrictionsWindow(
            folder_text(folder), folder, "project", self.composer.restrictions(),
            core=self.core(), probe={"folder": folder, "cwd": folder}, parent=self)
        accepted = window.exec() == QDialog.Accepted and window.saved is not None
        saved = window.saved
        window.deleteLater()
        if not accepted:
            return
        if session is None:
            self._new_restrictions = dict(saved)
            self.composer.set_restrictions(saved)
            return

        def done(payload):
            if payload.get("ok"):
                self._put(payload.get("session") or session)
                self.composer.set_restrictions((payload.get("session") or {}).get(
                    "restrictions") or {})
            else:
                self.composer.say(payload.get("error") or "The restrictions were not "
                                  "saved.", bad=True)

        self.ask("session-set", lambda core, cancel: core.agents(
            "session-set", session["id"], document={"restrictions": saved}), done)

    # -- skills -------------------------------------------------------------------
    def set_library(self, skills, own=None):
        """The skills library as the core last listed it."""
        self.library = list(skills or [])
        self.own = dict(own or {})

    def choose_skills(self):
        """Open the list of the library's skills, ticked for this conversation.
        The library is read when the list is opened: a folder, never a fetch."""
        if self.host is None:
            return

        def show():
            from .agentskills import SkillsPanel
            if self.skills_panel is None:
                self.skills_panel = SkillsPanel(self)
                self.skills_panel.changed.connect(self._skills_chosen)
                self.skills_panel.add_requested.connect(self.skills_wanted)
            chosen = self.composer.skills()
            self.skills_panel.set_skills(self.library, chosen, self._own(),
                                         self._scripts_note(chosen))
            self.skills_panel.open_over(self.composer.skills_button)

        self.host.load_skills(then=show)

    def _own(self):
        """The skills the agent of this conversation has by itself."""
        kind = (self.current or {}).get("kind") or next(
            (one.get("kind") for one in self.connections
             if one.get("id") == self.composer.connection.currentData()), "")
        return self.own.get(kind) or []

    def _scripts_note(self, chosen):
        """What to say when a chosen skill has files to run and the turn does
        not let the agent run anything."""
        if self.composer.settings()["mode"] == "full":
            return ""
        runnable = [one.get("name") for one in self.library
                    if one.get("name") in chosen and one.get("scripts")]
        if not runnable:
            return ""
        return ("%s %s scripts: the agent can read them, and runs them only with Full "
                "access." % (", ".join(runnable), "has" if len(runnable) == 1 else "have"))

    def _skills_chosen(self, names):
        names = list(names or [])
        self.composer.set_skills(names)
        if self.skills_panel is not None:
            # Said the moment the tick is made, where the tick is.
            self.skills_panel.set_note(self._own(), self._scripts_note(names))
        session = self.current
        if session is None:
            self._new_skills = names
            return

        def done(payload):
            if payload.get("ok"):
                self._put(payload.get("session") or session)
            else:
                self.composer.say(payload.get("error") or "The skills were not saved.",
                                  bad=True)
                if self._showing(session["id"]):
                    self.composer.set_skills((self.current or {}).get("skills") or [])

        self.ask("session-set", lambda core, cancel: core.agents(
            "session-set", session["id"], document={"skills": names}), done)
