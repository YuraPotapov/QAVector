"""A patch read file by file: what changed in a folder while an agent worked.

The core keeps what a turn changed as text (``integrations/agents/changes.py``)
and hands it over with a list of the files in it; this lays the two out. One
section per file, folded shut - the list of files is the first thing a person
wants to see, the lines of one of them the second - with the patch of a
section laid out only when it is opened: a conversation that rewrote a hundred
files is a hundred lines here until somebody looks into one.

It never says who made a change. What it shows is the folder's difference over
the turn, which is the agent's work plus anything somebody else did there
meanwhile, and it says so.
"""

from PySide6.QtCore import QEvent
from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (QDialog, QComboBox, QFrame, QLabel, QPlainTextEdit,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from . import theme, widgets

#: What a reader is told about where the changes come from.
WHOSE = ("Changes in the folder while the agent worked - anything somebody else "
         "changed there at the same time shows too.")

#: How tall one file's patch grows before it scrolls inside its section.
MAX_PATCH_HEIGHT = 520

STATUS_WORDS = {"added": "added", "modified": "", "deleted": "deleted",
                "renamed": "renamed", "copied": "copied", "written": "written"}


def split(patch):
    """``{path: its part of the patch}`` - each file's own ``diff --git`` section."""
    found, lines = {}, []
    for line in (patch or "").splitlines(True):
        if line.startswith("diff --git "):
            if lines:
                found[_path_of(lines)] = "".join(lines)
            lines = [line]
        elif lines:
            lines.append(line)
    if lines:
        found[_path_of(lines)] = "".join(lines)
    return found


def _path_of(lines):
    """The path a section is about, as the file list names it: where it is
    now, or where it was for a file deleted."""
    gone = ""
    for line in lines[1:]:
        if line.startswith("+++ b/"):
            return line[6:].rstrip("\n")
        if line.startswith("--- a/"):
            gone = line[6:].rstrip("\n")
        if line.startswith("rename to "):
            return line[10:].rstrip("\n")
        if line.startswith("@@"):
            break
    if gone:
        return gone
    header = lines[0][len("diff --git a/"):].rstrip("\n")
    return header[:(len(header) - 3) // 2]


def counts_text(added, removed):
    return "+%d -%d" % (added or 0, removed or 0)


def summary_text(found):
    """``3 files changed · +12 -4``, or that nothing did."""
    files = found.get("files") or []
    if not files:
        return "No files changed."
    return "%d file%s changed · %s" % (len(files), "" if len(files) == 1 else "s",
                                       counts_text(found.get("added"), found.get("removed")))


class DiffHighlighter(QSyntaxHighlighter):
    """Added lines on green, removed on red, the rest of a patch muted."""

    META = ("diff --git", "index ", "--- ", "+++ ", "new file", "deleted file",
            "similarity", "rename ", "old mode", "new mode", "Binary files")

    def __init__(self, document):
        super().__init__(document)
        self.restyle()

    def restyle(self):
        def made(ink, ground=None, bold=False):
            found = QTextCharFormat()
            found.setForeground(QColor(ink))
            if ground:
                found.setBackground(QColor(ground))
            if bold:
                found.setFontWeight(600)
            return found
        self.added = made(theme.DIFF_ADD_INK, theme.DIFF_ADD_BG)
        self.removed = made(theme.DIFF_DEL_INK, theme.DIFF_DEL_BG)
        self.hunk = made(theme.NEUTRAL[600], bold=True)
        self.meta = made(theme.NEUTRAL[500])
        self.rehighlight()

    def highlightBlock(self, text):             # noqa: N802 - Qt's own spelling
        if text.startswith(self.META):
            self.setFormat(0, len(text), self.meta)
        elif text.startswith("@@"):
            self.setFormat(0, len(text), self.hunk)
        elif text.startswith("+"):
            self.setFormat(0, len(text), self.added)
        elif text.startswith("-"):
            self.setFormat(0, len(text), self.removed)


class PatchText(QPlainTextEdit):
    """One file's patch: read-only, monospaced, as tall as it is up to a cap."""

    def __init__(self, text):
        super().__init__()
        self.setProperty("role", "diff")
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setPlainText(text.rstrip("\n"))      # no empty line under the last
        self.highlighter = DiffHighlighter(self.document())
        lines = max(1, self.document().blockCount())
        height = lines * self.fontMetrics().lineSpacing() + 16
        self.setFixedHeight(min(MAX_PATCH_HEIGHT, height))

    def changeEvent(self, event):               # noqa: N802 - Qt's own spelling
        super().changeEvent(event)
        # The theme changed: the colours are the highlighter's, not the
        # stylesheet's, and are made again in the new ones.
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange) and hasattr(
                self, "highlighter"):
            self.highlighter.restyle()


class FileSection(widgets.Disclosure):
    """One file: its path, what became of it and its counts on the title's
    line; its patch when opened."""

    def __init__(self, entry, patch):
        path = entry.get("path", "")
        title = path if not entry.get("from") else "%s (from %s)" % (path, entry["from"])
        super().__init__(title, expanded=False)
        self.entry, self.patch, self.text = entry, patch, None
        word = STATUS_WORDS.get(entry.get("status"), "")
        if word:
            status = QLabel(word)
            status.setProperty("role", "muted")
            self.add_to_header(status)
        if not entry.get("binary") and entry.get("status") != "written":
            added = QLabel("+%d" % (entry.get("added") or 0))
            added.setProperty("role", "diffadded")
            removed = QLabel("-%d" % (entry.get("removed") or 0))
            removed.setProperty("role", "diffremoved")
            self.add_to_header(added)
            self.add_to_header(removed)
        self.toggled.connect(self._opened)

    def _opened(self, opened):
        if not opened or self.text is not None:
            return
        if self.entry.get("status") == "written":
            said = ("Not compared: this part of the folder is too large to keep. "
                    "The agent wrote this file.")
        elif self.entry.get("binary"):
            said = "A binary file: what changed in it is not shown."
        elif not self.patch:
            said = "Its lines are past the length of what is kept."
        else:
            said = ""
        if said:
            self.text = QLabel(said)
            self.text.setProperty("role", "hint")
            self.text.setWordWrap(True)
        else:
            self.text = PatchText(self.patch)
        self.body().addWidget(self.text)


class DiffView(QWidget):
    """What changed: a line of totals, what was not kept, and a section per
    file. ``set_changes`` takes what the core's ``changes`` command answers.

    ``scrolling=False`` lays the files out at their own height, for a view
    inside a list that scrolls as a whole - several conversations' changes on
    one page."""

    def __init__(self, parent=None, scrolling=True):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self.summary = QLabel("")
        self.summary.setProperty("role", "chattitle")
        self.expand = QPushButton("Expand all")
        self.expand.setProperty("variant", "ghost")
        self.expand.clicked.connect(lambda: self.set_all(True))
        self.collapse = QPushButton("Collapse all")
        self.collapse.setProperty("variant", "ghost")
        self.collapse.clicked.connect(lambda: self.set_all(False))
        head = widgets.row(self.summary, None, self.expand, self.collapse)
        column.addWidget(head)
        self.note = QLabel(WHOSE)
        self.note.setProperty("role", "muted")
        self.note.setWordWrap(True)
        column.addWidget(self.note)
        self._body = QWidget()
        self._files = QVBoxLayout(self._body)
        self._files.setContentsMargins(0, 0, 0, 0)
        self._files.setSpacing(2)
        if scrolling:
            self._files.addStretch(1)
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QFrame.NoFrame)
            self.scroll.setWidget(self._body)
            column.addWidget(self.scroll, 1)
        else:
            self.scroll = None
            column.addWidget(self._body)
        self.sections = []
        self.set_changes({})

    def set_changes(self, found):
        found = found or {}
        for section in self.sections:
            self._files.removeWidget(section)
            section.hide()                  # not a window of its own when shown late
            section.setParent(None)
            section.deleteLater()
        self.sections = []
        self.summary.setText(summary_text(found))
        notes = [WHOSE]
        if found.get("unkept"):
            where = ", ".join("the folder itself" if one == "." else one
                              for one in found["unkept"])
            notes.append("Too large to compare every turn: %s - only the files the "
                         "agent wrote there are listed." % where)
        if found.get("truncated"):
            notes.append("The patch is longer than what is kept: the last files' lines "
                         "are left out.")
        if found.get("error"):
            notes.append(str(found["error"]))
        self.note.setText("\n".join(notes))
        parts = split(found.get("patch") or "")
        for entry in found.get("files") or []:
            section = FileSection(entry, parts.get(entry.get("path", ""), ""))
            self._files.insertWidget(len(self.sections), section)
            self.sections.append(section)
        for button in (self.expand, self.collapse):
            button.setVisible(bool(self.sections))

    def set_all(self, opened):
        for section in self.sections:
            section.set_expanded(opened)


