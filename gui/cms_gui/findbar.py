"""Find in a reader: Ctrl+F over what is on show, as a browser finds in a page.

The application reads HTML in several places - a run's guide and a Jira task's
reading copy on the Artifacts page, a step's readable summary and a subject's
details on the Cycles page, an agent's review in the approval dialog - and none
of them could be searched. :class:`FindBar` is the one bar for all of them: it
sits over its reader, every match is lit and the current one more strongly,
and "3 of 12" stands beside the field.

Two kinds of reader, one bar:

* a QWebEngineView (:class:`WebTarget`): Chromium finds and lights the matches
  itself, and the count comes back in ``findTextFinished``;
* a QTextEdit, QTextBrowser or QPlainTextEdit (:class:`TextTarget`): the matches
  are found in the document and lit as extra selections.

:func:`install` gives a host one Ctrl+F - and F3 / Shift+F3 and Ctrl+G /
Ctrl+Shift+G - for whichever reader it is showing at that moment. One set per
host, so two readers on one page never make a key ambiguous: Qt fires neither
of two shortcuts that both want it.
"""

import functools

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
                               QToolButton)

from . import icons, theme, widgets
from .hotkeys import ActionSpec, alternate_name, bind_action

#: The most matches lit in a text reader. A long run's log can match a common
#: word tens of thousands of times; past this the count says so ("2000+").
TEXT_LIMIT = 2000
#: How long a reader's text has to stay still before an open search is run on
#: it again: a log being written changes many times a second.
REFRESH_MS = 250
#: What the count says when the reader on show has no text to search.
NOTHING = "Nothing to search here"


class _Target(QObject):
    """A reader as the bar sees it."""

    #: ``(current, total)`` - where the search stands, counting from 1. (0, 0)
    #: is no match; a total of None means "not known yet".
    found = Signal(int, int)
    #: What the reader shows changed under an open search.
    changed = Signal()

    limit = 0          # the most matches counted; 0 for no limit

    def __init__(self, reader):
        super().__init__(reader)
        self.reader = reader

    def search(self, text):
        """Find ``text`` afresh, from where the reader is."""

    def step(self, forward):
        """Go to the next match, or the previous one."""

    def refresh(self, text):
        """The reader changed: find ``text`` in what it shows now."""
        self.search(text)

    def clear(self):
        """Put out every light."""


class WebTarget(_Target):
    """A QWebEngineView: Chromium's own find, which lights every match."""

    def __init__(self, view):
        super().__init__(view)
        self._text = ""
        view.page().findTextFinished.connect(self._finished)
        view.loadFinished.connect(lambda _ok: self.changed.emit())

    def _find(self, text, backward=False):
        from PySide6.QtWebEngineCore import QWebEnginePage

        flags = (QWebEnginePage.FindFlag.FindBackward if backward
                 else QWebEnginePage.FindFlag(0))
        self.reader.page().findText(text, flags)

    def search(self, text):
        self._text = text
        self._find(text)
        if not text:
            self.found.emit(0, 0)

    def step(self, forward):
        if self._text:
            self._find(self._text, backward=not forward)

    def _finished(self, result):
        if self._text:
            self.found.emit(result.activeMatch(), result.numberOfMatches())

    def clear(self):
        self._text = ""
        self.reader.page().findText("")


