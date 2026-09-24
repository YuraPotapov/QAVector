"""The forms behind ``logsources.json``: connections, logs, and reading one.

The file has two levels and conflating them is what makes a flat list of
"sources" unusable. A **connection** says *where* to run a reader - this machine,
or one ssh hop away - and a **log** says *what* to read there. One connection
serves every log on a machine, so a stand with three logs opens one ssh
connection rather than three.

**Rows are created in a dialog, not in a grid.** An inline row means typing eight
narrow columns in a fixed order with nothing saying what each one wants, and the
fields that matter depend on choices made in the same row - an ssh connection
needs a host and a local one must not have one; a log's target is a path, a
container, a unit or a URL depending on its type. A form can show exactly the
fields that apply and explain them; a table cannot. So the tables that show these
rows are a readable overview, and every edit opens :class:`ConnectionDialog` or
:class:`LogDialog`.

**Nothing here is coupled to one backend.** Format presets are named after the
*shape* of a line (`iso`, `slash`, `clf`, `syslog`, `none`), with application names
offered as aliases, and "custom" takes your own timestamp/level patterns for a
backend no preset describes.

The file is the launcher's, so these forms keep it exactly as valid as the CLI
expects: every rule in ``logsourcesfile.validate`` mirrors one in
``engine.serverlog``. The GUI never imports the engine, so it has no ssh, no
docker and no tail of its own - :class:`LogViewerDialog` shows what the *core*
read when asked.

The tables, the file and the Save button live on the Services & Logs page
(:mod:`cms_gui.pages.services`), which owns these dialogs. They stayed here
because they are about one file's shape, and that page is about two.
"""

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from .. import logsourcesfile as lsf
from .. import theme, widgets

#: Label for the one field whose meaning changes with the log's type, and what to
#: say underneath it. The whole reason a log is edited in a form.
TARGET_LABELS = {
    "file": ("Path", "Absolute path to the log file on that machine."),
    "docker": ("Container", "Container name or id; read with `docker logs -f`."),
    "journal": ("Unit", "systemd unit, or leave empty for the whole journal."),
    "http": ("URL", "A line-oriented HTTP stream (plain text or server-sent events)."),
}

TYPE_HINTS = {
    "local": "Readers run on this machine.",
    "ssh": "Readers run over ssh. The host key is not accepted automatically - "
           "ssh in by hand once first.",
}


def _form(parent):
    column = QVBoxLayout(parent)
    column.setContentsMargins(20, 18, 20, 16)
    column.setSpacing(10)
    return column


class RowDialog(QDialog):
    """Shared shell: a scrolling form, a live problem line, and OK gated on validity.

    The form scrolls because the log form grows: revealing the custom-format
    patterns takes it past a thousand pixels, which is taller than a laptop
    screen, and a dialog whose OK button is off the bottom edge cannot be
    completed at all. The problem line and the buttons stay outside the scroll,
    so what is wrong and how to leave are always in view.
    """

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.frame = widgets.dress(self)
        self.setMinimumWidth(580)
        self._settled = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._body = QWidget()
        self.column = _form(self._body)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._body)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(self._scroll, 1)

        self.problem = QLabel("")
        self.problem.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        footer = QWidget()
        footer_column = QVBoxLayout(footer)
        footer_column.setContentsMargins(20, 8, 20, 14)
        footer_column.setSpacing(8)
        footer_column.addWidget(self.problem)
        footer_column.addWidget(self.buttons)
        outer.addWidget(footer)

    def finish_layout(self):
        self.column.addStretch(1)
        self._fit()

    def _fit(self):
        """Open at the form's natural height, but never taller than the screen."""
        wanted = self._body.sizeHint().height() + 110
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry().height() if screen else 900
        self.resize(self.width() or 580, min(wanted, int(available * 0.9)))

    def showEvent(self, event):
        super().showEvent(event)
        # Once the form has real geometry, take back whatever it did not need.
        # Every hint under a field is a word-wrapped QLabel, and one of those
        # reports a sizeHint for a width it has not been given - always more
        # lines than it will actually take. _fit can only add those up, so a form
        # opened with several hints stood a band of nothing under its last field.
        if not self._settled:
            self._settled = True
            QTimer.singleShot(0, self._settle)

    def _settle(self):
        """Shrink to what is actually laid out. Never grows, never scrolls away."""
        content = 0
        for index in range(self.column.count()):
            widget = self.column.itemAt(index).widget()
            if widget is not None and widget.isVisible():
                content = max(content, widget.geometry().bottom() + 1)
        if content <= 0:
            return
        slack = self._body.height() - (content
                                       + self.column.contentsMargins().bottom())
        # At the screen cap the body is already scrolling and there is no slack
        # to take; a couple of pixels is rounding, not a band.
        if slack > 2:
            self.resize(self.width(), self.height() - slack)

    def show_problems(self, problems):
        """Report what is still wrong and gate OK on it. Returns True when valid."""
        # Only the row being edited: validate() reports on the whole file, and a
        # problem in another row is not something this dialog can fix.
        self.problem.setText("\n".join(problems[:3]))
        self.problem.setStyleSheet("color: %s; font-size: 12px;"
                                   % (theme.BAD if problems else theme.NEUTRAL[700]))
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(not problems)
        return not problems