class ChangesDialog(QDialog):
    """A conversation's changes, the whole of it or one turn's, in a window of
    its own beside the chat. One window, shown for whichever conversation asks:
    reading is the caller's - ``read(turn, done)`` asks for a turn's (None for
    the whole conversation) and calls ``done`` with what the core answered."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Changes")
        self.frame = widgets.dress(self)
        self.resize(900, 640)
        self._read = None
        self._serial = 0
        column = QVBoxLayout(self)
        column.setSpacing(8)
        self.scope = widgets.capped(QComboBox(), 360)
        self.scope.currentIndexChanged.connect(lambda _index: self.load())
        column.addWidget(widgets.row(self.scope, None))
        self.view = DiffView()
        column.addWidget(self.view, 1)

    def show_for(self, title, turns, read, turn=None):
        """Show ``title``'s changes: ``turns`` are the turns that changed
        something, ``turn`` the one to open on - None for all of them."""
        self.setWindowTitle("Changes - %s" % title)
        self._read = read
        self.scope.blockSignals(True)
        self.scope.clear()
        self.scope.addItem("The whole conversation", None)
        for number in turns:
            self.scope.addItem("Turn %d" % number, number)
        index = self.scope.findData(turn)
        self.scope.setCurrentIndex(index if index >= 0 else 0)
        self.scope.blockSignals(False)
        self.load()
        self.show()
        self.raise_()
        self.activateWindow()

    def load(self):
        if self._read is None:
            return
        self._serial += 1
        serial = self._serial
        self.view.set_changes({})
        self.view.summary.setText("Reading...")

        def done(payload):
            if serial != self._serial:
                return
            if not payload.get("ok"):
                self.view.set_changes({"error": payload.get("error")
                                       or "What changed could not be read."})
                return
            self.view.set_changes(payload)

        self._read(self.scope.currentData(), done)