class TextTarget(_Target):
    """A QTextEdit, QTextBrowser or QPlainTextEdit: matches as extra selections."""

    @property
    def limit(self):
        return TEXT_LIMIT

    def __init__(self, edit):
        super().__init__(edit)
        self._text = ""
        self._matches = []        # a QTextCursor per match, its text selected
        self._current = -1
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(REFRESH_MS)
        self._settle.timeout.connect(self.changed.emit)
        edit.document().contentsChanged.connect(self._settle.start)

    def matches(self):
        """Where the matches are, as (start, end) positions. For a test to read."""
        return [(cursor.selectionStart(), cursor.selectionEnd())
                for cursor in self._matches]

    def _collect(self, text):
        self._text = text
        self._matches = []
        if not text:
            return
        document = self.reader.document()
        cursor = QTextCursor(document)
        while len(self._matches) < TEXT_LIMIT:
            cursor = document.find(text, cursor)
            if cursor.isNull():
                break
            self._matches.append(QTextCursor(cursor))

    def _from_view(self):
        """The first match at or below the top of what is on screen.

        A search that starts at the top of the document yanks a reader who is
        halfway down back to the beginning; a browser starts where you are.
        """
        top = self.reader.cursorForPosition(QPoint(0, 0)).position()
        for index, cursor in enumerate(self._matches):
            if cursor.selectionStart() >= top:
                return index
        return 0 if self._matches else -1

    def search(self, text):
        self._collect(text)
        self._current = self._from_view()
        self._show(scroll=True)

    def step(self, forward):
        if not self._matches:
            self._show(scroll=False)
            return
        self._current = (self._current + (1 if forward else -1)) % len(self._matches)
        self._show(scroll=True)

    def refresh(self, text):
        """The text changed under the search: light it again where it stands."""
        current = self._current
        self._collect(text)
        self._current = min(max(current, 0), len(self._matches) - 1)
        self._show(scroll=False)

    def _show(self, scroll):
        every, now = QTextCharFormat(), QTextCharFormat()
        # Read when painted, not at import: dark mode rewrites the theme.
        every.setBackground(QColor(theme.WARN_TINT))
        now.setBackground(QColor(theme.SELECT_BG))
        now.setForeground(QColor(theme.SELECT_INK))
        lights = []
        for index, cursor in enumerate(self._matches):
            light = QTextEdit.ExtraSelection()
            light.cursor = cursor
            light.format = now if index == self._current else every
            lights.append(light)
        self.reader.setExtraSelections(lights)
        if scroll and 0 <= self._current < len(self._matches):
            place = QTextCursor(self._matches[self._current])
            place.setPosition(place.selectionStart())
            self.reader.setTextCursor(place)
            self.reader.ensureCursorVisible()
        self.found.emit(self._current + 1 if self._matches else 0, len(self._matches))

    def clear(self):
        self._text, self._matches, self._current = "", [], -1
        self.reader.setExtraSelections([])


def target_for(reader):
    """The one target a reader has, made the first time it is asked for."""
    target = getattr(reader, "_find_target", None)
    if target is None:
        target = (WebTarget(reader) if reader.inherits("QWebEngineView")
                  else TextTarget(reader))
        reader._find_target = target
    return target


def _tool(name, tip):
    button = icons.button(QToolButton(), name)
    button.setToolButtonStyle(Qt.ToolButtonIconOnly)
    button.setProperty("variant", "icon")
    button.setCursor(Qt.PointingHandCursor)
    button.setToolTip(tip)
    button.setAccessibleName(tip.split(" (")[0])
    return button


