"""Writing to a running cycle: who is told, and the draft that tells them.

The core says, as the run goes, which steps take a message and when they would
read it (``cycle.interaction.offers``); this builds the input from that and
nothing else. There is no list of plugins here and no guess from a step's
category - a step can be written to because the core offered it.

**The recipient is whoever is working.** With one step running that takes
messages, that step is the recipient, without being asked for; with none
running, the rest of the run is. That is what somebody watching a run means
nearly every time they start typing, and it is said beside the name ("auto")
so it is never mistaken for a choice somebody made.

**Two rules keep that honest.** It is never read off the newest stage - the
feed is the whole run's, and the last row says nothing about who a person is
talking to. And once a draft is begun, its recipient stays put: a step
finishing, another one starting or a node selected on the canvas does not
carry the text somewhere its author did not see. When the step it was begun
for stops listening, the draft says so and waits for the person.

**Where it cannot know, it asks.** Two steps running at once is the one case
where "whoever is working" has two answers. The recipient then reads as a
choice to make - picking one on the canvas makes it, as does the list.

**Sent is not taken.** Enter writes a line to the core; the message is taken
only when the run has saved it and said so. Until then the draft waits, and
Enter again sends the same id, which the core treats as the same message.
Shift+Enter starts a new line. There is no Send button: a clarification is
written the way a chat line is, and Ctrl+Enter already belongs to Run.

**From anywhere.** :class:`ClarifyPopup` is the same composer over whatever
window is in front, opened by :data:`SHORTCUT` - the run does not wait for
somebody to find the Cycles page and its Stages tab.
"""

import copy
import getpass
import uuid

from PySide6.QtCore import QEvent, QPointF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QMenu, QPlainTextEdit, QPushButton,
                               QToolButton, QVBoxLayout)

from . import theme, widgets

#: The most a message may say. The core's own limit, mirrored rather than
#: imported, like every other value the GUI shares with the core.
MAX_TEXT = 8000

#: What the rest of the run is called as a recipient.
RUN_RECIPIENT = "Further work of this run"

#: What opens :class:`ClarifyPopup` from any window of the application.
SHORTCUT = "Ctrl+Shift+M"

#: How to send, said in the empty text box.
KEYS = " - Enter sends, Shift+Enter adds a line"

#: How tall the text box may grow, in lines, before it scrolls instead. Enough
#: for a paragraph; the panel it sits in is the stages feed's, and a box that
#: kept growing would push the feed off the top of it.
MAX_LINES = 4

#: The states of an offer that mean the step is working now - which is what
#: "whoever is working" reads. Waiting is still working: a step that asked to
#: be come back to is part-way through its execution.
WORKING = ("running", "waiting")


def target_key(scope, step="", execution=None):
    return scope, step, execution


def recipient_label(scope, step="", execution=None, labels=None, state=""):
    """How a recipient reads: its name, which run of it, and whether it has begun."""
    if scope == "run":
        return RUN_RECIPIENT
    name = (labels or {}).get(step) or step
    if execution and execution > 1:
        # Only when it matters: a step sent round again by a plan revision is
        # a different recipient from the one a message was written to before.
        name += " - execution %s" % execution
    if state == "pending":
        name += " (not started)"
    elif state == "waiting":
        name += " (waiting)"
    return name


def _who():
    """Whoever is at this machine - the same name an approval is signed with."""
    try:
        return getpass.getuser()
    except Exception:                    # no account name on this platform
        return ""


def _dot(state):
    """The mark before a recipient's name: filled for working, open for not yet.

    Painted, never typed - ``test_theme`` refuses a glyph above U+2100, and a
    mark that depends on the font is not a drawing.
    """
    size = 12
    canvas = QPixmap(size, size)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing, True)
    ink = QColor(theme.ACCENT if state in WORKING or state == "run"
                 else theme.NEUTRAL[500])
    pen = painter.pen()
    pen.setColor(ink)
    pen.setWidthF(1.6)
    painter.setPen(pen)
    middle = QPointF(size / 2.0, size / 2.0)
    if state == "run":
        # The rest of the run: a ring round a dot - every step still to come.
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(middle, 4.4, 4.4)
        painter.setBrush(ink)
        painter.drawEllipse(middle, 1.6, 1.6)
    elif state in WORKING:
        painter.setBrush(ink)
        painter.drawEllipse(middle, 3.6, 3.6)
    else:
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(middle, 3.4, 3.4)
    painter.end()
    return QIcon(canvas)


