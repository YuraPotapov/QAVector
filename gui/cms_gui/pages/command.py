"""Command page: every launcher flag as a form, with a live command preview.

The controls are generated from :mod:`cms_gui.commands`, so this file lays out
groups rather than enumerating flags. The one rule it enforces on top of the
catalogue is the launcher's own: the flow-execution and report flags are
rejected without ``--run-tests``, so they stay disabled until it is filled in
and an invalid line simply cannot be built here.
"""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from .. import commands, icons, launch, theme, widgets


class ScenarioPicker(QDialog):
    """Pick scenarios and tags for --run-tests, from what --describe reported."""

    def __init__(self, inventory, current="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose scenarios")
        self.resize(620, 560)
        self.frame = widgets.dress(self)
        chosen = {part.strip() for part in current.split(",") if part.strip()}

        column = QVBoxLayout(self)
        column.setContentsMargins(18, 16, 18, 14)
        column.addWidget(widgets.heading("Scenarios", "h2"))
        column.addWidget(widgets.lede(
            "Ticked entries become --run-tests. A tag selector pulls in every "
            "scenario carrying it, template and manual ones included."))

        self.list = QListWidget()
        self.list.setAlternatingRowColors(False)
        for tag in inventory.tags:
            self._add("tag:" + tag, "tag:%s" % tag, chosen, is_tag=True)
        for scenario in inventory.scenarios:
            label = scenario.get("id", "")
            note = scenario.get("name") or ""
            if not scenario.get("in_all", True):
                note += "   (not in --run-tests=all)"
            self._add(label, note, chosen)
        column.addWidget(self.list, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

    def _add(self, value, note, chosen, is_tag=False):
        item = QListWidgetItem("%-38s %s" % (value, note))
        item.setFont(theme.mono_font(9))
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if value in chosen else Qt.Unchecked)
        item.setData(Qt.UserRole, value)
        if is_tag:
            item.setForeground(theme.color(theme.ACCENT_RAMP[700]))
        self.list.addItem(item)

    def selection(self):
        values = []
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.checkState() == Qt.Checked:
                values.append(item.data(Qt.UserRole))
        return ",".join(values)


