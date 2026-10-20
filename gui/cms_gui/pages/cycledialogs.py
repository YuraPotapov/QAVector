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

**A field nobody touched is given back exactly as it came.** A form is a
narrower thing than the file: a checkbox cannot hold ``${vars.offline}``, a
line of words cannot say where one option ends and the next begins, and a box
of ``name = value`` lines cannot tell ``3`` from ``"3"``. Converting every
value through its widget on every save meant opening a step and pressing Save
turned ``options: [Start work, Leave it]`` into four options and a list of
files into a list holding one reference. So each field remembers what it showed,
and only one that now shows something else is read back through its widget.
A value its widget cannot show at all is offered as an expression, or kept as
written with a pointer to the YAML tab - never quietly reshaped.
"""

import copy
import os
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QHeaderView, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
                               QTableWidget, QVBoxLayout, QWidget)

from .. import icons, theme, widgets

#: What a listening step's schedule starts at, and the least it may be. The
#: core's own (domain/cycle.py) - repeated because the GUI never imports the
#: core, and checked again by the core when the file is saved.
WATCH_EVERY = 300
WATCH_EVERY_MIN = 60

#: What a step carries regardless of plugin. Kept here rather than read off the
#: node payload so the order is the order somebody reads them in.
ON_FAILURE = ("stop", "continue")

#: How the wait between tries grows - the core's domain.cycle.BACKOFF.
BACKOFF = ("fixed", "exponential")

#: A value that is one ``${...}`` reference and nothing else. The core resolves
#: it to whatever type it points at, so no widget but a line of text can hold
#: it without changing what it means.
_WHOLE_REFERENCE = re.compile(r"^\s*\$\{[^}]*\}\s*$")

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
                 ask_plugin=None, trigger=None, groups=(), apply_label="",
                 read_only_note="", phases=(), restrictions=None, core=None):
        super().__init__(step.get("label") or step.get("id", "Step"), parent,
                         writable)
        if apply_label:
            # "Apply" when the change goes into an unsaved draft rather than
            # straight into the file: the button says which it is.
            self.save_button.setText(apply_label)
        if read_only_note and not writable:
            self.note.setText(read_only_note)
        self._step = dict(step)
        self._plugin = dict(plugin or {})
        #: {"folder", "cycle"} when the cycle has a project folder - the step
        #: then shows what it is held to - or None, and nothing changes.
        self._restriction_context = restrictions
        self._core = core
        self._step_restrictions = dict(step.get("restrictions") or {})
        self._ask_plugin = ask_plugin
        self._groups = [one for one in groups if one]
        self._phases = list(phases)
        self._widgets = {}
        #: What each field showed when the dialog opened, by key - so Save can
        #: tell a field somebody changed from one they only looked at.
        self._shown = {}
        #: Fields whose value no widget here can hold; given back as they came.
        self._kept = set()
        #: Fields shown as a line of text because the value is an expression.
        self._as_text = set()
        self.saved = None            # the changed step, once Save was pressed
        #: The trigger watching this step, as the file has it, or None.
        self._trigger = dict(trigger) if trigger else None
        #: {"enabled", "every"} once saved - for a step that can be listened
        #: to - or None when there is nothing to say about listening.
        self.saved_trigger = None

        self.subtitle.setText("%s%s" % (
            self._plugin.get("id") or step.get("plugin", ""),
            " - " + self._plugin["summary"] if self._plugin.get("summary")
            else ""))

        self._add_step_fields()
        self._add_plugin_fields()
        self._add_restrictions()
        self._add_listening()
        self._add_actions()
        self._add_live(live)
        self.body_layout.addStretch(1)
        # Taken once everything is built: what each field shows now is what
        # "untouched" means at Save.
        self._shown = {key: self._value(key) for key in self._widgets}

    # -- the step's own -------------------------------------------------------
    def _add_step_fields(self):
        self.body_layout.addWidget(widgets.kicker("Step"))
        self._line("id", "Id", self._step.get("id", ""),
                   "Letters, digits, _ and -. Other steps name it in needs.")
        self._line("label", "Label", self._step.get("label", ""),
                   "What the node is called on the canvas. Blank uses the id.")
        self._group_field()
        self._check("important", "Important", bool(self._step.get("important")),
                    "Important mode asks for approval before this step starts.")
        self._line("importance_reason", "Why important", self._step.get("importance_reason", ""),
                   "Explain the cost, external change or decision that needs attention.")
        needs = self._step.get("needs") or []
        self._line("needs", "Depends on",
                   ", ".join(needs) if isinstance(needs, list) else _text(needs),
                   "Step ids, comma separated. They must succeed first.")
        self._line("if", "Runs if", self._step.get("if", ""),
                   "${steps.x.status} == 'failed', or blank for always. A step "
                   "with a condition runs even when what it needs did not "
                   "succeed - it decides for itself - and must also depend on "
                   "what it reads.")
        self._line("timeout", "Timeout",
                   _text(self._step.get("timeout")),
                   "Seconds for the whole step, retries and waits included. "
                   "Blank means no deadline. Stopping is cooperative: the step "
                   "is asked to stop, and a person watching may give it more "
                   "time.")
        retry = self._step.get("retry") or {}
        self._line("retry.attempts", "Tries", _text(retry.get("attempts")),
                   "Total tries, not extra ones. Blank means one.")
        self._line("retry.delay", "Delay between tries",
                   _text(retry.get("delay")), "Seconds before the second try.")
        self._choice("retry.backoff", "Backoff",
                     _text(retry.get("backoff")) or "fixed", BACKOFF,
                     "fixed waits the same delay every time; exponential "
                     "doubles it before each further try.")
        self._choice("on_failure", "On failure",
                     self._step.get("on_failure") or "stop", ON_FAILURE,
                     "stop cancels the rest of the run; continue lets "
                     "unrelated branches carry on.")
        self._check("disabled", "Disabled", bool(self._step.get("disabled")),
                    "A disabled step is skipped, and so is anything that needs "
                    "it.")

    def _group_field(self):
        """The phase the step is drawn in: typed, or one the cycle already has."""
        widget = QComboBox()
        widget.setEditable(True)
        widget.addItem("")
        for name in self._groups:
            widget.addItem(name)
        widget.setCurrentText(_text(self._step.get("group")))
        widget.setEnabled(self._writable)
        self._widgets["group"] = widget
        self.body_layout.addWidget(widgets.field(
            "Visual group" if self._phases else "Phase", widget,
            "Which part of the cycle this belongs to. The canvas frames a "
            "phase's steps together and can fold them into one box. It does "
            "not change when the step runs."))
        if self._phases:
            execution = QComboBox()
            for phase in self._phases:
                execution.addItem(phase.get("title") or phase["id"], phase["id"])
            execution.setCurrentIndex(execution.findData(self._step.get("phase")))
            execution.setEnabled(self._writable)
            self._widgets["phase"] = execution
            self.body_layout.addWidget(widgets.field("Execution phase", execution,
                "The scheduler completes earlier phases before this step may run. Phase policies are defined in YAML."))

    # -- whatever the plugin declares ----------------------------------------
    def _add_plugin_fields(self):
        fields = self._plugin.get("inputs") or []
        settings = self._step.get("with") or {}
        declared = {spec.get("key") for spec in fields}
        undeclared = sorted(key for key in settings if key not in declared)
        if not fields and not undeclared:
            return
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
        if undeclared:
            # Kept, not dropped: a typo the core reports is still the author's
            # to fix, and a dialog that deleted it would fix it by losing it.
            note = widgets.lede("Also in the file, and not something %s takes: "
                                "%s. Kept as written - the Problems tab says "
                                "why; change them in the YAML tab."
                                % (self._plugin.get("id") or "this plugin",
                                   ", ".join(undeclared)))
            note.setWordWrap(True)
            self.body_layout.addWidget(note)

    def _by_kind(self, key, label, spec, value):
        """One widget for one declared field. The kind decides, not the key."""
        kind = spec.get("kind", "text")
        hint = spec.get("hint", "")
        if value is not None and not _fits(kind, value):
            if isinstance(value, str):
                # An expression where a structure or a flag was expected:
                # resolved when the step runs, so text is the honest widget.
                self._as_text.add(key)
                self._line(key, label, value,
                           (hint + " " if hint else "")
                           + "This is an expression, resolved when the step "
                           "runs; it is kept as text.")
                return
            self._keep(key, label, value, hint)
            return
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
                       _join_args(value) if isinstance(value, (list, tuple))
                       else _text(value),
                       (hint + " " if hint else "") + "Separated by spaces; "
                       "put an item with a space in it in quotes, "
                       "\"like this one\".")
        elif kind in ("file", "dir"):
            self._path(key, label, _text(value), hint, kind)
        else:
            self._line(key, label, _text(value), hint)

    # -- what the plugin can be asked ----------------------------------------
    # -- being listened to ---------------------------------------------------
    def _add_listening(self):
        """Only for a step whose plugin says it can be listened to. Off keeps
        the schedule in the file, so turning it back on is one tick."""
        if not self._plugin.get("watchable"):
            return
        trigger = self._trigger or {}
        self.body_layout.addWidget(widgets.kicker("Listen for new work"))
        self._check("listen.enabled", "Listen",
                    bool(trigger) and trigger.get("enabled", True) is not False,
                    "While the application is open, ask this step what it "
                    "would take and offer anything new: start work on it? "
                    "Nothing starts without the answer.")
        self._line("listen.every", "Every (seconds)",
                   _text(trigger.get("every", WATCH_EVERY)),
                   "How often it asks. %d at the least." % WATCH_EVERY_MIN)

    def _listening(self, problems):
        if not self._plugin.get("watchable"):
            return None
        enabled = bool(self._value("listen.enabled"))
        every = _number(self._value("listen.every").strip() or str(WATCH_EVERY))
        if every is None or every < WATCH_EVERY_MIN:
            if enabled or self._trigger:
                problems.append("Every must be a number of seconds, %d at the "
                                "least." % WATCH_EVERY_MIN)
            return None
        if not enabled and not self._trigger:
            return None                 # never listened, still not: nothing to say
        return {"enabled": enabled, "every": int(every)}

    def _add_restrictions(self):
        """What the cycle's restrictions hold this step to, and a way to narrow it.

        Only when the cycle has a project folder: without one its steps run as
        they always have, and a section about restrictions would be noise.
        """
        self.system_box = None
        self.restrict_button = None
        if self._restriction_context is None:
            return
        from .. import restrictions as restrictions_mod
        self.body_layout.addWidget(widgets.kicker("Restrictions"))
        restrictable = self._step.get("plugin", "") in restrictions_mod.CYCLE_PLUGINS
        self.system_box = QCheckBox("System step - runs without the cycle's restrictions")
        self.system_box.setChecked(bool(self._step.get("system")) or not restrictable)
        self.system_box.setEnabled(self._writable and restrictable)
        self.restrict_button = restrictions_mod.FolderButton()
        self.restrict_button.clicked.connect(self._edit_restrictions)
        hint = ("This plugin works inside QAVector itself or drives a browser, so restrictions do not apply to it."
                if not restrictable else
                "Runs in the cycle's project folder, held to the cycle's restrictions; it can only narrow them.")
        self.body_layout.addWidget(widgets.field("", widgets.row(self.system_box, None, self.restrict_button), hint))
        self.system_box.toggled.connect(self._sync_restrictions)
        self._sync_restrictions()

    def _sync_restrictions(self, *_):
        from .. import restrictions as restrictions_mod
        if self.restrict_button is None:
            return
        system = self.system_box.isChecked()
        effective = restrictions_mod.resolve([("cycle", self._restriction_context.get("cycle") or {}),
                                              ("step", self._step_restrictions)])
        self.restrict_button.set_state(
            {} if system else effective, enabled=not system,
            why="A system step runs without restrictions.",
            pending=self._step_restrictions != dict(self._step.get("restrictions") or {}))

    def _edit_restrictions(self):
        from .. import restrictions as restrictions_mod
        folder = self._restriction_context.get("folder") or ""
        window = restrictions_mod.RestrictionsWindow(
            self._step.get("label") or self._step.get("id", "step"), folder, "step", self._step_restrictions,
            outer=[("cycle", self._restriction_context.get("cycle") or {})], outer_name="cycle", core=self._core,
            probe={"folder": folder, "cwd": folder}, parent=self, writable=self._writable)
        if window.exec() == QDialog.Accepted and window.saved is not None:
            self._step_restrictions = window.saved
            self._sync_restrictions()
        window.deleteLater()

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
    def _keep(self, key, label, value, hint=""):
        """A value no widget here can hold without changing it: shown, kept."""
        self._kept.add(key)
        shown = widgets.mono(_short(_output_value(value) if not isinstance(value, str)
                                    else value))
        shown.setWordWrap(True)
        shown.setToolTip(str(value))
        self.body_layout.addWidget(widgets.field(
            label, shown, (hint + " " if hint else "")
            + "Kept exactly as written; change it in the YAML tab."))

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
            return widget.currentText() if widget.isEditable() else widget.currentData()
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
            if "with." + key in self._kept:
                found[key] = (self._step.get("with") or {}).get(key)
                continue
            raw = self._value("with." + key)
            kind = spec.get("kind", "text")
            if "with." + key in self._as_text:
                if str(raw).strip():
                    found[key] = str(raw).strip()
                continue
            if kind == "check":
                found[key] = bool(raw)
                continue
            if kind == "env":
                pairs = _unpairs(raw)
                if pairs:
                    found[key] = pairs
                continue
            if kind == "args":
                parts = _split_args(str(raw))
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

    def _touched(self, key):
        """Whether the field now shows something other than it opened with."""
        return key not in self._kept and self._value(key) != self._shown.get(key)

    def _save(self):
        step = {"id": self._value("id").strip(),
                "plugin": self._step.get("plugin", "")}
        problems = []
        if not step["id"]:
            problems.append("A step needs an id.")

        label = self._value("label").strip()
        if label:
            step["label"] = label
        group = str(self._value("group") or "").strip()
        if group:
            step["group"] = group
        if "phase" in self._widgets:
            step["phase"] = self._value("phase") or ""
        if self._value("important"):
            step["important"] = True
        reason = self._value("importance_reason").strip()
        if reason:
            step["importance_reason"] = reason
        needs = [one.strip() for one in self._value("needs").split(",")
                 if one.strip()]
        if needs:
            step["needs"] = needs
        condition = self._value("if").strip()
        if condition:
            step["if"] = condition

        timeout = self._value("timeout").strip()
        if timeout and self._touched("timeout"):
            number = _number(timeout)
            if number is None or number <= 0:
                problems.append("Timeout must be a positive number of seconds.")
            else:
                step["timeout"] = number

        retry = {}
        for key, name in (("retry.attempts", "attempts"), ("retry.delay", "delay")):
            text = self._value(key).strip()
            if not text or not self._touched(key):
                continue
            number = _number(text)
            if number is None:
                problems.append("%s must be a number." % name.title())
            else:
                retry[name] = int(number) if name == "attempts" else number
        if self._value("retry.backoff") not in ("", "fixed", None):
            retry["backoff"] = self._value("retry.backoff")
        if retry:
            step["retry"] = retry

        if self._value("on_failure") != "stop":
            step["on_failure"] = self._value("on_failure")
        if self._value("disabled"):
            step["disabled"] = True

        for spec in self._plugin.get("inputs") or []:
            key = "with." + spec.get("key", "")
            if (spec.get("kind") == "args" and key not in self._as_text
                    and self._touched(key)
                    and _split_args(str(self._value(key))) is None):
                problems.append("%s: a quote is not closed."
                                % (spec.get("label") or spec.get("key")))
        settings = self._settings()
        if settings:
            step["with"] = settings
        listening = self._listening(problems)
        if self._restriction_context is not None:
            if self.system_box is not None and self.system_box.isChecked() and self.system_box.isEnabled():
                step["system"] = True
                step.pop("restrictions", None)
            else:
                step.pop("system", None)
                if self._step_restrictions:
                    step["restrictions"] = copy.deepcopy(self._step_restrictions)
                else:
                    step.pop("restrictions", None)

        if problems:
            self._complain(problems)
            return
        # Anything nobody changed goes back exactly as it came - value, type,
        # and whether it was written at all - and so does whatever the form
        # has no field for.
        step = self._untouched_as_they_were(step)
        # Everything else the core decides. A second opinion here is a second
        # thing to keep in step with the validator that actually runs.
        self.saved = step
        self.saved_trigger = listening
        self.accept()

    def _untouched_as_they_were(self, step):
        """``step`` with every field nobody changed put back as it came."""
        original = self._step
        for key in self._widgets:
            if key.startswith("listen.") or self._touched(key):
                continue
            path = key.split(".", 1) if key.startswith(("with.", "retry.")) else [key]
            _put(step, path, _get(original, path))
        declared = {spec.get("key") for spec in self._plugin.get("inputs") or []}
        for name, value in (original.get("with") or {}).items():
            if name not in declared:
                _put(step, ["with", name], value)
        for name in ("with", "retry"):
            if name in step and not step[name]:
                del step[name]
        # Keys the dialog has no field for at all, which a newer file may have.
        for name, value in original.items():
            if name not in step and name not in _STEP_FIELDS:
                step[name] = value
        return step


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
        self.original = {}
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
        #: name -> (kind, what the row showed, the value as the file had it),
        #: so a row nobody changed gives back ``3`` rather than ``"3"`` and
        #: ``false`` rather than an empty string.
        self.original = {}
        for name in sorted(variables or {}):
            value = (variables or {})[name]
            if isinstance(value, dict) and value.get("kind") == PATH:
                shown, kind = _text(value.get("value", "")), PATH
                self.add_row(name, shown, PATH)
            elif isinstance(value, dict):
                shown, kind = "", SECRET
                self.add_row(name, "", SECRET, stored=name in stored)
            else:
                shown, kind = ("false" if value is False else _text(value)), TEXT
                self.add_row(name, shown, TEXT)
            self.original[name] = (kind, shown, value)

    def value_of(self, row):
        """What a row means for the file: as it came, when nobody changed it."""
        kind, shown, value = self.original.get(row["name"], (None, None, None))
        if kind == row["kind"] and shown == row["value"] and kind != SECRET:
            return value
        if row["kind"] == PATH:
            return {"kind": PATH, "value": row["value"]}
        return row["value"]

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
                 stored_secrets=(), core=None):
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
        # The folder the cycle works on - the same name, and the same button
        # beside it, as a services project's. Without one the cycle runs as it
        # always has.
        from .. import restrictions as restrictions_mod
        self._core = core
        self._restrictions = dict(document.get("restrictions") or {})
        self.project_folder = QLineEdit(str(document.get("project_folder") or ""))
        self.project_folder.setPlaceholderText("~/projects/app, or ${vars.project_dir}")
        self.project_folder.setEnabled(self._writable)
        browse = QPushButton("Browse…")
        browse.setProperty("variant", "ghost")
        browse.setEnabled(self._writable)
        browse.clicked.connect(self._pick_project_folder)
        self.restrict_button = restrictions_mod.FolderButton()
        self.restrict_button.clicked.connect(self._edit_restrictions)
        self.project_folder.textChanged.connect(self._sync_restrictions)
        self.body_layout.addWidget(widgets.field(
            "Project folder", widgets.row(self.project_folder, browse, self.restrict_button),
            "The folder the steps work on: a path, or a path variable like ${vars.project_dir}. "
            "Restrictions hold the steps to it - network, files and limits."))
        self.body_layout.addWidget(widgets.field(
            "Variables", self.variables,
            "A step reads one as ${vars.name}. A secret's value is not kept in "
            "the cycle file - that file is committed and shipped - but "
            "encrypted beside your own data (Settings -> Cycle secrets). A "
            "path is ordinary text that the row will help you find."))
        self.body_layout.addStretch(1)
        self._sync_restrictions()

    def _folder(self):
        """The folder as written and, when it can be, resolved for a probe."""
        written = self.project_folder.text().strip()
        resolved = written
        for row in self.variables.rows():
            if not row["secret"]:
                resolved = resolved.replace("${vars.%s}" % row["name"], str(self.variables.value_of(row)))
        if "${" in resolved or not resolved:
            return written, ""
        return written, os.path.abspath(os.path.expanduser(resolved))

    def _pick_project_folder(self):
        path = widgets.pick_path(self, "Project folder", self._folder()[1] or "~", directory=True)
        if path:
            self.project_folder.setText(path)

    def _sync_restrictions(self, *_):
        from .. import restrictions as restrictions_mod
        written, _resolved = self._folder()
        self.restrict_button.set_state(
            restrictions_mod.resolve([("cycle", self._restrictions)]), enabled=bool(written),
            why="Set the project folder to restrict what the cycle's steps may do.",
            pending=self._restrictions != dict(self._document.get("restrictions") or {}))

    def _edit_restrictions(self):
        from .. import restrictions as restrictions_mod
        written, resolved = self._folder()
        window = restrictions_mod.RestrictionsWindow(
            self.name.text().strip() or self._document.get("id", "cycle"), resolved or written, "cycle",
            self._restrictions, core=self._core, probe={"folder": resolved, "cwd": resolved} if resolved else None,
            parent=self, writable=self._writable)
        if window.exec() == QDialog.Accepted and window.saved is not None:
            self._restrictions = window.saved
            self._sync_restrictions()
        window.deleteLater()

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
            else:
                variables[row["name"]] = self.variables.value_of(row)

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
        # Empty values are taken out of the file by the caller.
        self.saved["project_folder"] = self.project_folder.text().strip()
        self.saved["restrictions"] = copy.deepcopy(self._restrictions) if self.saved["project_folder"] else {}
        self.accept()


# -- reading and writing the small formats ------------------------------------
#: Every key of a step the dialog has a field for. Anything else a step carries
#: is passed through untouched.
_STEP_FIELDS = ("id", "plugin", "label", "group", "important", "importance_reason", "runtime",
                "restrictions", "system", "needs", "if", "with",
                "timeout", "retry", "on_failure", "disabled")

#: Stands for "not in the step", which None cannot: None is a value.
_ABSENT = object()


def _get(mapping, path):
    for part in path:
        if not isinstance(mapping, dict) or part not in mapping:
            return _ABSENT
        mapping = mapping[part]
    return mapping


def _put(mapping, path, value):
    """Set ``path`` to ``value``, or take it out when the value is absent."""
    for part in path[:-1]:
        if value is _ABSENT and not isinstance(mapping.get(part), dict):
            return
        mapping = mapping.setdefault(part, {})
    if value is _ABSENT:
        mapping.pop(path[-1], None)
    else:
        mapping[path[-1]] = value


_SCALARS = (str, int, float, bool, type(None))


def _fits(kind, value):
    """Whether a widget of ``kind`` can show ``value`` and give it back whole."""
    if kind == "check":
        return isinstance(value, bool)
    if kind == "env":
        return isinstance(value, dict) and all(
            isinstance(one, _SCALARS) and "=" not in str(key) and "\n" not in str(key)
            for key, one in value.items())
    if kind == "args":
        return (isinstance(value, (list, tuple))
                and all(isinstance(one, _SCALARS) for one in value)
                and _join_args(value) is not None)
    if isinstance(value, str) and _WHOLE_REFERENCE.match(value):
        return kind not in ("check", "env", "args")
    return isinstance(value, _SCALARS)


def _join_args(items):
    """A list as one line: items apart by spaces, one with a space in quotes.

    None when an item holds both kinds of quote - no quoting here could bring
    it back whole, so the caller keeps the list as written instead.
    """
    parts = []
    for item in items:
        text = _text(item) if item is not True else "true"
        if text and not any(one in text for one in " \t\"'"):
            parts.append(text)
        elif '"' not in text:
            parts.append('"%s"' % text)
        elif "'" not in text:
            parts.append("'%s'" % text)
        else:
            return None
    return " ".join(parts)


def _split_args(text):
    """The reverse of :func:`_join_args`. None when a quote is left open.

    Quotes group, and nothing else is special - a backslash in particular is
    left alone, because the paths people type here are Windows paths as often
    as not, and a shell's escaping would eat half of every one.
    """
    parts, current, quote, started = [], [], None, False
    for character in text or "":
        if quote:
            if character == quote:
                quote = None
            else:
                current.append(character)
        elif character in "\"'":
            quote, started = character, True
        elif character.isspace():
            if current or started:
                parts.append("".join(current))
            current, started = [], False
        else:
            current.append(character)
    if quote:
        return None
    if current or started:
        parts.append("".join(current))
    return parts


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