class ConnectionDialog(RowDialog):
    """Create or edit one connection: where readers run."""

    def __init__(self, row=None, taken=(), parent=None):
        super().__init__("Connection" if row else "New connection", parent)
        self._row = (row or lsf.ConnectionRow(type="local")).copy()
        self._taken = {name for name in taken if name != self._row.name}

        self.name = QLineEdit(self._row.name)
        self.name.setPlaceholderText("a short name, e.g. staging")
        self.name.textChanged.connect(self._changed)
        self.column.addWidget(widgets.field(
            "Name", self.name, "Logs refer to this connection by name."))

        self.type = QComboBox()
        self.type.addItems(list(lsf.CONNECTION_TYPES))
        self.type.setCurrentText(self._row.type)
        self.type.currentTextChanged.connect(self._type_changed)
        self.type_hint = QLabel("")
        self.type_hint.setProperty("role", "hint")
        self.type_hint.setWordWrap(True)
        self.column.addWidget(widgets.field("Runs on", self.type))
        self.column.addWidget(self.type_hint)

        self.host = QLineEdit(self._row.host)
        self.host.setPlaceholderText("staging.example.com")
        self.host.textChanged.connect(self._changed)
        self.host_field = widgets.field("Host", self.host)
        self.column.addWidget(self.host_field)

        self.user = QLineEdit(self._row.user)
        self.user.setPlaceholderText("deploy   (optional - ~/.ssh/config may say)")
        self.user.textChanged.connect(self._changed)
        self.user_field = widgets.field("User", self.user)
        self.column.addWidget(self.user_field)

        self.identity = QLineEdit(self._row.identity)
        self.identity.setPlaceholderText("~/.ssh/id_ed25519   (optional)")
        self.identity.textChanged.connect(self._changed)
        browse = QPushButton("Browse…")
        browse.setProperty("variant", "ghost")
        browse.clicked.connect(self._pick_identity)
        self.identity_field = widgets.field(
            "Identity file", widgets.row(self.identity, browse))
        self.column.addWidget(self.identity_field)

        self.port = QLineEdit(str(self._row.port or ""))
        self.port.setPlaceholderText("22")
        self.port.textChanged.connect(self._changed)
        self.port_field = widgets.field("Port", self.port)
        self.column.addWidget(self.port_field)

        self.options = QLineEdit(", ".join(self._row.options))
        self.options.setPlaceholderText("ConnectTimeout=5, ServerAliveInterval=30")
        self.options.textChanged.connect(self._changed)
        self.options_field = widgets.field(
            "Extra ssh options", self.options,
            "Comma-separated, each passed as -o.")
        self.column.addWidget(self.options_field)

        self.finish_layout()
        self._type_changed(self.type.currentText())

    def _pick_identity(self):
        # Starting inside ~/.ssh rather than above it: a chooser does not list a
        # dotted directory, but it does show what is in one it opens in.
        start = self.identity.text().strip() or "~/.ssh"
        path = widgets.pick_path(self, "Private key", start)
        if path:
            self.identity.setText(path)

    def _type_changed(self, value):
        remote = value == "ssh"
        for field in (self.host_field, self.user_field, self.identity_field,
                      self.port_field, self.options_field):
            field.setVisible(remote)
        self.type_hint.setText(TYPE_HINTS.get(value, ""))
        self._fit()
        self._changed()

    def _changed(self, *_args):
        self.show_problems(self._problems())

    def _problems(self):
        row = self.value()
        # The file's own rules, on this row alone, plus the one thing validate()
        # can only see across rows.
        problems = lsf.validate([row], [])
        if row.name and row.name in self._taken:
            problems.append("connection 1 (%s): that name is already used." % row.name)
        return problems

    def value(self):
        return lsf.ConnectionRow(
            name=self.name.text().strip(), type=self.type.currentText(),
            host=self.host.text().strip(), user=self.user.text().strip(),
            identity=self.identity.text().strip(), port=self.port.text().strip(),
            options=[o.strip() for o in self.options.text().split(",") if o.strip()],
            extra=dict(self._row.extra),
            # Carried, not renewed: editing a row does not make it a new one, and
            # the page shows these newest first.
            added=self._row.added)


