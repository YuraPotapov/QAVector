"""The variables of a launch address, in a small panel under it.

Opened by clicking a value a variable put into the address (the blue part), by
the Variables button, or by "Define port..." when the address names a variable
nobody has defined. Each row is one variable: its type, the value this run
gives it, where that value came from, what it would be without it, and Reset.
A value typed here is this run's; Save keeps it with the configuration, and
"Make default" makes it the environment's own, so every configuration of this
environment gets it. A port is a number in a box here and nowhere else - the address
itself only shows it.

The panel draws rows and reports what was asked; the page decides what each
answer changes (see LaunchSessionsPage._variable_*).
"""

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)

from .. import launchvars, widgets

#: Where a newly defined variable's value goes: (label, key).
LEVELS = (("This run", "run"), ("Environment default", "environment"),
          ("Project default", "project"))


class VariablesPanel(widgets.FloatingPanel):
    """See the module docstring."""

    #: (name, value): this run's value; None asks for Reset.
    value_changed = Signal(str, object)
    #: (name, value): make it the environment's own value.
    environment_value = Signal(str, object)
    #: (name, type, value, level key) for a variable nobody defined yet.
    define_requested = Signal(str, str, object, str)

    WIDTH = 640

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "clarifypopup")
        self.setFixedWidth(self.WIDTH)
        self._editors = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QFrame()
        head.setProperty("role", "clarifyhead")
        band = QHBoxLayout(head)
        band.setContentsMargins(12, 7, 12, 7)
        band.setSpacing(8)
        band.addWidget(widgets.kicker("Variables"))
        note = QLabel("Values for this run - Save keeps them with the configuration.")
        note.setProperty("role", "hint")
        band.addWidget(note, 1)
        keys = QLabel("Esc closes")
        keys.setProperty("role", "keys")
        band.addWidget(keys)
        outer.addWidget(head)

        self.body = QWidget(self)
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(12, 10, 12, 10)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(6)
        outer.addWidget(self.body)
        self.empty = QLabel("This address has no variables. Write one as {{name}} - "
                            "{{port}}, say - and give it a value here.", self.body)
        self.empty.setProperty("role", "hint")
        self.empty.setWordWrap(True)
        self.footnote = QLabel("", self)
        self.footnote.setProperty("role", "hint")
        self.footnote.setWordWrap(True)
        self.footnote.setContentsMargins(12, 0, 12, 10)
        outer.addWidget(self.footnote)

    # -- rows -----------------------------------------------------------------
    def set_rows(self, rows, can_set_environment=True):
        """Show ``rows``: one per variable, those of the address first.

        Each row is a dict: ``name``, ``defined`` and, for a defined one,
        ``type``, ``choices``, ``text`` (the value in force, "" when none),
        ``source``, ``inherited``, ``inherited_source``, ``resettable`` and
        ``problem``.
        """
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.empty:
                widget.hide()
                widget.deleteLater()
        self._editors = {}
        self.empty.setVisible(not rows)
        if not rows:
            self.grid.addWidget(self.empty, 0, 0, 1, 6)
        line = 0
        ports = False
        for row in rows:
            if row.get("defined"):
                line = self._defined_row(line, row, can_set_environment)
                ports = ports or row.get("type") == "port"
            else:
                line = self._undefined_row(line, row)
        self.footnote.setText(
            "Another port of the same environment opens the same profiles, signed "
            "in as before. Two ports of localhost share their cookies, so open "
            "them one at a time." if ports else "")
        self.footnote.setVisible(ports)
        self.adjustSize()

    def _defined_row(self, line, row, can_set_environment):
        name = row["name"]
        label = QLabel(name, self.body)
        label.setProperty("role", "mono")
        kind = QLabel(row.get("type", ""), self.body)
        kind.setProperty("role", "hint")
        editor = self._editor(row)
        source = widgets.Tag(row.get("source") or "no value", "neutral", self.body)
        inherited = QLabel(self._inherited_text(row), self.body)
        inherited.setProperty("role", "hint")
        reset = QPushButton("Reset", self.body)
        reset.setProperty("variant", "ghost")
        reset.setEnabled(bool(row.get("resettable")))
        reset.setToolTip("Go back to the value it inherits")
        reset.clicked.connect(lambda _c=False, n=name: self.value_changed.emit(n, None))
        keep = QPushButton("Make default", self.body)
        keep.setProperty("variant", "ghost")
        keep.setEnabled(can_set_environment and bool(row.get("text")))
        keep.setToolTip("Make it this environment's value: every configuration of "
                        "it gets this value unless it gives its own.")
        keep.clicked.connect(lambda _c=False, n=name: self.environment_value.emit(
            n, self._read(n)))
        for column, widget in enumerate((label, kind, editor, source, inherited)):
            self.grid.addWidget(widget, line, column)
        self.grid.addWidget(widgets.row(reset, keep, spacing=2), line, 5)
        line += 1
        if row.get("problem"):
            problem = QLabel(row["problem"], self.body)
            problem.setProperty("role", "error")
            problem.setWordWrap(True)
            self.grid.addWidget(problem, line, 2, 1, 4)
            line += 1
        return line

    def _undefined_row(self, line, row):
        """A name the address uses that nobody defined: define it here."""
        name = row["name"]
        label = QLabel(name, self.body)
        label.setProperty("role", "mono")
        kind = widgets.capped(QComboBox(self.body), 110)
        for choice in launchvars.TYPES:
            kind.addItem(choice)
        kind.setCurrentText("port" if name.lower() == "port" else "string")
        value = QLineEdit(self.body)
        value.setProperty("mono", True)
        value.setPlaceholderText("value")
        level = widgets.capped(QComboBox(self.body), 170)
        for text, key in LEVELS:
            level.addItem(text, key)
        define = QPushButton("Define", self.body)
        define.setProperty("variant", "primary")
        define.clicked.connect(lambda _c=False: self.define_requested.emit(
            name, kind.currentText(), value.text().strip(), level.currentData()))
        value.returnPressed.connect(define.click)
        self._editors[name] = value
        note = QLabel("not defined", self.body)
        note.setProperty("role", "hint")
        for column, widget in enumerate((label, kind, value, level, note)):
            self.grid.addWidget(widget, line, column)
        self.grid.addWidget(define, line, 5)
        return line + 1

    def _editor(self, row):
        name, kind = row["name"], row.get("type")
        text = row.get("text") or ""
        if kind == "boolean":
            editor = QCheckBox(self.body)
            editor.setChecked(text == "true")
            editor.toggled.connect(lambda on, n=name: self.value_changed.emit(n, on))
        elif kind == "string" and row.get("choices"):
            editor = widgets.capped(QComboBox(self.body), 180)
            for choice in row["choices"]:
                editor.addItem(choice)
            editor.setCurrentText(text)
            editor.currentTextChanged.connect(
                lambda value, n=name: self.value_changed.emit(n, value))
        else:
            editor = QLineEdit(text, self.body)
            editor.setProperty("mono", True)
            if kind == "port":
                editor.setValidator(QIntValidator(1, 65535, editor))
            elif kind == "integer":
                editor.setValidator(QIntValidator(editor))
            editor.setPlaceholderText("no value")
            editor.editingFinished.connect(
                lambda n=name, e=editor: self._edited(n, e))
        self._editors[name] = editor
        return editor

    def _edited(self, name, editor):
        if editor.isModified():
            editor.setModified(False)
            self.value_changed.emit(name, editor.text().strip())

    def _read(self, name):
        editor = self._editors.get(name)
        if isinstance(editor, QCheckBox):
            return editor.isChecked()
        if isinstance(editor, QComboBox):
            return editor.currentText()
        return editor.text().strip() if editor is not None else None

    @staticmethod
    def _inherited_text(row):
        if not row.get("inherited_source"):
            return ""
        return "over %s from %s" % (row.get("inherited") or '""', row["inherited_source"])

    # -- showing --------------------------------------------------------------
    def open_below(self, anchor, focus=""):
        """Show under ``anchor`` (the address box), on ``focus``'s value."""
        self.adjustSize()
        where = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        self.move(where)
        self.show()
        self.raise_()
        self.activateWindow()
        editor = self._editors.get(focus)
        if editor is not None:
            editor.setFocus()
            if isinstance(editor, QLineEdit):
                editor.selectAll()

    def editor(self, name):
        return self._editors.get(name)

    def keyPressEvent(self, event):          # noqa: N802 - Qt's own spelling
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
