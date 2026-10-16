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
                               QPushButton, QTabWidget, QTextEdit, QVBoxLayout)

from . import findbar, theme, widgets
from .approvaltext import ApprovalText, review_facts
from .choicetable import SINGLE, ChoiceTable

#: A review is a document, with room to read it and resize handles to grow it.
REVIEW_WIDTH, REVIEW_HEIGHT = 960, 860
#: The tallest a choice's own detail grows above its table, before it scrolls.
CHOICE_DETAIL_HEIGHT = 120

#: The longest text an input question sends back; the core refuses more.
MAX_TEXT = 8000

#: A dismissed question, on the wire. Empty because the core reads an empty
#: answer as nobody having answered, and because no option can be spelled this
#: way - a blank button would have nothing written on it.
DISMISSED = ""


class AskDialog(QDialog):
    """One question, its detail, and the answers it takes as buttons."""

    def __init__(self, question, detail="", options=(), parent=None, revision=None,
                 auto="", text=None, choice=None):
        super().__init__(parent)
        #: The answer the question takes by itself when its time runs out.
        self.auto = str(auto or "")
        self.setModal(False)
        self.answer = DISMISSED
        self._answered = False
        self.feedback = ""
        self.revise = False
        #: What was written, for a question that asks for text; None otherwise.
        self.text = None
        self._text = dict(text) if text is not None else None
        #: The rows ticked, for a question that asks for a choice; None otherwise.
        self.chosen = None
        self._choice = dict(choice) if choice is not None else None
        self.setWindowTitle("Choose and send" if self._choice is not None
                            else "Write and send" if self._text is not None
                            else "Review and decide")
        revision = dict(revision or {})
        self._approving = {str(one).strip().lower() for one in
                           revision.get("approvals", (options or ("Approve",))[:1])}
        self.frame = widgets.dress(self)
        available = self.screen().availableGeometry()
        # A table to pick from needs the room a review does, whatever its detail.
        roomy = bool(detail) or self._choice is not None
        width = min(REVIEW_WIDTH if roomy else 640, max(320, available.width() - 48))
        height = min(REVIEW_HEIGHT if roomy else 320, max(240, available.height() - 48))
        self.setMinimumSize(min(640, width), min(440 if roomy else 240, height))
        self.resize(width, height)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(14)
        layout.addWidget(widgets.kicker(
            "Your choice" if self._choice is not None else
            "Your input" if self._text is not None else "Your decision"))

        self.question = widgets.heading(question)
        self.question.setTextFormat(Qt.PlainText)
        self.question.setWordWrap(True)
        self.question.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.question)
        single = (self._choice or {}).get("mode") == SINGLE
        layout.addWidget(widgets.lede(
            ("Pick the one this run should take." if single else
             "Tick every one this run should take.")
            if self._choice is not None else
            "Write your answer below; later steps of this run use it."
            if self._text is not None else
            "Review the details below before choosing how this step should continue."
            if detail else "Choose an answer to continue this step."))

        self.facts = []
        facts = QHBoxLayout()
        # A review's figures, read off its text. A choice's detail is a note
        # above a table, not a review, and has none.
        for label, value in (review_facts(detail) if self._choice is None else ()):
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

        self.choice_table = None
        if self._choice is not None:
            if detail:
                self.choice_note = _fitted_note(detail)
                layout.addWidget(self.choice_note)
            self.choice_table = ChoiceTable(self._choice)
            layout.addWidget(self.choice_table, 1)
        elif detail:
            self.detail_tabs = QTabWidget()
            self.review = ApprovalText(detail)
            self.original = QPlainTextEdit(detail)
            self.original.setReadOnly(True)
            self.original.setFont(theme.mono_font())
            self.detail_tabs.addTab(self.review, "Review")
            self.detail_tabs.addTab(self.original, "Original text")
            # Ctrl+F over the review, or the original text - whichever is on show.
            self.find_bar = findbar.FindBar()
            self.detail_tabs.currentChanged.connect(
                lambda _index: self.find_bar.retarget(self._find_target()))
            findbar.install(self, lambda: (self.find_bar, self._find_target()))
            layout.addWidget(self.find_bar)
            layout.addWidget(self.detail_tabs, 1)
        elif self._text is None:
            layout.addStretch(1)

        self.text_editor = None
        if self._text is not None:
            self.text_editor = QPlainTextEdit(str(self._text.get("default") or ""))
            self.text_editor.setPlaceholderText(
                str(self._text.get("placeholder") or "") or "Write here")
            layout.addWidget(self.text_editor, 1)

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
            # Its answer, apart from its label: a countdown changes the label.
            button.setProperty("answer", one)
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
        if self.text_editor is not None:
            self.text_editor.textChanged.connect(self._text_changed)
            self._text_changed()
        if self.choice_table is not None:
            self.choice_table.changed.connect(self._choice_changed)
            self._choice_changed()
        layout.addLayout(row)
        note = widgets.lede(
            "If nobody answers, the counting button is pressed when it reaches "
            "zero. Closing this window ends the request without approval."
            if self.auto else
            "Closing this window ends the request without an answer; the step fails."
            if self._text is not None or self._choice is not None else
            "Closing this window ends the request without approval.")
        note.setProperty("role", "hint")
        layout.addWidget(note)
        if self.choice_table is not None:
            self.choice_table.table.setFocus(Qt.OtherFocusReason)
        elif self.text_editor is not None:
            self.text_editor.setFocus(Qt.OtherFocusReason)
        elif detail:
            self.review.setFocus(Qt.OtherFocusReason)

    def _find_target(self):
        """The review or the original text, whichever tab is on show."""
        return findbar.target_for(self.detail_tabs.currentWidget())

    def _text_changed(self):
        """The first answer sends the text on, so it waits for some when required."""
        text = self.text_editor.toPlainText().strip()
        empty = self._text.get("required", True) and not text
        long = len(text) > MAX_TEXT
        first = self.answer_buttons[0] if self.answer_buttons else None
        if first is not None:
            first.setEnabled(not empty and not long)
            first.setToolTip("Keep it within 8,000 characters." if long else
                             "Write something first." if empty else "")

    def _choice_changed(self):
        """The first answer hands the ticked rows on, so it waits for a valid set."""
        first = self.answer_buttons[0] if self.answer_buttons else None
        if first is None:
            return
        ready = self.choice_table.valid()
        first.setEnabled(ready)
        first.setToolTip("" if ready else
                         "Pick one row." if self.choice_table.mode == SINGLE else
                         "Tick at least one row.")

    def _feedback_changed(self):
        text = self.feedback_editor.toPlainText().strip()
        self.revision_button.setEnabled(0 < len(text) <= 8000)
        self.revision_button.setToolTip(
            "Keep feedback within 8,000 characters." if len(text) > 8000 else
            "Revise the plan and ask for approval again.")
        # Do not silently discard typed feedback by treating it as approval.
        for button in self.answer_buttons:
            if str(button.property("answer")).strip().lower() in self._approving:
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
        button = self.auto_button()
        if button is not None:
            # On the button that presses itself, the way "In plan (20)" counts
            # down on the new-task window: one habit for every timed answer.
            self._left.setText("")
            button.setText("%s (%d:%02d)" % (self.auto, max(0, seconds) // 60,
                                             max(0, seconds) % 60)
                           if seconds > 0 else self.auto)
            return
        if seconds <= 0:
            self._left.setText("")
            return
        if seconds >= 120:
            self._left.setText("%d minutes left" % (seconds // 60))
        else:
            self._left.setText("%d seconds left" % seconds)

    def auto_button(self):
        """The button for the answer taken by itself, or None."""
        if not self.auto:
            return None
        for button in self.answer_buttons:
            if button.property("answer") == self.auto:
                return button
        return None

    def _choose(self, text):
        self.answer = text
        if self.text_editor is not None:
            self.text = self.text_editor.toPlainText().strip()
        if self.choice_table is not None:
            self.chosen = self.choice_table.chosen()
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
                           parent=self._parent, revision=event.get("revision"),
                           auto=str(event.get("auto") or ""),
                           text=event.get("text"), choice=event.get("choice"))
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
        if found[0].auto:
            # The core takes the answer itself and then withdraws the question.
            # Closing it here would send a dismissal - which is no answer, and
            # could beat the core to it.
            found[0].countdown(0)
            found[1].stop()
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
        if dialog.text is not None:
            extra["text"] = dialog.text
        if dialog.chosen is not None:
            extra["chosen"] = dialog.chosen
        self._send(command="ask.result", id=request_id, answer=dialog.answer,
                   who=_who(), **extra)
        dialog.deleteLater()


def _fitted_note(text):
    """The note above a choice's table: as tall as its text, then it scrolls.

    One line stays one line rather than a tall empty box, and a page of text
    stops at :data:`CHOICE_DETAIL_HEIGHT` and scrolls in place - the table is
    what the window is for, and a long note must not push it, or the window,
    off the bottom of the screen.
    """
    note = QTextEdit()
    note.setReadOnly(True)
    note.setPlainText(text)

    def fit(size):
        wanted = int(size.height()) + 2 * note.frameWidth()
        note.setFixedHeight(min(CHOICE_DETAIL_HEIGHT, wanted))

    note.document().documentLayout().documentSizeChanged.connect(fit)
    fit(note.document().size())
    return note


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
