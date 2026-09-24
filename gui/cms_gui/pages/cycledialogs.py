"""Editing one step, and a cycle's own properties, in a window of their own.

These were a panel down the right-hand side of the Cycles page. A panel is the
wrong shape for this: a step has a dozen fields plus whatever its plugin
declares, and squeezing that into a column three hundred pixels wide meant every
field was cramped and the canvas - the thing the page is actually for - gave up a
third of its width permanently to something only wanted occasionally. A window
opens when there is something to edit and takes the room it needs.

**The step's own form is generated, never written.** A plugin declares its
settings as :class:`~cycle.registry.Field` entries and the core publishes them in
``--describe``; this reads that and builds a widget per ``kind``. So a plugin
added to the core is editable here with nothing added here, which is the same
bargain the Services page's runner dialog already strikes, and the reason the
metadata carries kinds at all.

Nothing here parses YAML or knows what any plugin does. The dialog is handed a
document, gives back a changed one, and the page sends it to the core - which is
the only thing that decides whether it may be written.
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QHeaderView, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
                               QTableWidget, QVBoxLayout, QWidget)

from .. import icons, theme, widgets

#: What a step carries regardless of plugin. Kept here rather than read off the
#: node payload so the order is the order somebody reads them in.
ON_FAILURE = ("stop", "continue")

#: How tall a generated multi-line box is. Enough for a command or a task
#: paragraph without the dialog becoming a page of text areas.
MULTILINE_HEIGHT = 92

#: What a variable can be. ``secret`` means the value is not in the cycle file
#: at all - the file says only that the variable is one, and the value lives in
#: the encrypted store the core keeps (see ``cycle/secrets.py``).
#:
#: ``path`` is a folder on this machine. Its value is ordinary and sits in the
#: file like any other - ``${vars.repo}`` reads the same string whatever it was
#: declared as - and saying so buys one thing: the row offers to *find* the
#: folder instead of leaving somebody to type it correctly, which is how a
#: cycle ends up pointed at a directory that is nearly right.
TEXT, SECRET, PATH = "text", "secret", "path"
VARIABLE_KINDS = (TEXT, PATH, SECRET)


class _Editor(QDialog):
    """What the two dialogs share: a scrolling body and a Save/Close footer."""

    def __init__(self, title, parent=None, writable=True):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.resize(620, 680)
        self.frame = widgets.dress(self)
        self._writable = bool(writable)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(10)

        self.heading = widgets.heading(title)
        outer.addWidget(self.heading)
        self.subtitle = widgets.mono("")
        self.subtitle.setWordWrap(True)
        outer.addWidget(self.subtitle)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 8, 8, 8)
        self.body_layout.setSpacing(8)
        area.setWidget(self.body)
        outer.addWidget(area, 1)

        self.note = widgets.mono("")
        self.note.setWordWrap(True)
        outer.addWidget(self.note)

        self.buttons = QDialogButtonBox()
        self.save_button = self.buttons.addButton("Save",
                                                  QDialogButtonBox.AcceptRole)
        self.save_button.setProperty("variant", "primary")
        self.buttons.addButton("Close", QDialogButtonBox.RejectRole)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        self.save_button.setEnabled(self._writable)
        if not self._writable:
            self.note.setText("This cycle ships with the application. "
                              "Duplicate it to change it.")

    def _save(self):
        raise NotImplementedError

    def _complain(self, problems):
        self.note.setText("Not saved:\n- %s"
                          % "\n- ".join(str(one) for one in problems))
        self.note.setStyleSheet("font-family: %s; font-size: 11px; color: %s;"
                                % (theme.MONO_CSS, theme.BAD))


class NodeDialog(_Editor):
    """One step: what it runs, when, and what its plugin takes.

    Opened by double-clicking a node. Everything on it is editable except what
    a run already did, which is shown at the bottom when there is a run to show.
    """

    def __init__(self, step, plugin, parent=None, writable=True, live=None,
                 ask_plugin=None):
        super().__init__(step.get("label") or step.get("id", "Step"), parent,
                         writable)
        self._step = dict(step)
        self._plugin = dict(plugin or {})
        self._ask_plugin = ask_plugin
        self._widgets = {}
        self.saved = None            # the changed step, once Save was pressed

        self.subtitle.setText("%s%s" % (
            self._plugin.get("id") or step.get("plugin", ""),
            " - " + self._plugin["summary"] if self._plugin.get("summary")
            else ""))

        self._add_step_fields()
        self._add_plugin_fields()
        self._add_actions()
        self._add_live(live)
        self.body_layout.addStretch(1)

    # -- the step's own -------------------------------------------------------
    def _add_step_fields(self):
        self.body_layout.addWidget(widgets.kicker("Step"))
        self._line("id", "Id", self._step.get("id", ""),
                   "Letters, digits, _ and -. Other steps name it in needs.")
        self._line("label", "Label", self._step.get("label", ""),
                   "What the node is called on the canvas. Blank uses the id.")
        self._line("needs", "Depends on",
                   ", ".join(self._step.get("needs") or []),
                   "Step ids, comma separated. They must succeed first.")
        self._line("if", "Runs if", self._step.get("if", ""),
                   "${steps.x.status} == 'failed', or blank for always. A step "
                   "with a condition must also depend on what it reads.")
        self._line("timeout", "Timeout",
                   _text(self._step.get("timeout")),
                   "Seconds for the whole step, retries included. Blank means "
                   "no deadline.")
        retry = self._step.get("retry") or {}
        self._line("retry.attempts", "Tries", _text(retry.get("attempts")),
                   "Total tries, not extra ones. Blank means one.")
        self._line("retry.delay", "Delay between tries",
                   _text(retry.get("delay")), "Seconds.")
        self._choice("on_failure", "On failure",
                     self._step.get("on_failure") or "stop", ON_FAILURE,
                     "stop cancels the rest of the run; continue lets "
                     "unrelated branches carry on.")
        self._check("disabled", "Disabled", bool(self._step.get("disabled")),
                    "A disabled step is skipped, and so is anything that needs "
                    "it.")

    # -- whatever the plugin declares ----------------------------------------
    def _add_plugin_fields(self):
        fields = self._plugin.get("inputs") or []
        if not fields:
            return
        settings = self._step.get("with") or {}
        self.body_layout.addWidget(widgets.kicker("Settings"))
        for spec in fields:
            key = "with." + spec.get("key", "")
            value = settings.get(spec.get("key"))
            if value is None:
                value = spec.get("default")
            label = spec.get("label") or spec.get("key", "")
            if spec.get("required"):
                label += " *"
            self._by_kind(key, label, spec, value)

    def _by_kind(self, key, label, spec, value):
        """One widget for one declared field. The kind decides, not the key."""
        kind = spec.get("kind", "text")
        hint = spec.get("hint", "")
        if kind == "check":
            self._check(key, label, bool(value), hint)
        elif kind == "choice":
            options = list(spec.get("options") or [])
            # A field that declares a blank default means "unset" is one of the
            # answers - so the list has to be able to say it, or opening the
            # dialog and pressing Save would pick the first option for a
            # setting nobody chose.
            if spec.get("default") == "" and "" not in options:
                options.insert(0, "")
            self._choice(key, label, _text(value), options, hint)
        elif kind == "multiline":
            self._multiline(key, label, _text(value), hint)
        elif kind == "env":
            self._multiline(key, label, _pairs(value),
                            (hint + " " if hint else "")
                            + "One per line, name = value.")
        elif kind == "args":
            self._line(key, label,
                       " ".join(value) if isinstance(value, list) else _text(value),
                       (hint + " " if hint else "") + "Split the way a shell "
                       "would split it.")
        elif kind in ("file", "dir"):
            self._path(key, label, _text(value), hint, kind)
        else:
            self._line(key, label, _text(value), hint)

    # -- what the plugin can be asked ----------------------------------------
    def _add_actions(self):
        actions = self._plugin.get("actions") or []
        if not actions or self._ask_plugin is None:
            return
        self.body_layout.addWidget(widgets.kicker("Setup"))
        for entry in actions:
            if entry.get("kind") == "status":
                self._status_action(entry)
            else:
                self._command_action(entry)

    def _status_action(self, entry):
        line = widgets.mono("not checked")
        line.setWordWrap(True)
        button = QPushButton("Check")
        button.setProperty("variant", "ghost")

        def check():
            answer = self._ask_plugin(entry.get("key", ""), self._settings())
            line.setText(str(answer.get("summary") or ""))
            line.setStyleSheet(
                "font-family: %s; font-size: 11px; color: %s;"
                % (theme.MONO_CSS, theme.OK if answer.get("ok") else theme.WARN))
            line.setToolTip(str(answer.get("detail") or ""))

        button.clicked.connect(check)
        self.body_layout.addWidget(widgets.field(
            entry.get("label") or entry.get("key", ""),
            widgets.row(line, None, button), entry.get("hint", "")))

    def _command_action(self, entry):
        note = widgets.mono("")
        note.setWordWrap(True)
        button = QPushButton(entry.get("label") or entry.get("key", ""))

        def run():
            import subprocess

            answer = self._ask_plugin(entry.get("key", ""), self._settings())
            argv = answer.get("argv") or []
            if not answer.get("ok") or not argv:
                note.setText(str(answer.get("summary") or "cannot run this"))
                note.setToolTip(str(answer.get("detail") or ""))
                return
            try:
                subprocess.Popen(argv, start_new_session=(os.name != "nt"))
            except OSError as exc:
                note.setText("could not start it: %s" % exc)
                return
            note.setText(str(answer.get("detail") or answer.get("summary") or ""))

        button.clicked.connect(run)
        self.body_layout.addWidget(widgets.field("", widgets.row(button, None),
                                                 entry.get("hint", "")))
        self.body_layout.addWidget(note)

    # -- what the run did -----------------------------------------------------
    def _add_live(self, live):
        if not live or live.get("status") in ("", "pending"):
            return
        self.body_layout.addWidget(widgets.kicker("This run"))
        self.body_layout.addWidget(widgets.field("Status",
                                                 widgets.mono(live["status"])))
        if live.get("duration_ms"):
            self.body_layout.addWidget(widgets.field(
                "Took", widgets.mono(_duration(live["duration_ms"]))))
        if int(live.get("attempt") or 0) > 1:
            self.body_layout.addWidget(widgets.field(
                "Tries", widgets.mono(str(live["attempt"]))))
        if live.get("message"):
            note = widgets.mono(live["message"])
            note.setWordWrap(True)
            self.body_layout.addWidget(widgets.field("Result", note))
        outputs = live.get("outputs") or {}
        for key, value in sorted(outputs.items()):
            note = widgets.mono(_output_value(value))
            note.setWordWrap(True)
            self.body_layout.addWidget(widgets.field(key, note))
        if any(isinstance(value, (list, dict)) for value in outputs.values()):
            # Named but not dumped. A list of issues printed as a Python repr
            # is the wall of text the Stages tab exists to replace, and the
            # name alone is what a following step needs in order to reference
            # it - ${steps.<id>.outputs.<key>}.
            self.body_layout.addWidget(widgets.lede(
                "What the step actually found is laid out in the Stages tab. "
                "The names above are what a later step can reference."))

    # -- building the widgets -------------------------------------------------
    def _line(self, key, label, value, hint=""):
        widget = QLineEdit(value)
        widget.setEnabled(self._writable)
        self._widgets[key] = widget
        self.body_layout.addWidget(widgets.field(label, widget, hint))

    def _multiline(self, key, label, value, hint=""):
        widget = QPlainTextEdit(value)
        widget.setFixedHeight(MULTILINE_HEIGHT)
        widget.setStyleSheet("font-family: %s; font-size: 11px;"
                             % theme.MONO_CSS)
        widget.setEnabled(self._writable)
        self._widgets[key] = widget
        self.body_layout.addWidget(widgets.field(label, widget, hint))

    def _check(self, key, label, value, hint=""):
        widget = QCheckBox()
        widget.setChecked(bool(value))
        widget.setEnabled(self._writable)
        self._widgets[key] = widget
        self.body_layout.addWidget(widgets.field(label, widget, hint))

    def _choice(self, key, label, value, options, hint=""):
        widget = QComboBox()
        for option in options:
            # An empty option is a real answer - "leave it to whatever the tool
            # does by itself" - and a blank row in a list would read as a bug.
            widget.addItem("(default)" if option == "" else str(option), option)
        index = widget.findData(value)
        if index < 0 and value:
            # A value this build does not offer is kept rather than reset,
            # which would lose it on the next save.
            widget.addItem("%s (not offered)" % value, value)
            index = widget.count() - 1
        widget.setCurrentIndex(max(0, index))
        widget.setEnabled(self._writable)
        self._widgets[key] = widget
        self.body_layout.addWidget(widgets.field(label, widget, hint))

    def _path(self, key, label, value, hint, kind):
        widget = QLineEdit(value)
        widget.setEnabled(self._writable)
        browse = QPushButton("Browse")
        browse.setProperty("variant", "ghost")
        browse.setEnabled(self._writable)

        def pick():
            if kind == "dir":
                chosen = QFileDialog.getExistingDirectory(
                    self, label, widget.text() or os.path.expanduser("~"))
            else:
                chosen, _filter = QFileDialog.getOpenFileName(
                    self, label, widget.text() or os.path.expanduser("~"))
            if chosen:
                widget.setText(chosen)

        browse.clicked.connect(pick)
        self._widgets[key] = widget
        self.body_layout.addWidget(widgets.field(
            label, widgets.row(widget, browse), hint))

    # -- reading them back ----------------------------------------------------
    def _value(self, key):
        widget = self._widgets.get(key)
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QComboBox):
            return widget.currentData()
        if isinstance(widget, QPlainTextEdit):
            return widget.toPlainText()
        return widget.text() if widget is not None else ""

    def _settings(self):
        """The ``with:`` mapping as the form has it now.

        Read live rather than from the step, because a plugin asked whether it
        is ready has to answer about what is on screen - somebody changing the
        backend and then pressing Check means the new one.
        """
        found = {}
        for spec in self._plugin.get("inputs") or []:
            key = spec.get("key", "")
            raw = self._value("with." + key)
            kind = spec.get("kind", "text")
            if kind == "check":
                found[key] = bool(raw)
                continue
            if kind == "env":
                pairs = _unpairs(raw)
                if pairs:
                    found[key] = pairs
                continue
            if kind == "args":
                parts = str(raw).split()
                if parts:
                    found[key] = parts
                continue
            text = str(raw).strip()
            if not text:
                continue
            if kind == "number":
                number = _number(text)
                found[key] = number if number is not None else text
            else:
                found[key] = text
        return found

    def _save(self):
        step = {"id": self._value("id").strip(),
                "plugin": self._step.get("plugin", "")}
        problems = []
        if not step["id"]:
            problems.append("A step needs an id.")

        label = self._value("label").strip()
        if label:
            step["label"] = label
        needs = [one.strip() for one in self._value("needs").split(",")
                 if one.strip()]
        if needs:
            step["needs"] = needs
        condition = self._value("if").strip()
        if condition:
            step["if"] = condition

        timeout = self._value("timeout").strip()
        if timeout:
            number = _number(timeout)
            if number is None or number <= 0:
                problems.append("Timeout must be a positive number of seconds.")
            else:
                step["timeout"] = number

        retry = {}
        for key, name in (("retry.attempts", "attempts"), ("retry.delay", "delay")):
            text = self._value(key).strip()
            if not text:
                continue
            number = _number(text)
            if number is None:
                problems.append("%s must be a number." % name.title())
            else:
                retry[name] = int(number) if name == "attempts" else number
        if retry:
            step["retry"] = retry

        if self._value("on_failure") != "stop":
            step["on_failure"] = self._value("on_failure")
        if self._value("disabled"):
            step["disabled"] = True

        settings = self._settings()
        if settings:
            step["with"] = settings

        if problems:
            self._complain(problems)
            return
        # Everything else the core decides. A second opinion here is a second
        # thing to keep in step with the validator that actually runs.
        self.saved = step
        self.accept()


class _VariablesTable(QWidget):
    """A cycle's variables as rows: name, value, and which kind of value it is.

    This was a text box of ``name = value`` lines. Three things made that the
    wrong shape once secrets existed. A line has nowhere to put a third fact, so
    "this one is secret" had no home; a secret's value must never be echoed, and
    a shared text box echoes everything in it; and a stored secret is a value
    the GUI is never given back, so there is nothing to put on the line even if
    there were room.

    So each row owns its own value box and can hide what is in it. A secret row
    starts **empty** whatever is stored: the core answers "is this one set", not
    "what is it", and leaving the box blank means saving without touching it
    keeps the stored value rather than quietly writing a blank over it.
    """

    #: Enough rows to be worth calling a table, with the dialog still opening at
    #: a sane height. It grows by scrolling, like everything else in the form.
    HEIGHT = 168

    #: One row's height. The controls in it are the theme's inputs, and they are
    #: the same ones the fields above the table use, so the table has to give
    #: them the room they are drawn at rather than the shorter row a table would
    #: pick for plain text.
    ROW_HEIGHT = 34

    #: Where a cell's text actually starts, measured from the edge of its
    #: column: the 4px a table insets a cell widget by, plus the 1px border and
    #: 8px padding ``theme.py`` gives every input. The header is told the same
    #: number, so a column's title sits over its values rather than to the left
    #: of them. If the input rule in ``theme.py`` ever changes, this follows it.
    TEXT_INSET = 4 + 1 + 8

    def __init__(self, parent=None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Name", "Value", "Type"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setFixedHeight(self.HEIGHT)
        # No grid and no frame: every cell holds a real input, and those already
        # draw their own edges. Leaving the grid on puts a second set of lines
        # between boxes that are already boxes.
        self.table.setShowGrid(False)
        self.table.setFrameShape(QTableWidget.NoFrame)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # Only the left padding: everything else about a header - its ground,
        # its font, its rule underneath - still comes from theme.py.
        header.setStyleSheet("QHeaderView::section { padding-left: %dpx; }"
                             % self.TEXT_INSET)
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(2, 104)
        column.addWidget(self.table)

        self.add_button = QPushButton("Add")
        self.remove_button = QPushButton("Remove")
        for button in (self.add_button, self.remove_button):
            button.setProperty("variant", "ghost")
        self.add_button.clicked.connect(lambda: self.add_row("", "", TEXT))
        self.remove_button.clicked.connect(self._remove_selected)
        column.addWidget(widgets.row(self.add_button, self.remove_button, None))

    # -- filling it in --------------------------------------------------------
    def load(self, variables, stored=()):
        """Show ``variables`` as the document holds them.

        A value that is a mapping is declared rather than plain - that is the
        shape the core reads and writes, ``{"secret": true}`` or
        ``{"kind": "path", "value": ...}`` - and ``stored`` names the secrets
        that already have a value, which is the only thing about them the GUI
        is told.
        """
        stored = set(stored or ())
        self.table.setRowCount(0)
        for name in sorted(variables or {}):
            value = (variables or {})[name]
            if isinstance(value, dict) and value.get("kind") == PATH:
                self.add_row(name, _text(value.get("value", "")), PATH)
            elif isinstance(value, dict):
                self.add_row(name, "", SECRET, stored=name in stored)
            else:
                self.add_row(name, _text(value), TEXT)

    def add_row(self, name="", value="", kind=TEXT, stored=False):
        """One row of three inputs, the same height and the same edges.

        The name is an input rather than a plain table item on purpose. A table
        draws an item's text flat and a cell widget with its own frame, so a row
        of one item and two widgets reads as one label beside two boxes - and
        the name would want a double-click to edit while the value took one.
        Three of the same control is one rhythm and one way of typing.
        """
        row = self.table.rowCount()
        self.table.insertRow(row)

        title = QLineEdit(name)
        title.setProperty("mono", True)
        title.setPlaceholderText("name")
        self.table.setCellWidget(row, 0, title)

        box = QLineEdit(value)
        box.setProperty("mono", True)
        # A folder chooser inside the field rather than a fourth column. Most
        # of a cycle's variables are paths - the checkout it works in, where
        # the rules are written down - and typing one by hand is how a cycle
        # ends up pointed at a directory that is nearly right. Inside the box
        # because a column of buttons would be a column of mostly nothing.
        box.browse = box.addAction(icons.icon("browse"),
                                   QLineEdit.TrailingPosition)
        box.browse.setToolTip("Choose a folder.")
        box.browse.setVisible(False)
        box.browse.triggered.connect(lambda _=False, edit=box: self._browse(edit))
        self.table.setCellWidget(row, 1, box)

        choice = QComboBox()
        choice.addItems(VARIABLE_KINDS)
        choice.setCurrentIndex(max(0, VARIABLE_KINDS.index(kind)
                                   if kind in VARIABLE_KINDS else 0))
        # The row's own box is handed to the handler rather than looked up: a
        # row removed above this one would move it, and the handler would then
        # dress the wrong line.
        choice.currentTextChanged.connect(
            lambda text, edit=box: self._dress(edit, text, stored))
        self.table.setCellWidget(row, 2, choice)

        self._dress(box, kind, stored)
        self.table.setRowHeight(row, self.ROW_HEIGHT)
        return row

    def _browse(self, box):
        """Put a chosen folder in this row's value box.

        Cancelling leaves what was there. A chooser that emptied the field
        when somebody changed their mind would be worse than no chooser.
        """
        chosen = widgets.pick_path(self, "Choose a folder", box.text(),
                                   directory=True)
        if chosen:
            box.setText(chosen)

    def _dress(self, box, kind, stored):
        """Make a value box look like what it now holds."""
        secret = kind == SECRET
        box.setEchoMode(QLineEdit.Password if secret else QLineEdit.Normal)
        # Offered on a path row and nowhere else. On a token it would invite
        # exactly the wrong thing; on ordinary text it is a button beside a
        # value nobody is choosing a folder for.
        action = getattr(box, "browse", None)
        if action is not None:
            action.setVisible(kind == PATH)
        if not secret:
            box.setPlaceholderText("")
        elif stored:
            box.setPlaceholderText("stored - type to replace it")
        else:
            box.setPlaceholderText("not set")

    def _remove_selected(self):
        rows = sorted({index.row() for index in
                       self.table.selectionModel().selectedRows()}, reverse=True)
        for row in rows or ([self.table.currentRow()]
                            if self.table.currentRow() >= 0 else []):
            self.table.removeRow(row)

    def set_writable(self, writable):
        """Disable the rows, and take the two buttons away entirely.

        Hidden rather than merely disabled, because the ghost variant has no
        disabled look of its own and a button that still reads as live but does
        nothing is worse than no button. There is nothing to add to a cycle that
        cannot be saved anyway.
        """
        self.setEnabled(bool(writable))
        self.add_button.setVisible(bool(writable))
        self.remove_button.setVisible(bool(writable))

    # -- reading it back ------------------------------------------------------
    def rows(self):
        """Every row as ``{"name", "value", "kind"}``, nameless ones dropped."""
        found = []
        for row in range(self.table.rowCount()):
            title = self.table.cellWidget(row, 0)
            name = (title.text() if title else "").strip()
            if not name:
                continue
            box = self.table.cellWidget(row, 1)
            choice = self.table.cellWidget(row, 2)
            kind = choice.currentText() if choice else TEXT
            found.append({"name": name,
                          "value": box.text() if box else "",
                          "kind": kind,
                          "secret": kind == SECRET})
        return found


class CycleDialog(_Editor):
    """A cycle's own properties: what it is called, whose it is, its variables."""

    def __init__(self, document, projects=(), parent=None, writable=True,
                 stored_secrets=()):
        super().__init__(document.get("name") or document.get("id", "Cycle"),
                         parent, writable)
        self._document = dict(document)
        self.saved = None
        #: Values the caller has to put in the secrets store, and names to take
        #: out of it. Kept apart from ``saved`` because they go somewhere else
        #: entirely: ``saved`` is written into the cycle file, and neither of
        #: these ever may be.
        self.secret_values = {}
        self.dropped_secrets = []
        self._was_secret = {name for name, value
                            in (document.get("variables") or {}).items()
                            if isinstance(value, dict) and value.get("secret")}

        self.subtitle.setText(document.get("id", ""))
        self.body_layout.addWidget(widgets.kicker("Cycle"))

        self.name = QLineEdit(document.get("name") or "")
        self.description = QLineEdit(document.get("description") or "")
        self.project = QComboBox()
        self.project.addItem("Unassigned", "")
        for one in projects:
            self.project.addItem(one, one)
        current = str(document.get("project") or "")
        if current and self.project.findData(current) < 0:
            self.project.addItem("%s (not in the list)" % current, current)
        self.project.setCurrentIndex(max(0, self.project.findData(current)))

        self.variables = _VariablesTable(self)
        self.variables.load(document.get("variables"), stored_secrets)

        for widget in (self.name, self.description, self.project):
            widget.setEnabled(self._writable)
        self.variables.set_writable(self._writable)

        self.body_layout.addWidget(widgets.field("Name", self.name))
        self.body_layout.addWidget(widgets.field("Description",
                                                 self.description))
        self.body_layout.addWidget(widgets.field("Project", self.project))
        self.body_layout.addWidget(widgets.field(
            "Variables", self.variables,
            "A step reads one as ${vars.name}. A secret's value is not kept in "
            "the cycle file - that file is committed and shipped - but "
            "encrypted beside your own data (Settings -> Cycle secrets). A "
            "path is ordinary text that the row will help you find."))
        self.body_layout.addStretch(1)

    def _save(self):
        variables, values = {}, {}
        for row in self.variables.rows():
            if row["secret"]:
                # The name goes in the file, the value goes to the store, and
                # an untouched box means "keep what is already there" rather
                # than "set it to nothing".
                variables[row["name"]] = {"secret": True}
                if row["value"]:
                    values[row["name"]] = row["value"]
            elif row["kind"] == PATH:
                variables[row["name"]] = {"kind": PATH, "value": row["value"]}
            else:
                variables[row["name"]] = row["value"]

        self.secret_values = values
        # A secret that is gone, or is now text or a path, leaves a value behind in
        # the store. Nothing would read it, but a credential nobody can see and
        # nobody deletes is exactly what this whole arrangement is against.
        self.dropped_secrets = sorted(
            self._was_secret - {name for name, value in variables.items()
                                if isinstance(value, dict) and value.get("secret")})
        self.saved = {"name": self.name.text().strip(),
                      "description": self.description.text().strip(),
                      "project": self.project.currentData() or "",
                      "variables": variables}
        self.accept()