class _RecipientButton(QToolButton):
    """The recipient: a choice from a list, with the caret a list has.

    The caret is painted here rather than left to the style's menu indicator,
    which some platforms draw as a bar - a mark that should say "choose" must
    say it everywhere.
    """

    def paintEvent(self, event):             # noqa: N802 - Qt's own spelling
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.NEUTRAL[600] if self.isEnabled()
                                else theme.NEUTRAL[400]))
        right, middle = self.width() - 12.0, self.height() / 2.0
        painter.drawPolygon([QPointF(right - 4, middle - 2), QPointF(right + 4, middle - 2),
                             QPointF(right, middle + 3)])
        painter.end()


class InteractionComposer(QFrame):
    """A draft belongs to one run and one execution, never to a graph node."""

    submitted = Signal(dict)
    #: The run answered about a message on its way: ``(taken, what to say)``.
    settled = Signal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "composer")
        self._run_id = ""
        self._live = False
        self._offers = {}
        self._targets = {}              # key -> (label, offer, state)
        self._labels = {}
        self._selected = ""
        #: What the person picked from the list, or None for "whoever is
        #: working". Kept until the message goes or they go back to automatic.
        self._manual = None
        #: The recipient a draft was begun for, when it was chosen for them.
        #: Fixed the moment the first character is typed - see the docstring.
        self._locked = None
        self._pending = None
        self._notice, self._notice_bad = "", False
        self._drafts = {}
        self._listed = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.to = widgets.kicker("To")
        head.addWidget(self.to)
        self.recipient = _RecipientButton()
        self.recipient.setProperty("variant", "recipient")
        self.recipient.setProperty("hasmenu", "true")
        self.recipient.setPopupMode(QToolButton.InstantPopup)
        self.recipient.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.recipient.setMenu(QMenu(self.recipient))
        head.addWidget(self.recipient)
        self.mode = QLabel("AUTO")
        self.mode.setProperty("role", "modetag")
        self.mode.setToolTip("Chosen for you: the step that is working now. "
                             "Pick another from the list to choose yourself.")
        head.addWidget(self.mode)
        self.automatic = QPushButton("Back to automatic")
        self.automatic.setProperty("variant", "ghost")
        self.automatic.clicked.connect(lambda: self._pick(None))
        head.addWidget(self.automatic)
        self.when = widgets.ElidedLabel(mode=Qt.ElideRight)
        self.when.setProperty("role", "hint")
        head.addWidget(self.when, 1)
        outer.addLayout(head)

        self.text = QPlainTextEdit()
        self.text.setTabChangesFocus(True)
        self.text.setToolTip("Enter sends, Shift+Enter starts a new line. %s opens "
                             "this from any window of the application." % SHORTCUT)
        self.text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.text.installEventFilter(self)
        outer.addWidget(self.text)

        self.note = QLabel("")
        self.note.setProperty("role", "composernote")
        self.note.setWordWrap(True)
        self.note.setTextFormat(Qt.PlainText)
        outer.addWidget(self.note)

        #: What stands in for all of the above when nothing can be written to.
        self.closed = QLabel("")
        self.closed.setProperty("role", "hint")
        self.closed.setWordWrap(True)
        outer.addWidget(self.closed)

        self.text.textChanged.connect(self._typed)
        self._fit()
        self.set_context({}, False)

    # -- what the run offers ----------------------------------------------------
    def set_context(self, cycle, live, labels=None, selected=""):
        """What the run offers now. Called on every repaint of the page.

        Cheap when nothing changed, and it must be: a running cycle repaints
        several times a second, and a menu rebuilt each time would close under
        the hand of somebody choosing from it.
        """
        cycle = cycle or {}
        run_id = cycle.get("run_id", "")
        if run_id != self._run_id:
            if self._run_id:
                self._drafts[self._run_id] = (self.text.toPlainText(), self._manual,
                                              self._locked, self._pending,
                                              self._notice, self._notice_bad)
            self._run_id = run_id
            (draft, self._manual, self._locked, self._pending, self._notice,
             self._notice_bad) = self._drafts.pop(run_id, ("", None, None, None, "", False))
            self.text.blockSignals(True)
            self.text.setPlainText(draft)
            self.text.blockSignals(False)
            self._fit()
        self._live = bool(live and run_id)
        self._labels = dict(labels or {})
        self._selected = selected or ""
        self._offers = cycle.get("interaction_offers") or {}
        self._targets = {}
        run_offer = self._offers.get("run") or {}
        if "text" in (run_offer.get("kinds") or []):
            self._targets[target_key("run")] = (RUN_RECIPIENT, run_offer, "run")
        for step, offer in (self._offers.get("steps") or {}).items():
            if "text" in (offer.get("kinds") or []):
                state = offer.get("state", "")
                key = target_key("step", step, offer.get("execution"))
                self._targets[key] = (recipient_label(
                    "step", step, offer.get("execution"), self._labels, state),
                    offer, state)
        if self._pending:
            message = (cycle.get("interactions") or {}).get(self._pending["id"])
            if message and message.get("state") not in ("sending", "unconfirmed"):
                self._settled(message)
        if (self.text.toPlainText().strip() and self._manual is None
                and self._locked is None and not self._pending):
            # Written while nobody could be named - two steps working, or the
            # offers not in yet. The first recipient it gets is the one it
            # keeps: gaining one is shown, but it must not then change hands.
            self._locked = self._automatic()
        self._list()
        self._refresh()

    def _settled(self, message):
        """The run has answered about the message on its way."""
        sent_to = self._label_of(self._key_of(self._pending))
        self._pending = None
        if message.get("state") == "rejected":
            self._notice = message.get("reason") or "The message was refused."
            self._notice_bad = True
            self.settled.emit(False, self._notice)
            return
        self._notice = ("Sent to %s. What becomes of it shows in the stages feed."
                        % sent_to)
        self._notice_bad = False
        self.settled.emit(True, "Clarification sent to %s" % sent_to)
        self._manual = self._locked = None
        self.text.blockSignals(True)
        self.text.clear()
        self.text.blockSignals(False)
        self._fit()

    # -- who it goes to --------------------------------------------------------
    def working(self):
        """The recipients that are steps working now, as keys."""
        return [key for key, (_label, _offer, state) in self._targets.items()
                if key[0] == "step" and state in WORKING]

    def _automatic(self):
        """Whoever is working: one step, the rest of the run, or None to ask."""
        working = self.working()
        if len(working) == 1:
            return working[0]
        if len(working) > 1:
            chosen = [key for key in working if key[1] == self._selected]
            return chosen[0] if chosen else None
        return target_key("run") if target_key("run") in self._targets else None

    def current_key(self):
        """Who the draft would go to now, or None when that is not settled."""
        if self._pending:
            return self._key_of(self._pending)
        if self._manual is not None:
            return self._manual
        if self._locked is not None:
            return self._locked
        return self._automatic()

    def is_automatic(self):
        return self._manual is None

    def choose(self, key):
        """Pick a recipient, as a person picking it from the list would."""
        if key is not None and key not in self._targets:
            return False
        self._pick(key)
        return True

    def recipients(self):
        """What the list offers, as its rows read. For a test to check."""
        return [action.text() for action in self.recipient.menu().actions()
                if action.data() is not None and not action.isSeparator()]

    def _pick(self, key):
        self._manual = key
        self._locked = None if key is None else self._locked
        if key is None and self.text.toPlainText().strip():
            # Back to automatic with a draft in hand: fixed on whoever that is
            # now, so it does not wander off while the person finishes typing.
            self._locked = self._automatic()
        self._notice, self._notice_bad = "", False
        self._list()
        self._refresh()

    def _list(self):
        """The menu of recipients, rebuilt only when what it lists has changed."""
        listed = [(key, label, state) for key, (label, _offer, state)
                  in self._targets.items()]
        current = self.current_key()
        if current is not None and current not in self._targets:
            listed.append((current, recipient_label(*current, labels=self._labels)
                           + " - no longer takes messages", "gone"))
        signature = (listed, self._manual)
        if signature == self._listed:
            return
        self._listed = signature
        menu = self.recipient.menu()
        menu.clear()
        automatic = QAction("Automatically: whoever is working", menu)
        automatic.setCheckable(True)
        automatic.setChecked(self._manual is None)
        automatic.setData(None)
        automatic.triggered.connect(lambda _checked=False: self._pick(None))
        menu.addAction(automatic)
        for title, wanted in (("Working now", WORKING), ("Not started", ("pending",)),
                              ("Later", ("run",)), ("", ("gone",))):
            rows = [(key, label, state) for key, label, state in listed
                    if state in wanted]
            if not rows:
                continue
            if title:
                menu.addSection(title)
            else:
                menu.addSeparator()
            for key, label, state in rows:
                action = QAction(_dot(state), label, menu)
                action.setData(key)
                action.setCheckable(True)
                action.setChecked(self._manual == key)
                action.setEnabled(state != "gone")
                offer = (self._targets.get(key) or ("", {}, ""))[1]
                action.setToolTip(offer.get("when") or "")
                action.triggered.connect(lambda _checked=False, key=key: self._pick(key))
                menu.addAction(action)

    def _key_of(self, request):
        return target_key(request.get("scope"), request.get("step", ""),
                          request.get("execution"))

    def _label_of(self, key):
        if key is None:
            return ""
        found = self._targets.get(key)
        return found[0] if found else recipient_label(*key, labels=self._labels)

    # -- the draft -------------------------------------------------------------
    def _typed(self):
        draft = self.text.toPlainText()
        if draft.strip() and self._manual is None and self._locked is None:
            self._locked = self._automatic()
        elif not draft.strip():
            self._locked = None
        if not self._pending:
            self._notice, self._notice_bad = "", False
        self._fit()
        self._list()
        self._refresh()

    def _fit(self):
        """One line to begin with, growing to :data:`MAX_LINES` as it fills."""
        layout = self.text.document().documentLayout()
        lines = max(1, min(MAX_LINES, int(round(layout.documentSize().height()))))
        spacing = self.text.fontMetrics().lineSpacing()
        margin = self.text.document().documentMargin()
        # The contents margins, not the frame width: under the stylesheet the
        # two both carry its padding, and adding both made one line two tall.
        edges = self.text.contentsMargins()
        self.text.setFixedHeight(int(lines * spacing + 2 * margin + edges.top()
                                     + edges.bottom() + 2))

    def eventFilter(self, watched, event):     # noqa: N802 - Qt's own spelling
        """Enter sends; Shift+Enter is left to the box, which starts a line."""
        if (watched is self.text and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and not event.modifiers() & ~Qt.KeypadModifier):
            self.send()
            return True
        return super().eventFilter(watched, event)

    # -- saying where things stand ---------------------------------------------
    def _refresh(self):
        key = self.current_key()
        draft = self.text.toPlainText()
        target = self._targets.get(key) if key is not None else None
        writable = bool(self._live and self._targets)
        for widget in (self.to, self.recipient, self.text, self.when):
            widget.setVisible(writable)
        self.closed.setVisible(not writable)
        self.closed.setText(self._closed_text())

        working = self.working()
        asking = key is None and len(working) > 1
        if key is not None:
            state = "gone" if target is None else target[2]
            self.recipient.setText(self._label_of(key) if target else
                                   recipient_label(*key, labels=self._labels))
            self.recipient.setIcon(_dot(state))
        elif asking:
            self.recipient.setText("Choose one of %d working steps" % len(working))
            self.recipient.setIcon(QIcon())
        else:
            self.recipient.setText("Nobody to write to")
            self.recipient.setIcon(QIcon())
        self._set_property(self.recipient, "attention", "true" if asking else "false")
        self.recipient.setEnabled(writable and not self._pending)
        gone = key is not None and target is None
        automatic = self._automatic()
        self.mode.setVisible(writable and self._manual is None and key is not None
                             and not gone and not self._pending)
        if gone and automatic is not None and automatic != key:
            # The one-click way on, offered rather than taken: the person
            # decides whether what they wrote is for whoever is working now.
            self.automatic.setText("Send to %s instead" % self._label_of(automatic))
        else:
            self.automatic.setText("Back to automatic")
        self.automatic.setVisible(writable and not self._pending and (
            self._manual is not None or (gone and automatic is not None
                                         and automatic != key)))

        self.text.setReadOnly(bool(not self._live or self._pending))
        # How to send, where the eye already is: there is no button to say it,
        # and the line above is for when the recipient will read it.
        self.text.setPlaceholderText(
            ("A clarification for %s" % self._label_of(key) if target else
             "Write a clarification") + KEYS)

        if asking:
            when = ("%d steps are working at once. Choose which one should read "
                    "it - from the list, or by selecting it on the graph."
                    % len(working))
        elif key is not None and target is None:
            when = ("%s no longer takes messages, so it would not read this. "
                    "Choose another recipient." % recipient_label(*key, labels=self._labels))
        elif target is not None:
            when = target[1].get("when") or "At its next point."
            if key[0] == "run" and self._manual is None:
                when = "No step is working now. " + when
        else:
            when = ""
        self.when.setText(when)
        self._set_property(self.when, "role", "composernote" if (
            asking or (key is not None and target is None)) else "hint")
        self._set_property(self.when, "bad", "true" if (
            key is not None and target is None) else "false")

        if self._pending:
            note, bad = ("Waiting for the run to save it. Enter sends it again - "
                         "the same message, never a second one."), False
        elif len(draft) > MAX_TEXT:
            note, bad = "At most %d characters (%d written)." % (MAX_TEXT, len(draft)), True
        else:
            note, bad = self._notice, self._notice_bad
        self.note.setText(note)
        self.note.setVisible(writable and bool(note))
        self._set_property(self.note, "bad", "true" if bad else "false")

    def _closed_text(self):
        if not self._run_id:
            return "Clarifications can be sent to a cycle while it runs."
        if not self._live:
            return ("This run is over. Its messages are kept in the feed above; "
                    "nothing more can be sent to it.")
        return "No step still to come in this run takes clarifications."

    @staticmethod
    def _set_property(widget, name, value):
        """Set a property the stylesheet reads, and make it look again."""
        if widget.property(name) == value:
            return
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # -- sending ---------------------------------------------------------------
    def can_send(self):
        """Whether Enter would send something now."""
        key = self.current_key()
        draft = self.text.toPlainText()
        return bool(self._live and (self._pending or (
            key is not None and key in self._targets and draft.strip()
            and len(draft) <= MAX_TEXT)))

    def send(self):
        """Send the draft - or, while it is on its way, the same message again."""
        if not self.can_send():
            return False
        if self._pending is None:
            scope, step, execution = self.current_key()
            self._pending = {"command": "cycle.message", "id": uuid.uuid4().hex,
                             "run_id": self._run_id, "scope": scope, "step": step,
                             "execution": execution, "kind": "text",
                             "payload": {"text": self.text.toPlainText()},
                             "offer": self._offers.get("version", 0), "source": "gui",
                             "who": _who()}
        self._notice, self._notice_bad = "", False
        self._refresh()
        self.submitted.emit(copy.deepcopy(self._pending))
        return True


