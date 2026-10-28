"""Environments page: projects, their environments, the variables an address is
written with - and the per-run directories.

Environments are not a thing the launcher stores anywhere - they are *derived*
from the distinct ``env`` values in users.json (session_launcher.build_environments),
which is why this page reads them from ``--describe`` and edits them through the
config file rather than through some GUI-only registry.

An environment's own address has no home in the core: it lives in
launch-projects.json (:mod:`cms_gui.launchprojectsfile`), filed under the
environment's id, and is applied wherever a run is built - Launch Sessions and
the Command page both open it in place of the origin the core derives from the
env value. It may carry variables (``http://localhost:{{port}}``). Every
users.json value is listed whether or not anyone has set something for it; the
first change writes it down. The default directories live in QSettings.

Projects group environments; a project's variables are what its addresses may
use (``port`` is a port, with a default), and each environment gives them its
own values. The forms are in :mod:`cms_gui.pages.environmentdialog`.
"""

import os
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog,
                               QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import icons, launch, launchprojectsfile as lpf, launchvars, search, theme, widgets
from .environmentdialog import EnvironmentDialog, VariableDialog


class EnvironmentsPage(QWidget):
    """Read-only inventory of environments + the GUI's own per-env extras."""

    directories_changed = Signal()
    #: An environment's address was set, changed or cleared.
    addresses_changed = Signal()

    HEADERS = ["Environment", "env value (users.json)", "Origin", "Users", "Address",
               "Variables"]
    VARIABLE_HEADERS = ["Variable", "Type", "Default"]

    def __init__(self, settings, parent=None, registry=None):
        super().__init__(parent)
        self.settings = settings
        self.registry = registry or lpf.Registry(
            lpf.resolve_path(settings.launch_projects_path))
        self._envs = []
        self._inventory = None
        self._project_id = lpf.WORKSPACE

        # Scrolled, as Launch Sessions is: projects, environments, variables and
        # folders stacked are taller than a laptop screen, and a page that
        # cannot scroll makes the whole window refuse to fit.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(24, 20, 24, 20)
        column.setSpacing(6)
        self.wording = widgets.Phrasing()
        column.addWidget(widgets.heading("Environments"))
        column.addWidget(self.wording.text(
            widgets.lede(""),
            "Read from the launcher. The env value and the user rows live in "
            "users.json. An address typed here is where every run of that "
            "environment opens instead of its origin; it and the default "
            "directories are stored by the GUI.",
            "Read from --describe. The env value and the user rows live in users.json. "
            "An address here is passed as --url for that --env; it and the default "
            "directories are stored by the GUI."))
        column.addSpacing(12)

        # Which project's environments: Workspace holds every environment
        # nobody has put in another one.
        self.project_combo = widgets.capped(QComboBox())
        self.project_combo.activated.connect(self._project_picked)
        add_project = QPushButton("+ Project")
        add_project.clicked.connect(self.add_project)
        self.rename_project_button = QPushButton("Rename…")
        self.rename_project_button.setProperty("variant", "ghost")
        self.rename_project_button.clicked.connect(self.rename_project)
        self.delete_project_button = QPushButton("Delete…")
        self.delete_project_button.setProperty("variant", "ghost")
        self.delete_project_button.clicked.connect(self.delete_project)
        project_row = widgets.row(widgets.field("Project", self.project_combo),
                                  add_project, self.rename_project_button,
                                  self.delete_project_button, None)
        # Level with the combo, not centred on it and its label together.
        widgets.level_with(self.project_combo, project_row, add_project,
                           self.rename_project_button, self.delete_project_button)
        self.project_row = project_row
        column.addWidget(project_row)
        self.project_hint = QLabel("Put environments that belong together - one "
                                   "application's stands and ports - in a project "
                                   "of their own with + Project.")
        self.project_hint.setProperty("role", "hint")
        self.project_hint.setWordWrap(True)
        column.addWidget(self.project_hint)
        # The file cannot be read: said above what it would have changed, and
        # nothing here is changeable until it reads.
        self.registry_problem = QLabel("")
        self.registry_problem.setProperty("role", "error")
        self.registry_problem.setWordWrap(True)
        self.registry_problem.setVisible(False)
        column.addWidget(self.registry_problem)
        column.addSpacing(6)

        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.table.horizontalHeader()
        for index in range(len(self.HEADERS)):
            header.setSectionResizeMode(index, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setMinimumHeight(140)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.doubleClicked.connect(lambda _index: self.edit_environment())
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self._env_note = widgets.empty_note(
            self.table, "No environments in this project yet - + Environment adds "
                        "one, or move one here from another project.")
        panel.layout().addWidget(self.table)
        column.addWidget(panel)
        self.add_env_button = QPushButton("+ Environment")
        self.add_env_button.clicked.connect(self.add_environment)
        self.edit_env_button = QPushButton("Edit…")
        self.edit_env_button.clicked.connect(self.edit_environment)
        self.move_env_button = QPushButton("Move to…")
        self.move_env_button.setToolTip("Put this environment in another project")
        self.move_env_button.clicked.connect(self.move_environment)
        self.extract_button = QPushButton("Extract port…")
        self.extract_button.setToolTip("Turn the port in its address into a {{port}} "
                                       "variable, so other ports are one value away")
        self.extract_button.clicked.connect(self.extract_port)
        self.delete_env_button = QPushButton("Delete…")
        self.delete_env_button.setProperty("variant", "ghost")
        self.delete_env_button.clicked.connect(self.delete_environment)
        column.addWidget(widgets.row(self.add_env_button, self.edit_env_button,
                                     self.move_env_button, self.extract_button, None,
                                     self.delete_env_button))

        column.addSpacing(12)
        column.addWidget(widgets.kicker("Variables of this project"))
        variables_panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.variables = widgets.Table(0, len(self.VARIABLE_HEADERS))
        self.variables.setHorizontalHeaderLabels(self.VARIABLE_HEADERS)
        self.variables.verticalHeader().setVisible(False)
        self.variables.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.variables.setSelectionMode(QAbstractItemView.SingleSelection)
        self.variables.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.variables.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.variables.setMinimumHeight(96)
        self.variables.doubleClicked.connect(lambda _index: self.edit_variable())
        self.variables.itemSelectionChanged.connect(self._update_buttons)
        widgets.empty_note(self.variables,
                           "No variables yet. Write {{port}} in an address and define "
                           "it there, or add one here.")
        variables_panel.layout().addWidget(self.variables)
        column.addWidget(variables_panel)
        self.add_variable_button = QPushButton("+ Variable")
        self.add_variable_button.clicked.connect(self.add_variable)
        self.edit_variable_button = QPushButton("Edit…")
        self.edit_variable_button.clicked.connect(self.edit_variable)
        self.remove_variable_button = QPushButton("Remove…")
        self.remove_variable_button.setProperty("variant", "ghost")
        self.remove_variable_button.clicked.connect(self.remove_variable)
        column.addWidget(widgets.row(self.add_variable_button,
                                     self.edit_variable_button, None,
                                     self.remove_variable_button))

        # Addresses the old "URL override" column saved and nothing ever
        # applied. Switching them on unasked would move where runs open, so each
        # is offered instead - and the block is not there at all without one.
        self.legacy_box = QWidget()
        self.legacy_rows = QVBoxLayout(self.legacy_box)
        self.legacy_rows.setContentsMargins(0, 8, 0, 0)
        self.legacy_rows.setSpacing(4)
        column.addWidget(self.legacy_box)
        self.legacy_box.setVisible(False)

        column.addSpacing(18)
        column.addWidget(widgets.kicker("Default directories"))
        column.addWidget(self.wording.text(
            widgets.lede(""),
            "Where every run reads and writes. Empty means the launcher's own default.",
            "Passed to every run as --flows-dir / --reports-dir / --sessions-dir. "
            "Empty means the core's own default."))
        column.addSpacing(6)

        self.dir_edits = {}
        dirs = QHBoxLayout()
        dirs.setSpacing(16)
        for key, plain, flag in (("flows", "Flows", "--flows-dir"),
                                 ("reports", "Reports", "--reports-dir"),
                                 ("sessions", "Sessions", "--sessions-dir")):
            edit = QLineEdit(self.settings.directory(key))
            edit.setProperty("mono", True)
            edit.setPlaceholderText("(default)")
            edit.editingFinished.connect(
                lambda k=key, e=edit: self._save_directory(k, e.text()))
            browse = icons.button(QPushButton(""), "browse")
            browse.setFixedWidth(38)
            browse.clicked.connect(lambda _c=False, k=key, e=edit: self._browse(k, e))
            self.dir_edits[key] = edit
            box = widgets.field(plain, widgets.row(edit, browse, stretch_last=False))
            self.wording.text(box.label, plain, flag)
            dirs.addWidget(box, 1)
        column.addLayout(dirs)
        column.addStretch(1)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll)

    def set_developer_mode(self, enabled):
        self.wording.apply(enabled)

    # -- data -----------------------------------------------------------------
    def set_inventory(self, inventory):
        self._inventory = inventory
        self.registry.reload()
        self._fill_projects()
        self._envs = [env for env in self.registry.environments(inventory)
                      if env.project_id == self._project_id]
        # The address editors of the rows being replaced go now, not whenever
        # their deferred deletion comes round: until then they would still be
        # drawn, over the rows that took their place.
        for row in range(self.table.rowCount()):
            editor = self.table.cellWidget(row, 4)
            if editor is not None:
                editor.hide()
                self.table.removeCellWidget(row, 4)
        self.table.setRowCount(len(self._envs))
        for row, env in enumerate(self._envs):
            self._set(row, 0, env.name, mono=True)
            self._set(row, 1, env.account_key, mono=True)
            self._set(row, 2, env.origin or "(no URL)", mono=True,
                      color=theme.ACCENT_RAMP[700] if env.origin
                      else theme.NEUTRAL[600])
            self._set(row, 3, str(env.accounts))
            override = QLineEdit(env.address)
            override.setProperty("mono", True)
            override.setPlaceholderText(env.origin or "type the address")
            override.editingFinished.connect(
                lambda i=env.id, e=override: self._save_address(i, e.text()))
            self.table.setCellWidget(row, 4, override)
            values = ", ".join("%s=%s" % (name, value)
                               for name, value in sorted(env.variables.items()))
            self._set(row, 5, values or "—", mono=True,
                      color=None if values else theme.NEUTRAL[600])
        self._fill_variables()
        self._fit_rows_to_the_override()
        self._show_legacy()
        self.table.resizeColumnsToContents()
        if self.table.columnWidth(4) < 240:
            self.table.setColumnWidth(4, 240)
        # Directory defaults follow the core unless the user set their own.
        for key, value in inventory.dirs().items():
            edit = self.dir_edits.get(key)
            if edit is not None and not edit.text():
                edit.setPlaceholderText(value or "(default)")
        self.registry_problem.setText(
            "launch-projects.json cannot be read, so nothing here can be changed until "
            "it can: %s" % self.registry.error if self.registry.error else "")
        self.registry_problem.setVisible(bool(self.registry.error))
        self._update_buttons()

    def _fit_rows_to_the_override(self):
        """Make a row tall enough for the editor it holds.

        Every other column is text, which a row of any height can draw. The URL
        override is a real QLineEdit, and the design's inputs are 30px - taller
        than a row of text - while the view insets a cell widget by the item
        padding on both sides. In a row sized for text the editor was handed
        24px, rendered at its own 30 regardless, and the extra hung *downwards*:
        3px of air above it and 3px of it lying across the gridline below, which
        is the lopsidedness you could see rather than measure.

        Measured, not written down: how tall an input comes out is the body
        font's doing and the stylesheet's, and neither is fixed here.
        """
        tallest = 0
        for row in range(self.table.rowCount()):
            editor = self.table.cellWidget(row, 4)
            if editor is None:
                continue
            editor.ensurePolished()
            tallest = max(tallest, editor.minimumSizeHint().height())
        if tallest:
            self.table.verticalHeader().setDefaultSectionSize(
                tallest + 2 * theme.CELL_INSET_V)

    def _set(self, row, col, text, mono=False, color=None):
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if mono:
            item.setFont(theme.mono_font(9))
        if color:
            item.setForeground(theme.color(color))
        self.table.setItem(row, col, item)

    def address_for(self, env_id):
        """The address set for an environment; "" when it follows its origin."""
        env = lpf.find(self.registry.environments(self._inventory), env_id)
        return env.address if env is not None else ""

    def _save_address(self, env_id, text):
        text = (text or "").strip()
        env = lpf.find(self.registry.environments(self._inventory), env_id)
        if env is None or text == env.address:
            return

        def change(projects):
            lpf.environment_row(projects, env).address = text
        try:
            self.registry.update(change)
        except lpf.LaunchProjectsError as exc:
            widgets.warn(self, "Environments", "The address was not saved.", str(exc))
            return
        self.addresses_changed.emit()

    def _show_legacy(self):
        """One line per old override still waiting: apply it, or let it go."""
        while self.legacy_rows.count():
            item = self.legacy_rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        by_alias = {env.name: env.id for env in self._envs}
        shown = 0
        for alias, url in sorted(self.settings.legacy_env_overrides().items()):
            value = by_alias.get(alias, "")
            note = QLabel("Saved earlier for \"%s\" but never applied: %s"
                          % (alias, url))
            note.setProperty("role", "hint")
            apply = QPushButton("Apply")
            apply.setProperty("variant", "ghost")
            apply.setEnabled(bool(value))
            apply.setToolTip("Use it as this environment's address" if value else
                             "No environment is called that any more")
            apply.clicked.connect(lambda _c=False, a=alias, v=value, u=url:
                                  self._adopt_legacy(a, v, u))
            discard = QPushButton("Discard")
            discard.setProperty("variant", "ghost")
            discard.clicked.connect(lambda _c=False, a=alias: self._drop_legacy(a))
            self.legacy_rows.addWidget(widgets.row(note, None, apply, discard))
            shown += 1
        self.legacy_box.setVisible(bool(shown))

    def _adopt_legacy(self, alias, env_id, url):
        env = lpf.find(self.registry.environments(self._inventory), env_id)
        if env is None:
            return

        def change(projects):
            lpf.environment_row(projects, env).address = url
        try:
            self.registry.update(change)
        except lpf.LaunchProjectsError as exc:
            widgets.warn(self, "Environments", "The address was not saved.", str(exc))
            return
        self.settings.discard_legacy_env_override(alias)
        self.set_inventory_rows()
        self.addresses_changed.emit()

    def _drop_legacy(self, alias):
        self.settings.discard_legacy_env_override(alias)
        self._show_legacy()

    def set_inventory_rows(self):
        """Redraw the table from the inventory last given."""
        if self._inventory is not None:
            self.set_inventory(self._inventory)

    # -- projects -------------------------------------------------------------
    def _all_environments(self):
        return self.registry.environments(self._inventory)

    def _fill_projects(self):
        names = lpf.project_names(self.registry.projects, self._all_environments())
        if self._project_id not in {pid for pid, _name in names}:
            self._project_id = names[0][0] if names else lpf.WORKSPACE
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        for index, (project_id, name) in enumerate(names):
            self.project_combo.addItem(name, project_id)
            if project_id == self._project_id:
                self.project_combo.setCurrentIndex(index)
        self.project_combo.blockSignals(False)
        # The hint says what projects are for until there is one.
        self.project_hint.setVisible(all(pid == lpf.WORKSPACE for pid, _n in names))

    def _project_picked(self, index):
        project_id = self.project_combo.itemData(index)
        if project_id and project_id != self._project_id:
            self._project_id = project_id
            self.set_inventory_rows()

    def _project(self):
        return self.registry.project(self._project_id)

    def _change(self, change, what):
        """Apply ``change`` to launch-projects.json; say so when it cannot be."""
        try:
            result = self.registry.update(change)
        except lpf.LaunchProjectsError as exc:
            widgets.warn(self, "Environments", "%s was not saved." % what, str(exc))
            return False, None
        self.set_inventory_rows()
        self.addresses_changed.emit()
        return True, result

    def add_project(self):
        name = widgets.ask_text(self, "Add a project", "Name the project:")
        if not name:
            return
        project_id = lpf.new_id("p")
        done, _result = self._change(
            lambda projects: projects.append(lpf.ProjectRow(project_id, name.strip())),
            "The project")
        if done:
            self._project_id = project_id
            self.set_inventory_rows()

    def rename_project(self):
        project = self._project()
        name = widgets.ask_text(self, "Rename project", "New name:",
                                project.name if project else "")
        if not name or project is None or name == project.name:
            return

        def change(projects):
            lpf.project_row(projects, self._project_id).name = name.strip()
        self._change(change, "The name")

    def delete_project(self):
        """A project goes; its environments go to Workspace, nothing is lost."""
        project = self._project()
        if project is None or self._project_id == lpf.WORKSPACE:
            return
        if not widgets.confirm(
                self, "Delete project", "Delete \"%s\"?" % project.name,
                "Its %d environment(s) and variables move to Workspace; their "
                "accounts and configurations stay as they are."
                % len(project.environments), agree="Delete", kind="warn"):
            return
        gone = self._project_id

        def change(projects):
            source = lpf.project_row(projects, gone)
            target = lpf.project_row(projects, lpf.WORKSPACE)
            for name, spec in source.variables.items():
                target.variables.setdefault(name, spec)
            target.environments.extend(source.environments)
            projects.remove(source)
        done, _result = self._change(change, "The project")
        if done:
            self._project_id = lpf.WORKSPACE
            self.set_inventory_rows()

    # -- environments ---------------------------------------------------------
    def _selected_env(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() \
            else []
        row = rows[0].row() if rows else -1
        return self._envs[row] if 0 <= row < len(self._envs) else None

    def _account_keys(self):
        keys = [env.get("value", "") for env in
                (self._inventory.envs if self._inventory is not None else [])]
        keys += [env.account_key for env in self._all_environments()]
        return sorted({key for key in keys if key})

    def _dialog(self, environment=None):
        project = self._project() or lpf.ProjectRow(lpf.WORKSPACE, lpf.WORKSPACE_NAME)
        return EnvironmentDialog(
            self, project, environment, account_keys=self._account_keys(),
            taken_names=[env.name for env in self._envs],
            known_keys=[env.get("value", "") for env in
                        (self._inventory.envs if self._inventory is not None else [])])

    def add_environment(self):
        dialog = self._dialog()
        if dialog.exec() != QDialog.Accepted:
            return
        name, key, address, values = dialog.result_values()
        project_id = self._project_id

        def change(projects):
            lpf.project_row(projects, project_id).environments.append(
                lpf.EnvironmentRow(lpf.new_id("e"), name, key, address, values))
        self._change(change, "The environment")

    def edit_environment(self):
        env = self._selected_env()
        if env is None:
            return
        dialog = self._dialog(env)
        if dialog.exec() != QDialog.Accepted:
            return
        name, key, address, values = dialog.result_values()

        def change(projects):
            row = lpf.environment_row(projects, env)
            row.name, row.account_key, row.address = name, key, address
            row.variables = values
        self._change(change, "The environment")

    def move_environment(self):
        env = self._selected_env()
        if env is None:
            return
        targets = [(pid, name) for pid, name in
                   lpf.project_names(self.registry.projects, self._all_environments())
                   if pid != env.project_id]
        if not targets:
            widgets.note(self, "Move environment", "There is no other project to "
                         "move it to yet - add one with + Project.")
            return
        chosen = widgets.ask_item(self, "Move environment",
                                  "Move \"%s\" to:" % env.name,
                                  [name for _pid, name in targets])
        if chosen is None:
            return
        target_id = targets[[name for _pid, name in targets].index(chosen)][0]

        def change(projects):
            row = lpf.environment_row(projects, env)
            source = next(project for project in projects if row in project.environments)
            target = lpf.project_row(projects, target_id)
            # The variables it gives values to go with it.
            for name in row.variables:
                if name in source.variables:
                    target.variables.setdefault(name, dict(source.variables[name]))
            source.environments.remove(row)
            target.environments.append(row)
        self._change(change, "The environment")

    def delete_environment(self):
        env = self._selected_env()
        if env is None or env.virtual:
            return
        if not widgets.confirm(
                self, "Delete environment", "Delete \"%s\"?" % env.name,
                "Its accounts stay in users.json. While they are there it is listed "
                "again, as it was before anything was set for it.",
                agree="Delete", kind="warn"):
            return

        def change(projects):
            for project in projects:
                project.environments = [row for row in project.environments
                                        if row.id != env.id]
        self._change(change, "The change")

    def extract_port(self):
        """``…:8069/…`` becomes ``…:{{port}}/…``, port 8069 here."""
        env = self._selected_env()
        if env is None:
            return
        address = env.address or env.origin
        url, _problem = launch.normalize_address(address) if "{{" not in address \
            else ("", "")
        try:
            port = urlsplit(url).port if url else None
        except ValueError:
            port = None
        if not port:
            widgets.note(self, "Extract port", "There is no port in %s to make a "
                         "variable of." % (address or "this environment's address"))
            return
        written = address if "://" in address else url
        template = written.replace(":%d" % port, ":{{port}}", 1)
        if not widgets.confirm(
                self, "Extract port",
                "%s\nbecomes\n%s" % (written, template),
                "port is %d here; another port of the same environment is then one "
                "value away - on Launch Sessions, for one run or kept." % port,
                agree="Extract"):
            return
        project_id = env.project_id

        def change(projects):
            project = lpf.project_row(projects, project_id)
            if "port" not in project.variables:
                lpf.define(projects, project_id, "port", "port")
            row = lpf.environment_row(projects, env)
            row.address = template
            row.variables["port"] = port
        self._change(change, "The address")

    # -- variables ------------------------------------------------------------
    def _fill_variables(self):
        project = self._project()
        definitions = project.definitions() if project is not None else {}
        self.variables.setRowCount(len(definitions))
        for row, name in enumerate(sorted(definitions)):
            definition = definitions[name]
            default = "—" if definition.default is None else \
                launchvars.text_of(definition, definition.default)
            for column, text in enumerate((name, definition.type, default)):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if column != 1:
                    item.setFont(theme.mono_font(9))
                self.variables.setItem(row, column, item)
        self._variable_names = sorted(definitions)

    def _selected_variable(self):
        rows = self.variables.selectionModel().selectedRows() \
            if self.variables.selectionModel() else []
        row = rows[0].row() if rows else -1
        names = getattr(self, "_variable_names", [])
        return names[row] if 0 <= row < len(names) else ""

    def add_variable(self):
        project = self._project() or lpf.ProjectRow(lpf.WORKSPACE, lpf.WORKSPACE_NAME)
        dialog = VariableDialog(self, project, taken=list(project.variables))
        if dialog.exec() != QDialog.Accepted:
            return
        name, kind, default = dialog.result_values()
        self._change(lambda projects: lpf.define(projects, self._project_id, name,
                                                 kind, default), "The variable")

    def edit_variable(self):
        name = self._selected_variable()
        project = self._project()
        if not name or project is None:
            return
        definition = project.definitions()[name]
        dialog = VariableDialog(self, project, name, definition.type, definition.default)
        if dialog.exec() != QDialog.Accepted:
            return
        _name, kind, default = dialog.result_values()
        self._change(lambda projects: lpf.define(projects, self._project_id, name,
                                                 kind, default), "The variable")

    def remove_variable(self):
        name = self._selected_variable()
        project = self._project()
        if not name or project is None:
            return
        using = [env.name for env in self._envs
                 if name in launchvars.references(env.address)]
        if not widgets.confirm(
                self, "Remove variable", "Remove %s from %s?" % (name, project.name),
                ("%s still use it in their address, and will say {{%s}} is not "
                 "defined until it is changed." % (", ".join(using), name)) if using
                else "Every environment's value for it goes too.",
                agree="Remove", kind="warn"):
            return

        def change(projects):
            target = lpf.project_row(projects, self._project_id)
            target.variables.pop(name, None)
            for row in target.environments:
                row.variables.pop(name, None)
        self._change(change, "The change")

    def _update_buttons(self):
        readable = not self.registry.error
        env = self._selected_env()
        for button in (self.edit_env_button, self.move_env_button,
                       self.extract_button):
            button.setEnabled(readable and env is not None)
        self.delete_env_button.setEnabled(readable and env is not None
                                          and not env.virtual)
        self.add_env_button.setEnabled(readable)
        variable = self._selected_variable()
        self.add_variable_button.setEnabled(readable)
        self.edit_variable_button.setEnabled(readable and bool(variable))
        self.remove_variable_button.setEnabled(readable and bool(variable))
        workspace = self._project_id == lpf.WORKSPACE
        self.rename_project_button.setEnabled(readable)
        self.delete_project_button.setEnabled(readable and not workspace)

    def directories(self):
        return {key: edit.text().strip() for key, edit in self.dir_edits.items()}

    # -- search (Ctrl+K) -------------------------------------------------------
    def search_rows(self):
        """Every environment: its name, where it is, how many accounts it has."""
        return [search.hit(
            env.name,
            " · ".join(part for part in (env.address or env.origin or "",
                                         "%s accounts" % env.accounts) if part),
            keys=env.account_key, target=env.id)
            for env in self._envs]

    def show_found(self, env_id):
        for row, env in enumerate(self._envs):
            if env.id == env_id or env.name == env_id:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
                return True
        return False

    # -- editing --------------------------------------------------------------
    def _save_directory(self, key, value):
        self.settings.set_directory(key, value.strip())
        self.directories_changed.emit()

    def _browse(self, key, edit):
        start = edit.text() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose a directory", start)
        if chosen:
            edit.setText(chosen)
            self._save_directory(key, chosen)