class FindBar(QFrame):
    """The field, where the search stands, and the way to the next match."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "findbar")
        self._target = None
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 4, 6, 4)
        row.setSpacing(6)
        row.addWidget(widgets.kicker("Find"))
        self.field = QLineEdit()
        self.field.setPlaceholderText("Find on this page")
        self.field.installEventFilter(self)
        self.field.textChanged.connect(self._typed)
        row.addWidget(self.field, 1)
        self.count = QLabel("")
        self.count.setProperty("role", "keys")
        row.addWidget(self.count)
        self.previous = _tool("fold", "Previous match (Shift+Enter, Shift+F3)")
        self.previous.clicked.connect(lambda: self.step(False))
        self.next = _tool("unfold", "Next match (Enter, F3)")
        self.next.clicked.connect(lambda: self.step(True))
        self.close_button = _tool("close", "Close (Esc)")
        self.close_button.clicked.connect(self.close_bar)
        for button in (self.previous, self.next, self.close_button):
            row.addWidget(button)
        self.hide()

    # -- what it searches ------------------------------------------------------
    def target(self):
        return self._target

    def retarget(self, target):
        """The host shows another reader - or none. An open search follows it."""
        if target is self._target:
            self._enable()
            return
        if self._target is not None:
            self._target.found.disconnect(self._found)
            self._target.changed.disconnect(self._changed)
            self._target.clear()
        self._target = target
        if target is not None:
            target.found.connect(self._found)
            target.changed.connect(self._changed)
        self._enable()
        if self.isVisible() and target is not None and self.field.text():
            target.search(self.field.text())
        elif self.isVisible():
            self._say()

    def open(self, target):
        """Ctrl+F: show the bar over ``target``, the field ready to type in."""
        self.retarget(target)
        self.show()
        self.field.setFocus(Qt.ShortcutFocusReason)
        self.field.selectAll()
        if target is not None and self.field.text():
            target.search(self.field.text())
        else:
            self._say()

    def step(self, forward, target=None):
        """F3, Shift+F3, Enter, Shift+Enter: the next match, or the previous."""
        if target is not None and (not self.isVisible() or target is not self._target):
            self.open(target)
            return
        if self._target is not None and self.field.text():
            self._target.step(forward)

    def close_bar(self):
        """Esc: the lights go out, the bar goes, the reader has the keys back."""
        reader = self._target.reader if self._target is not None else None
        if self._target is not None:
            self._target.clear()
        self.hide()
        if reader is not None:
            reader.setFocus(Qt.OtherFocusReason)

    # -- reacting --------------------------------------------------------------
    def _typed(self, text):
        if self._target is not None and text:
            self._target.search(text)          # it says where it stands
            return
        if self._target is not None:
            self._target.clear()               # an emptied field: lights out
        self._say()

    def _changed(self):
        if self.isVisible() and self._target is not None and self.field.text():
            self._target.refresh(self.field.text())

    def _enable(self):
        has = self._target is not None
        self.field.setEnabled(has)
        self.previous.setEnabled(has)
        self.next.setEnabled(has)

    def _found(self, current, total):
        if not self.field.text():
            self._say()
            return
        if not total:
            self._say("No matches", bad=True)
            return
        limit = self._target.limit if self._target is not None else 0
        self._say("%d of %d%s" % (current, total,
                                  "+" if limit and total >= limit else ""))

    def _say(self, text=None, bad=False):
        if text is None:
            text = NOTHING if self._target is None else ""
        self.count.setText(text)
        if self.count.property("bad") != ("true" if bad else "false"):
            self.count.setProperty("bad", "true" if bad else "false")
            self.count.style().unpolish(self.count)
            self.count.style().polish(self.count)

    def eventFilter(self, watched, event):     # noqa: N802 - Qt's own spelling
        """Enter goes on, Shift+Enter goes back, Esc puts the bar away."""
        if watched is self.field and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.step(not event.modifiers() & Qt.ShiftModifier)
                return True
            if event.key() == Qt.Key_Escape:
                self.close_bar()
                return True
        return super().eventFilter(watched, event)


def install(host, resolve):
    """Give ``host`` its Ctrl+F, F3 and Shift+F3 (and Ctrl+G / Ctrl+Shift+G).

    ``resolve()`` answers ``(bar, target)`` for what the host shows when the
    key is pressed - ``target`` None when it shows nothing to search - or None
    when the keys mean nothing there at all.
    """
    def find():
        answer = resolve()
        if answer is not None:
            bar, target = answer
            tips(bar)
            bar.open(target)

    def step(forward):
        answer = resolve()
        if answer is not None:
            bar, target = answer
            tips(bar)
            if target is not None:
                bar.step(forward, target)

    made = []
    groups = {}

    def tips(bar):
        for key, button, label, fixed in (
                ("previous", bar.previous, "Previous match", "Shift+Enter"),
                ("next", bar.next, "Next match", "Enter")):
            keys = [action.shortcut().toString(QKeySequence.NativeText)
                    for action in groups.get(key, ()) if not action.shortcut().isEmpty()]
            button.setToolTip("%s (%s)" % (label, ", ".join([fixed] + keys)))

    def refresh_tips():
        answer = resolve()
        if answer is not None:
            tips(answer[0])

    for key, name, keys, slot in (
            ("open", "Find in reader", QKeySequence.StandardKey.Find, find),
            ("next", "Next match", QKeySequence.StandardKey.FindNext, functools.partial(step, True)),
            ("previous", "Previous match", QKeySequence.StandardKey.FindPrevious,
             functools.partial(step, False))):
        groups[key] = []
        # Qt supplies platform-specific alternatives (for example F3 and
        # Ctrl+G). Each is its own assignable action: one combination per row.
        for index, sequence in enumerate(QKeySequence.keyBindings(keys)):
            suffix = "" if index == 0 else ".alternate_%d" % index
            action = bind_action(host, ActionSpec(
                "find." + key + suffix,
                alternate_name(name, index),
                "Find text in the active reader.", "Readers",
                sequence.toString(QKeySequence.PortableText), "component/reader"), slot)
            action.changed.connect(refresh_tips)
            groups[key].append(action)
            made.append(action)
    return made
