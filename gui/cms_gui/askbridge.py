"""Putting a run's question to the person sitting in front of it.

A cycle step reaches a person the same way a scenario reaches a service: the
core emits a request on ``--events`` and blocks, this answers it on
``--control``. The core half is :mod:`cycle.ask`; between them they are the
whole feature and neither imports the other - the shape :mod:`servicebridge`
already has, for the same reason.

**The window is not modal.** Steps run in parallel, so two gates can be open at
once, and a modal dialog would make the second one unanswerable until the first
was dealt with - two steps deadlocked by a decision about how the window was
drawn. Each question gets its own window, and everything else in the
application keeps working while they stand.

**Closing the window is not an answer.** It is delivered as one - an empty one -
rather than being left silent, because the alternative is a run blocked for the
rest of its ten minute deadline by somebody who has already decided not to look
at it. The core reads an empty answer as *not answered*, so the gate fails,
which is what closing it without deciding ought to mean.

**The deadline is the core's.** The window carries its own timer only so a
question nobody can answer any more stops standing on screen claiming it can
be. Whichever of the two arrives first, the answer is the same one.
"""

import getpass

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QTabWidget, QVBoxLayout)

from . import theme, widgets
from .approvaltext import ApprovalText, review_facts

#: A review is a document, with room to read it and resize handles to grow it.
REVIEW_WIDTH, REVIEW_HEIGHT = 960, 860

#: A dismissed question, on the wire. Empty because the core reads an empty
#: answer as nobody having answered, and because no option can be spelled this
#: way - a blank button would have nothing written on it.
DISMISSED = ""