class CommandPage(QWidget):
    """The form, the preview, and the Run button's source of truth."""

    run_requested = Signal()
    state_changed = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.inventory = None
        self.core = None
        self._controls = {}     # flag name -> the editing widget
        self._flag_rows = {}    # flag name -> the whole row it lives in
        self._building = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(24, 20, 24, 20)
        column.setSpacing(6)
        column.addWidget(widgets.heading("Command"))
        column.addWidget(widgets.lede(
            "Every flag the launcher accepts, grouped as --help groups them. The "
            "flow-execution and report flags stay disabled until --run-tests is set, "
            "so an invalid command cannot be built."))
        column.addSpacing(14)

        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(20)
        grid.addWidget(self._group(commands.GENERAL), 0, 0, 2, 1)
        grid.addWidget(self._group(commands.FLOW), 0, 1)
        grid.addWidget(self._group(commands.REPORTS), 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)
        column.addLayout(grid)
        column.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # --- preview strip (the design's footer bar) -------------------------
        footer = QFrame()
        footer.setProperty("role", "footer")
        strip = QHBoxLayout(footer)
        strip.setContentsMargins(24, 10, 24, 10)
        strip.setSpacing(14)
        strip.addWidget(widgets.kicker("preview"))
        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview.setProperty("role", "preview")
        strip.addWidget(self.preview, 1)
        self.copy_button = icons.button(QPushButton("Copy"), "copy")
        self.copy_button.clicked.connect(self.copy_command)
        strip.addWidget(self.copy_button)
        # theme.BG, not the default ink: a primary button paints its label in
        # the background colour against the accent fill, and the mark beside it
        # has to be the same colour or it reads as a different control.
        self.run_button = icons.button(QPushButton("RUN"), "run", color=theme.BG)
        self.run_button.setProperty("variant", "primary")
        self.run_button.clicked.connect(self.run_requested.emit)
        strip.addWidget(self.run_button)
        outer.addWidget(footer)

        self._restore()

    # -- construction ---------------------------------------------------------
    def _group(self, name):
        panel = widgets.BlueprintPanel(padding=(16, 16, 16, 18))
        panel.layout().addWidget(widgets.kicker(name))
        for flag in commands.flags_for(name):
            row = self._control(flag)
            # Remember the whole row - label, control and any browse/pick button.
            # Enabling and disabling happens here and NEVER on a parentWidget():
            # a bare checkbox's parent is the panel itself, so disabling that
            # would grey out --run-tests too and make the group unreachable.
            self._flag_rows[flag.name] = row
            panel.layout().addWidget(row)
        panel.layout().addStretch(1)
        return panel

    def _control(self, flag):
        """One row of the form, chosen by the flag's kind."""
        if flag.kind == "flag":
            box = QCheckBox("%s   %s" % (flag.name, flag.help))
            box.setToolTip(flag.help)
            box.toggled.connect(self._changed)
            self._controls[flag.name] = box
            return box

        if flag.kind == "choice":
            combo = QComboBox()
            combo.setProperty("mono", True)
            combo.setEditable(flag.name == "--env")
            combo.addItem(flag.placeholder or "")
            for choice in flag.choices or []:
                combo.addItem(choice)
            if flag.default:
                combo.setCurrentText(flag.default)
            combo.currentTextChanged.connect(self._changed)
            self._controls[flag.name] = combo
            return widgets.field(flag.name, combo, flag.help)

        edit = QLineEdit()
        edit.setProperty("mono", True)
        edit.setPlaceholderText(flag.placeholder or (flag.default or ""))
        if flag.default:
            edit.setText(flag.default)
        edit.textChanged.connect(self._changed)
        self._controls[flag.name] = edit

        if flag.kind == "path":
            browse = icons.button(QPushButton(""), "browse")
            browse.setFixedWidth(38)
            browse.clicked.connect(lambda _c=False, e=edit: self._browse(e))
            return widgets.field(flag.name, widgets.row(edit, browse), flag.help)
        if flag.name == "--run-tests":
            pick = QPushButton("Pick…")
            pick.clicked.connect(self._pick_scenarios)
            return widgets.field(flag.name, widgets.row(edit, pick), flag.help)
        if flag.kind == "list" and flag.choices:
            pick = icons.button(QPushButton(""), "browse")
            pick.setFixedWidth(38)
            pick.clicked.connect(lambda _c=False, f=flag: self._pick_list(f))
            return widgets.field(flag.name, widgets.row(edit, pick), flag.help)
        return widgets.field(flag.name, edit, flag.help)

    # -- state ----------------------------------------------------------------
    def state(self):
        values = {}
        for name, control in self._controls.items():
            if isinstance(control, QCheckBox):
                values[name] = control.isChecked()
            elif isinstance(control, QComboBox):
                text = control.currentText().strip()
                flag = commands.BY_NAME[name]
                values[name] = "" if text == (flag.placeholder or "") else text
            else:
                values[name] = control.text().strip()
        return values

    def set_state(self, state):
        self._building = True
        for name, value in (state or {}).items():
            control = self._controls.get(name)
            if control is None:
                continue
            if isinstance(control, QCheckBox):
                control.setChecked(bool(value))
            elif isinstance(control, QComboBox):
                control.setCurrentText(str(value or ""))
            else:
                control.setText(str(value or ""))
        self._building = False
        self._changed()

    def argv(self):
        return commands.build_argv(self.state())

    def background_blockers(self):
        """What stops this command running with no browser; [] if nothing.

        ``config`` means each launched account's own list, so which accounts that
        is has to be read off the form the way the launcher will read it.
        """
        state = self.state()
        users = str(state.get("--filter-users") or "").strip()
        if state.get("--user"):
            logins = [state["--user"]]
        elif users and users != "all":
            logins = [login.strip() for login in users.split(",") if login.strip()]
        elif self.inventory:
            alias = state.get("--env") or ""
            logins = self.inventory.logins(
                self.inventory.env_value(alias) if alias else None)
        else:
            logins = None
        return launch.background_blockers(state.get("--run-tests"), self.inventory,
                                          logins)

    def _changed(self, *_args):
        if self._building:
            return
        state = self.state()
        run_tests = bool(str(state.get("--run-tests", "")).strip())
        for flag in commands.FLAGS:
            if flag.needs_run_tests:
                row = self._flag_rows.get(flag.name)
                if row is not None:
                    row.setEnabled(run_tests)
        self.preview.setText(commands.preview(state, self.core))
        self.settings.save_command_state(state)
        self.state_changed.emit()

    def _restore(self):
        saved = self.settings.command_state()
        if saved:
            self.set_state(saved)
        else:
            self._changed()

    # -- context --------------------------------------------------------------
    def set_core(self, core):
        self.core = core
        self._changed()

    def set_inventory(self, inventory):
        self.inventory = inventory
        combo = self._controls.get("--env")
        if isinstance(combo, QComboBox):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(all environments)")
            for alias in inventory.env_aliases():
                combo.addItem(alias)
            combo.setCurrentText(current)
            combo.blockSignals(False)
        # Directory defaults the Environments page owns.
        for key, name in (("flows", "--flows-dir"), ("reports", "--reports-dir"),
                          ("sessions", "--sessions-dir")):
            control = self._controls.get(name)
            value = self.settings.directory(key)
            if isinstance(control, QLineEdit) and value and not control.text():
                control.setText(value)
        self._changed()

    # -- helpers --------------------------------------------------------------
    def _browse(self, edit):
        start = edit.text() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose a directory", start)
        if chosen:
            edit.setText(chosen)

    def _pick_scenarios(self):
        if self.inventory is None:
            return
        edit = self._controls["--run-tests"]
        dialog = ScenarioPicker(self.inventory, edit.text(), self)
        if dialog.exec() == QDialog.Accepted:
            edit.setText(dialog.selection())

    def _pick_list(self, flag):
        """Tick-list for a comma-separated flag whose values are known."""
        edit = self._controls[flag.name]
        chosen = {p.strip() for p in edit.text().split(",") if p.strip()}
        dialog = QDialog(self)
        dialog.setWindowTitle(flag.name)
        column = QVBoxLayout(dialog)
        column.addWidget(widgets.lede(flag.help))
        listing = QListWidget()
        for choice in flag.choices:
            item = QListWidgetItem(choice)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if choice in chosen else Qt.Unchecked)
            listing.addItem(item)
        column.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        column.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            picked = [listing.item(i).text() for i in range(listing.count())
                      if listing.item(i).checkState() == Qt.Checked]
            edit.setText(",".join(picked))

    def copy_command(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.preview.text())
        icons.button(self.copy_button, "pass", "Copied")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1400, lambda: icons.button(self.copy_button, "copy", "Copy"))

    def set_running(self, running):
        self.run_button.setEnabled(not running)