class ClarifyPopup(QFrame):
    """The composer over whatever window is in front - see :data:`SHORTCUT`.

    A popup rather than a trip to the Cycles page: the person is usually
    reading something else - the output, a log, another page - when they
    notice the work heading the wrong way, and the useful thing is to say so
    there. It holds its own draft, so closing it (Esc, or a click elsewhere)
    loses nothing; it closes by itself once the run has saved the message, and
    stays open with the reason when the run refuses it.
    """

    submitted = Signal(dict)
    #: The run took a message sent from here; what to say about it.
    sent = Signal(str)

    #: How wide it is, and how far below the top of the window it opens.
    WIDTH = 760
    DROP = 96

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setProperty("role", "clarifypopup")
        self.setFixedWidth(self.WIDTH)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QFrame()
        head.setProperty("role", "clarifyhead")
        row = QHBoxLayout(head)
        row.setContentsMargins(12, 7, 12, 7)
        row.setSpacing(8)
        row.addWidget(widgets.kicker("Clarify the running cycle"))
        self.run = widgets.ElidedLabel(mode=Qt.ElideRight)
        self.run.setProperty("role", "hint")
        row.addWidget(self.run, 1)
        closes = QLabel("Enter sends  Esc closes")
        closes.setProperty("role", "keys")
        row.addWidget(closes)
        outer.addWidget(head)
        self.composer = InteractionComposer()
        outer.addWidget(self.composer)
        self.composer.submitted.connect(self.submitted.emit)
        self.composer.settled.connect(self._settled)

    def set_context(self, cycle, live, labels=None, selected=""):
        cycle = cycle or {}
        self.run.setText(" - ".join(part for part in (cycle.get("name") or cycle.get("cycle"),
                                                       cycle.get("run_id")) if part))
        self.composer.set_context(cycle, live, labels, selected=selected)

    def open_over(self, window=None):
        """Show it near the top of ``window``, centred, with the cursor in it."""
        window = window or QApplication.activeWindow()
        self.adjustSize()
        if window is not None:
            frame = window.frameGeometry()
            self.move(frame.x() + max(0, (frame.width() - self.width()) // 2),
                      frame.y() + min(self.DROP, max(0, frame.height() // 5)))
        self.show()
        self.raise_()
        self.activateWindow()
        self.composer.text.setFocus()

    def keyPressEvent(self, event):          # noqa: N802 - Qt's own spelling
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def _settled(self, taken, said):
        if taken and self.isVisible():
            self.hide()
            self.sent.emit(said)