class AskDialog(QDialog):
    """One question, its detail, and the answers it takes as buttons."""

    def __init__(self, question, detail="", options=(), parent=None, revision=None):
        super().__init__(parent)
        self.setWindowTitle("Review and decide")
        self.setModal(False)
        self.answer = DISMISSED
        self._answered = False
        self.feedback = ""
        self.revise = False
        revision = dict(revision or {})
        self._approving = {str(one).strip().lower() for one in
                           revision.get("approvals", (options or ("Approve",))[:1])}
        self.frame = widgets.dress(self)
        available = self.screen().availableGeometry()
        width = min(REVIEW_WIDTH if detail else 640, max(320, available.width() - 48))
        height = min(REVIEW_HEIGHT if detail else 320, max(240, available.height() - 48))
        self.setMinimumSize(min(640, width), min(440 if detail else 240, height))
        self.resize(width, height)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(14)
        layout.addWidget(widgets.kicker("Your decision"))

        self.question = widgets.heading(question)
        self.question.setTextFormat(Qt.PlainText)
        self.question.setWordWrap(True)
        self.question.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.question)
        layout.addWidget(widgets.lede(
            "Review the details below before choosing how this step should continue."
            if detail else "Choose an answer to continue this step."))

        self.facts = []
        facts = QHBoxLayout()
        for label, value in review_facts(detail):
            card = QFrame()
            card.setProperty("role", "panel")
            column = QVBoxLayout(card)
            column.setContentsMargins(12, 10, 12, 10)
            column.setSpacing(5)
            column.addWidget(widgets.kicker(label))
            content = QLabel(value)
            content.setTextFormat(Qt.PlainText)
            content.setWordWrap(True)
            content.setTextInteractionFlags(Qt.TextSelectableByMouse)
            content.setStyleSheet("font-size: 13px; font-weight: 600;")
            column.addWidget(content)
            facts.addWidget(card, 1)
            self.facts.append((label, content))
        if self.facts:
            layout.addLayout(facts)

        if detail:
            self.detail_tabs = QTabWidget()
            self.review = ApprovalText(detail)
            self.original = QPlainTextEdit(detail)
            self.original.setReadOnly(True)
            self.original.setFont(theme.mono_font())
            self.detail_tabs.addTab(self.review, "Review")
            self.detail_tabs.addTab(self.original, "Original text")
            layout.addWidget(self.detail_tabs, 1)
        else:
            layout.addStretch(1)

        self.feedback_editor = None
        if revision:
            label = widgets.heading("Request changes", "h2")
            layout.addWidget(label)
            self.feedback_editor = QPlainTextEdit()
            self.feedback_editor.setPlaceholderText(
                "What should the agent change or clarify in the plan?")
            self.feedback_editor.setMaximumHeight(84)
            self.feedback_editor.setEnabled(bool(revision.get("enabled")))
            layout.addWidget(self.feedback_editor)
            remaining = revision.get("remaining", 0)
            layout.addWidget(widgets.lede(
                "Your feedback goes to the planning agent, then the revised plan is reviewed "
                "and shown here again. %s revision(s) left. Agent calls may incur charges."
                % remaining if remaining else
                "The revision limit for this run has been reached. You can approve or decline."))

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setStyleSheet("color: %s;" % theme.DIVIDER)
        layout.addWidget(divider)
        self._left = QLabel("")
        self._left.setProperty("role", "muted")
        self._left.setToolTip("Time remaining to answer. Expiry does not approve this step.")

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self._left)
        row.addStretch(1)
        self.revision_button = None
        if self.feedback_editor is not None:
            self.revision_button = QPushButton("Send for revision")
            self.revision_button.setAutoDefault(False)
            self.revision_button.setEnabled(False)
            self.revision_button.clicked.connect(self._request_revision)
            row.addWidget(self.revision_button)
        self.answer_buttons = []
        for index, one in enumerate(options or ("Approve", "Reject")):
            button = QPushButton(one)
            if index == 0:
                button.setProperty("variant", "primary")
            button.setAutoDefault(False)
            button.setDefault(False)
            button.clicked.connect(lambda _checked=False, text=one:
                                   self._choose(text))
            row.addWidget(button)
            self.answer_buttons.append(button)
        if self.feedback_editor is not None:
            self.feedback_editor.textChanged.connect(self._feedback_changed)
        layout.addLayout(row)
        note = widgets.lede("Closing this window ends the request without approval.")
        note.setProperty("role", "hint")
        layout.addWidget(note)
        if detail:
            self.review.setFocus(Qt.OtherFocusReason)

    def _feedback_changed(self):
        text = self.feedback_editor.toPlainText().strip()
        self.revision_button.setEnabled(0 < len(text) <= 8000)
        self.revision_button.setToolTip(
            "Keep feedback within 8,000 characters." if len(text) > 8000 else
            "Revise the plan and ask for approval again.")
        # Do not silently discard typed feedback by treating it as approval.
        for button in self.answer_buttons:
            if button.text().strip().lower() in self._approving:
                button.setEnabled(not text)

    def _request_revision(self):
        text = self.feedback_editor.toPlainText().strip()
        if not self.revision_button.isEnabled() or not text:
            return
        self.answer = DISMISSED
        self.feedback = text
        self.revise = True
        self._answered = True
        self.accept()

    def countdown(self, seconds):
        """Say how long is left, so nobody decides after it stopped mattering."""
        if seconds <= 0:
            self._left.setText("")
            return
        if seconds >= 120:
            self._left.setText("%d minutes left" % (seconds // 60))
        else:
            self._left.setText("%d seconds left" % seconds)

    def _choose(self, text):
        self.answer = text
        self._answered = True
        self.accept()

    def reject(self):
        """Closing the window, by its button or by Escape.

        Left as a dismissal rather than turned into one of the options: the
        answers are the step's own and none of them can be assumed.
        """
        self.answer = DISMISSED
        self.feedback, self.revise = "", False
        super().reject()


class AskBridge(QObject):
    """Turns ``cycle.ask`` events into windows, and windows into answers."""

    def __init__(self, send, parent=None):
        """``send`` is the callable that writes one control command (**kwargs)."""
        super().__init__(parent)
        self._send = send
        self._parent = parent
        #: request id -> (dialog, timer)
        self._open = {}

    def handle(self, event):
        """Take one ``cycle.ask`` or ``cycle.ask.withdrawn`` event. True when it was one."""
        if event.get("kind") == "cycle.ask.withdrawn":
            self.withdraw(event.get("id"))
            return True
        if event.get("kind") != "cycle.ask":
            return False
        request_id = event.get("id")
        if request_id is None or request_id in self._open:
            # Already on screen: a duplicated event must not put up a second
            # window whose answer nobody is waiting for.
            return request_id is not None

        dialog = AskDialog(str(event.get("question") or "Continue?"),
                           detail=str(event.get("detail") or ""),
                           options=[str(one) for one in
                                    (event.get("options") or [])],
                           parent=self._parent, revision=event.get("revision"))
        dialog.finished.connect(lambda _code, rid=request_id: self._done(rid))

        timer = QTimer(self)
        timer.setInterval(1000)
        seconds = [int(event.get("timeout_ms") or 0) // 1000]
        timer.timeout.connect(lambda: self._tick(request_id, seconds))
        self._open[request_id] = (dialog, timer)
        if seconds[0] > 0:
            dialog.countdown(seconds[0])
            timer.start()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return True

    def cancel_all(self):
        """Close every open question. The run has gone; nobody is waiting.

        No answer is sent: the thing that would have read it is not there, and
        a control command written to a launcher that has exited goes nowhere.
        """
        for request_id in list(self._open):
            dialog, timer = self._open.pop(request_id)
            timer.stop()
            dialog.close()
            dialog.deleteLater()

    def withdraw(self, request_id):
        """Close one question the core stopped waiting for. Sends nothing back.

        The step it was about has finished or been stopped, so an answer would
        be read by nobody - the same reasoning as :meth:`cancel_all`, for one.
        """
        found = self._open.pop(request_id, None)
        if found is None:
            return
        dialog, timer = found
        timer.stop()
        dialog.close()
        dialog.deleteLater()

    def _tick(self, request_id, seconds):
        """One second of the core's deadline, counted down on the window."""
        found = self._open.get(request_id)
        if found is None:
            return
        seconds[0] -= 1
        if seconds[0] > 0:
            found[0].countdown(seconds[0])
            return
        # The core has stopped waiting. Taking the window away is the honest
        # thing: a button that no longer decides anything should not be there
        # to be pressed. Closing it sends a dismissal like any other close,
        # which the core ignores as an answer nobody was waiting for - and
        # which unblocks the step if this clock happened to be the faster of
        # the two.
        found[0].close()

    def _done(self, request_id):
        found = self._open.pop(request_id, None)
        if found is None:
            return
        dialog, timer = found
        timer.stop()
        extra = {"revise": True, "feedback": dialog.feedback} if dialog.revise else {}
        self._send(command="ask.result", id=request_id, answer=dialog.answer,
                   who=_who(), **extra)
        dialog.deleteLater()


def _who():
    """Whoever is at this machine, by the name the machine knows them by.

    The account name rather than anything from an account elsewhere: this only
    has to distinguish people at the keyboard in a run record, and a name taken
    from somewhere else would be a detail nobody asked to have written down.
    """
    try:
        return getpass.getuser()
    except Exception:                    # no account name on this platform
        return ""