class LogDialog(RowDialog):
    """Create or edit one log: what to read, where it belongs, how to parse it."""

    #: What the project combo calls "belongs to no stack". Never written to the
    #: file - an unassigned log carries no project key at all.
    NO_PROJECT = "(none)"

    def __init__(self, row=None, connections=(), envs=(), siblings=(),
                 developer=False, projects=(), parent=None):
        super().__init__("Log" if row else "New log", parent)
        self._row = (row or lsf.LogRow()).copy()
        self._connections = [c.name for c in connections if c.name]
        self._siblings = list(siblings)
        # Fixed for the life of the dialog: it is opened, answered and closed,
        # so there is no mode switch to follow the way a page has to.
        self._developer = bool(developer)

        self.name = QLineEdit(self._row.name)
        self.name.setPlaceholderText("app, nginx, worker…")
        self.name.textChanged.connect(self._changed)
        self.column.addWidget(widgets.field(
            "Name", self.name,
            "How %s refers to it. Unique per environment, so the same name may "
            "repeat on another stand."
            % ("--server-log" if self._developer else "a run")))

        self.project = QComboBox()
        self.project.addItem(self.NO_PROJECT)
        known_projects = list(projects)
        # A log whose stack has since been renamed or deleted keeps its own answer
        # in the list, so opening the dialog cannot silently reassign it.
        if self._row.project and self._row.project not in known_projects:
            known_projects.append(self._row.project)
        self.project.addItems(known_projects)
        self.project.setCurrentText(self._row.project or self.NO_PROJECT)
        self.project.currentTextChanged.connect(self._changed)
        self.column.addWidget(widgets.field(
            "Stack", self.project,
            "Which block on this page the log appears under. It groups the "
            "display and nothing else - a run streams it either way."))

        self.connection = QComboBox()
        self.connection.addItems(self._connections or [self._row.connection])
        if self._row.connection:
            self.connection.setCurrentText(self._row.connection)
        self.connection.currentTextChanged.connect(self._changed)
        self.column.addWidget(widgets.field(
            "Read it over", self.connection,
            "Add connections above if the one you need is not here."))

        self.envs = widgets.CheckList(searchable=False)
        self.envs.set_noun("environments")
        # Bounded: a QListWidget expands to whatever the layout will give it, and
        # in a scrolling form that is all of it - pushing every field below the
        # fold behind an empty white box.
        self.envs.list.setMinimumHeight(76)
        self.envs.list.setMaximumHeight(120)
        known = list(envs)
        for env in self._row.envs:
            if env not in known:
                known.append(env)
        for env in known:
            self.envs.add(env, env, "")
        self.envs.set_checked(self._row.envs)
        self.envs.changed.connect(self._changed)
        self.column.addWidget(widgets.field(
            "Environments", self.envs,
            "Spelled exactly as in users.json - that string is what ties this log "
            "to the windows opened against it."))

        self.other_envs = QLineEdit("")
        self.other_envs.setPlaceholderText("another environment, comma-separated")
        self.other_envs.textChanged.connect(self._changed)
        self.column.addWidget(widgets.field("Other environments", self.other_envs))

        self.type = QComboBox()
        self.type.addItems(list(lsf.LOG_TYPES))
        self.type.setCurrentText(self._row.type)
        self.type.currentTextChanged.connect(self._type_changed)
        self.column.addWidget(widgets.field("Kind", self.type))

        self.target = QLineEdit(self._row.target)
        self.target.textChanged.connect(self._changed)
        self.target_browse = QPushButton("Browse…")
        self.target_browse.setProperty("variant", "ghost")
        self.target_browse.clicked.connect(self._pick_target)
        self.target_field = widgets.field("Path",
                                          widgets.row(self.target,
                                                      self.target_browse))
        self.column.addWidget(self.target_field)
        self.target_hint = QLabel("")
        self.target_hint.setProperty("role", "hint")
        self.target_hint.setWordWrap(True)
        self.column.addWidget(self.target_hint)

        self.format = QComboBox()
        for shape in lsf.FORMAT_SHAPES:
            self.format.addItem(shape)
        self.format.insertSeparator(self.format.count())
        for alias in sorted(lsf.FORMAT_ALIASES):
            self.format.addItem(alias)
        self.format.insertSeparator(self.format.count())
        self.format.addItem(lsf.CUSTOM_FORMAT)
        self.format.setCurrentText(self._row.format_label())
        self.format.currentTextChanged.connect(self._format_changed)
        self.column.addWidget(widgets.field(
            "Line format", self.format,
            "Presets are named after the shape of a line; the rest are aliases for "
            "one of them. Pick \"custom\" for anything they do not describe."))
        self.format_hint = QLabel("")
        self.format_hint.setProperty("role", "hint")
        self.format_hint.setWordWrap(True)
        self.column.addWidget(self.format_hint)

        self.tz = QComboBox()
        self.tz.setEditable(True)
        self.tz.addItems(["local", "utc"])
        self.tz.setCurrentText(self._row.tz or "local")
        self.tz.currentTextChanged.connect(self._changed)
        self.column.addWidget(widgets.field(
            "Written in", self.tz,
            "The clock the timestamps use, when the line carries no offset of its "
            "own. A backend logging UTC on a machine that is not is why a log can "
            "stream nothing at all."))

        stamp = self._row.timestamp or {}
        self.ts_regex = QLineEdit(stamp.get("regex", ""))
        self.ts_regex.setPlaceholderText(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        self.ts_regex.textChanged.connect(self._changed)
        self.ts_regex_field = widgets.field(
            "Timestamp pattern", self.ts_regex,
            "One capturing group around the timestamp.")
        self.column.addWidget(self.ts_regex_field)

        self.ts_format = QLineEdit(stamp.get("format", "") or "iso")
        self.ts_format.setPlaceholderText("%Y-%m-%d %H:%M:%S   or   iso")
        self.ts_format.textChanged.connect(self._changed)
        self.ts_format_field = widgets.field(
            "Timestamp reading", self.ts_format,
            "A strptime pattern, or \"iso\" / \"clf\" for those shapes.")
        self.column.addWidget(self.ts_format_field)

        self.level_regex = QLineEdit((self._row.level or {}).get("regex", ""))
        self.level_regex.setPlaceholderText(r"\b(DEBUG|INFO|WARN|ERROR)\b")
        self.level_regex.textChanged.connect(self._changed)
        self.level_field = widgets.field(
            "Level pattern", self.level_regex,
            "Optional; one capturing group. Only tints the line.")
        self.column.addWidget(self.level_field)

        self.default = QCheckBox("Stream this one by default")
        self.default.setChecked(self._row.default)
        self.default.setToolTip(
            "%s takes the logs marked default for the environment being launched."
            % ("A bare --server-log" if self._developer
               else "A run that names no log"))
        self.default.toggled.connect(self._changed)
        self.column.addWidget(self.default)

        self.finish_layout()
        self._type_changed(self.type.currentText())
        self._format_changed(self.format.currentText())

    # -- reactions ---------------------------------------------------------
    def _pick_target(self):
        path = widgets.pick_path(self, "Log file", self.target.text().strip()
                                 or "/var/log")
        if path:
            self.target.setText(path)

    def _type_changed(self, value):
        label, hint = TARGET_LABELS.get(value, TARGET_LABELS["file"])
        self.target_field.label.setText(label)
        self.target_hint.setText(hint)
        # Only a local file can be browsed for; a container or a remote path is
        # not on this machine to point at.
        self.target_browse.setVisible(value == "file")
        self._changed()

    def _format_changed(self, value):
        custom = value == lsf.CUSTOM_FORMAT
        for field in (self.ts_regex_field, self.ts_format_field, self.level_field):
            field.setVisible(custom)
        self.format_hint.setText(
            "Your own patterns, for a backend no preset describes." if custom
            else lsf.FORMAT_EXAMPLES.get(lsf.FORMAT_ALIASES.get(value, value), ""))
        self._fit()
        self._changed()

    def _changed(self, *_args):
        self.show_problems(self._problems())

    def _problems(self):
        row = self.value()
        problems = [p for p in lsf.validate([], [row])
                    if "connection" not in p or "no connection named" not in p]
        if not self._connections:
            problems.append("log 1: add a connection first - a log has to be read "
                            "over one.")
        for other in self._siblings:
            shared = set(other.envs) & set(row.envs)
            if row.name and other.name == row.name and shared:
                problems.append("log 1 (%s): that name is already used for %s."
                                % (row.name, ", ".join(sorted(shared))))
                break
        return problems

    def value(self):
        envs = list(self.envs.checked())
        for extra in self.other_envs.text().split(","):
            if extra.strip() and extra.strip() not in envs:
                envs.append(extra.strip())
        custom = self.format.currentText() == lsf.CUSTOM_FORMAT
        timestamp = level = None
        fmt = self._row.format if custom else self.format.currentText()
        if custom:
            timestamp = {"regex": self.ts_regex.text().strip(),
                         "format": self.ts_format.text().strip() or "iso"}
            if self.level_regex.text().strip():
                level = {"regex": self.level_regex.text().strip()}
        return lsf.LogRow(
            name=self.name.text().strip(), connection=self.connection.currentText(),
            envs=envs, type=self.type.currentText(),
            target=self.target.text().strip(), format=fmt,
            default=self.default.isChecked(), headers=dict(self._row.headers),
            timestamp=timestamp, level=level,
            tz=self.tz.currentText().strip() or "local",
            project=("" if self.project.currentText() == self.NO_PROJECT
                     else self.project.currentText()),
            extra=dict(self._row.extra),
            added=self._row.added)


class LogViewerDialog(QDialog):
    """What one log holds right now, read through the core.

    Read-only and one-shot on purpose: this is for looking at a log while setting
    it up - is this the right file, is anything in it, does it look the way the
    format expects. Following it live is what a run does, into the session panels.
    """

    def __init__(self, name, result, whole, parent=None):
        super().__init__(parent)
        self._lines = list(result.get("lines") or [])
        self.setWindowTitle("%s - %s" % (name, "whole log" if whole else "tail"))
        self.resize(1000, 620)
        self.frame = widgets.dress(self, minimizable=True)

        column = QVBoxLayout(self)
        column.setContentsMargins(20, 16, 20, 14)
        column.setSpacing(8)
        column.addWidget(widgets.mono(result.get("target", "")))

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter these lines…")
        self.filter.textChanged.connect(self._repaint)
        self.count = widgets.mono("")
        save = QPushButton("Save…")
        save.setProperty("variant", "ghost")
        save.clicked.connect(self._save)
        column.addWidget(widgets.row(self.filter, self.count, None, save))

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setStyleSheet("font-family: %s; font-size: 12px;" % theme.MONO_CSS)
        column.addWidget(self.view, 1)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        column.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

        if result.get("truncated"):
            self._say("Showing the end of the log - it is longer than this.")
        elif result.get("empty"):
            self._say("The log is readable and empty. That is not a fault.")
        self._repaint()

    def _say(self, text):
        self.note.setText(text)
        self.note.setStyleSheet("color: %s; font-size: 12px;" % theme.NEUTRAL[700])

    def _visible(self):
        needle = self.filter.text().strip().lower()
        if not needle:
            return self._lines
        return [line for line in self._lines if needle in line.lower()]

    def _repaint(self):
        lines = self._visible()
        self.view.setPlainText("\n".join(lines))
        self.count.setText("%d of %d line%s" % (len(lines), len(self._lines),
                                                "" if len(self._lines) == 1 else "s"))
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())          # a log is read from its end

    def _save(self):
        path = widgets.pick_path(self, "Save log", "%s.log"
                                 % self.windowTitle().split(" - ")[0], save=True)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(self._visible()))
        except OSError as exc:
            widgets.warn(self, "Save log", str(exc))
