"""Stepping a paused scenario from the Run page (engine/stepping.py).

The core does the stepping; this only shows where a window is and asks it to
move. Positions, as the core counts them: 0 is the page before the first
step, n the page after step n. ``last`` is how far the live page has gone and
``cursor`` the page being looked at - ``last``, unless somebody stepped back.

:class:`StepBar` is one window's controls - the same four the HUD in the page
has, with the same words, so a person moving between the two reads one thing.
:class:`CopyPane` shows the copy of the page at the cursor: the run's own copy,
made by the core, never the live page, which stays in the browser as the run
left it.
"""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from .. import icons, widgets

#: The actions, the core's name for each, and the icon each is drawn with.
ACTIONS = (("back", "back"), ("pause", "pause"), ("step", "step-one"),
           ("forward", "forward"))


def describe(step, total=0):
    """``(headline, the line under it)`` for a window's stepping state."""
    if not step.get("paused"):
        return "Running", "Pause stops it after the step it is on"
    cursor, last = step.get("cursor") or 0, step.get("last") or 0
    of = " of %d" % total if total else ""
    if cursor < last:
        return (("The page after step %d" % cursor) if cursor else
                "The page before step 1",
                "Continue runs step %d again, on the live page" % (cursor + 1))
    if step.get("reason") == "failure":
        return ("Step %d%s failed" % (last, of),
                "Go back to run steps again; continue ends the scenario")
    head = ("Paused after step %d%s" % (last, of)) if last else "Paused before step 1%s" % of
    return head, "At a breakpoint" if step.get("reason") == "breakpoint" else ""


class StepBar(QWidget):
    """One window's stepping controls: back, pause or continue, one step, forward.

    ``asked`` carries a command for the core - ``{"command": "back"}`` - and
    nothing changes here until the core says it did.
    """

    asked = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "stepbar")
        line = QHBoxLayout(self)
        line.setContentsMargins(10, 6, 14, 6)
        line.setSpacing(2)
        self.buttons = {}
        for action, _icon in ACTIONS:
            button = QToolButton()
            button.setProperty("variant", "icon")
            button.setToolButtonStyle(Qt.ToolButtonIconOnly)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, action=action: self._clicked(action))
            line.addWidget(button)
            self.buttons[action] = button
        line.addSpacing(10)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.head = QLabel("")
        self.head.setProperty("role", "stephead")
        self.sub = QLabel("")
        self.sub.setProperty("role", "hint")
        self.sub.setWordWrap(True)
        text.addWidget(self.head)
        text.addWidget(self.sub)
        line.addLayout(text, 1)
        self._step = {}
        self._keys = {}
        self.show_state({}, 0, {})

    def _clicked(self, action):
        if action == "pause":
            action = "continue" if self._step.get("paused") else "pause"
        self.asked.emit({"command": action})

    def show_state(self, step, total, keys):
        """Paint ``step`` (RunState's session["step"]); ``keys`` is the keymap's
        text for each action, for the tooltips."""
        self._step, self._keys = dict(step or {}), dict(keys or {})
        paused = bool(self._step.get("paused"))
        cursor, last = self._step.get("cursor") or 0, self._step.get("last") or 0
        looking = paused and cursor < last
        state = ("failure" if paused and self._step.get("reason") == "failure"
                 else "paused" if paused else "running")
        if self.property("state") != state:
            self.setProperty("state", state)
            self.style().unpolish(self)
            self.style().polish(self)

        def keyed(text, action):
            key = self._keys.get(action)
            return "%s (%s)" % (text, key) if key else text

        tips = {
            "back": keyed("Back: the page one step earlier", "back"),
            "pause": keyed("Continue" if paused else "Pause after this step", "pause"),
            "step": keyed("Run one step, then pause" if paused else "Pause after this step",
                          "step"),
            "forward": keyed("Forward: the page one step later" if looking else
                             "Run one step", "forward"),
        }
        for action, icon in ACTIONS:
            button = self.buttons[action]
            name = ("run" if paused else "pause") if action == "pause" else icon
            icons.button(button, name)
            button.setToolTip(tips[action])
            button.setAccessibleName(tips[action].split(" (")[0])
        self.buttons["back"].setEnabled(not paused or cursor > 0)
        self.buttons["forward"].setEnabled(paused)
        head, sub = describe(self._step, total)
        self.head.setText(head)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))


class CopyPane(QWidget):
    """The copy of the page a paused window's cursor is on.

    Empty - and saying why - on the live page, or for a step with no copy;
    the copy itself, read from the file the core wrote, otherwise. The view is
    a web view, made the first time there is something to show in it.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(24, 10, 24, 12)
        column.setSpacing(6)
        self.title = QLabel("")
        self.title.setProperty("role", "stephead")
        self.meta = widgets.mono("")
        column.addWidget(widgets.row(self.title, self.meta, None))
        self.stack = QStackedWidget()
        self.empty = widgets.empty_zone("", "")
        self._empty_title, self._empty_text = self.empty.findChildren(QLabel)[:2]
        self.stack.addWidget(self.empty)
        # A hairline round the copy: it is a page of the app under test, with
        # its own colours, and without one it ran straight into this one.
        self.frame = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.frame.layout().addWidget(self.stack)
        column.addWidget(self.frame, 1)
        self.preview = None
        self.skeleton = None
        self.shown = None              # the file on show, so it is not read again

    def _view(self):
        if self.preview is None:
            # QtWebEngine, only once a copy is to be shown: a run that is never
            # paused never pays for starting it.
            from ..htmlpreview import HtmlPreview

            self.preview = HtmlPreview()
            self.preview.setAccessibleName("Copy of the page")
            self.stack.addWidget(self.preview)
            self.skeleton = widgets.Skeleton(self.preview, "lines")
            self.preview.loadFinished.connect(lambda _ok: self.skeleton.stop())
        return self.preview

    def show_step(self, session, step):
        """``session`` is RunState's session dict; its ``step`` says what to show."""
        step = step or {}
        cursor, last = step.get("cursor") or 0, step.get("last") or 0
        name = session.get("name") or ""
        scenario = session.get("scenario") or ""
        self.meta.setText(" · ".join(part for part in (name, scenario) if part))
        if cursor >= last:
            self.title.setText("The live page")
            self._say("The page is in the browser window",
                      "Back shows the page as it was after an earlier step - a copy, "
                      "laid over the live page in the window too.")
            return
        self.title.setText(("The page after step %d" % cursor) if cursor else
                           "The page before step 1")
        view = step.get("view")
        if not view or not os.path.exists(view):
            self._say("No copy of this step",
                      "Keep live history was off for this run, or the page could not be "
                      "copied at that step. Continue still runs step %d again."
                      % (cursor + 1))
            return
        preview = self._view()
        self.stack.setCurrentWidget(preview)
        if view != self.shown:
            self.shown = view
            self.skeleton.start()
            preview.load_file(view, os.path.dirname(view))

    def _say(self, title, text):
        self._empty_title.setText(title)
        self._empty_text.setText(text)
        self.stack.setCurrentWidget(self.empty)
        self.shown = None