# -- reading and writing the small formats ------------------------------------
def _text(value):
    """A value as the string a line edit shows. None and False become blank."""
    if value is None or value is False:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _number(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return int(value) if value.is_integer() else value


def _pairs(mapping):
    """``{"a": 1}`` as the ``a = 1`` lines an env box shows.

    A newline inside a value is written ``\\n`` rather than as a real break. One
    pair is one line, so a value that spans lines would otherwise show up as a
    blank line in the box and come back without the newline it was saved with -
    silently, on a save that changed nothing.
    """
    return "\n".join("%s = %s" % (key, _escaped(value))
                     for key, value in sorted((mapping or {}).items()))


def _escaped(value):
    """A value on one line. Backslashes too, or the reverse would be ambiguous."""
    return _text(value).replace("\\", "\\\\").replace("\n", "\\n")


def _unescaped(text):
    r"""The reverse of :func:`_escaped`: ``\n`` back to a newline, ``\\`` to one."""
    out, index = [], 0
    while index < len(text):
        pair = text[index:index + 2]
        if pair == "\\n":
            out.append("\n")
            index += 2
        elif pair == "\\\\":
            out.append("\\")
            index += 2
        else:
            out.append(text[index])
            index += 1
    return "".join(out)


def _unpairs(text):
    """The reverse. A line without an ``=`` is dropped rather than refused.

    Forgiving on purpose: this is a small box in a form, not a file format, and
    refusing to save over a stray line would be a worse answer than ignoring
    it. What it produces still goes through the core, which does have an
    opinion.
    """
    found = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip():
            found[name.strip()] = _unescaped(value.strip())
    return found


def _duration(milliseconds):
    seconds = (milliseconds or 0) / 1000.0
    if seconds < 1:
        return "%dms" % (milliseconds or 0)
    if seconds < 60:
        return "%.1fs" % seconds
    minutes, seconds = divmod(int(seconds), 60)
    return "%dm %02ds" % (minutes, seconds)


def _output_value(value):
    """One of a step's outputs, as a line rather than as a repr.

    A list or a mapping is named and counted, never printed: ``issues`` as
    ``[{'description': 'Needless complexity: the function...`` is unreadable, cut off
    mid-word by the panel edge, and says less than "2 items" does.
    """
    if isinstance(value, (list, tuple)):
        return "%d item%s" % (len(value), "" if len(value) == 1 else "s")
    if isinstance(value, dict):
        return "%d key%s" % (len(value), "" if len(value) == 1 else "s")
    return _short(value)


def _short(value):
    text = str(value)
    return text if len(text) <= 240 else text[:240].rstrip() + "..."
