"""Environments page: each environment's variables and the users who sign in
there, the way Postman keeps environments.

On the left, the environments of a project; on the right, the one chosen: its
name, its variables - ``url`` first, where its windows open, written with the
others if it likes (``http://localhost:{{port}}/web``) - and its users with the
scenarios each runs there.

Environments and their variables live in launch-projects.json
(:mod:`cms_gui.launchprojectsfile`); users live in users.json, filed under the
environment's account key (:mod:`cms_gui.usersfile`), which nobody types and
nothing here shows outside developer wording. A change on this page is saved
as it is made: a variable when its cell is left, users when the picker closes.

Every users.json value is listed whether or not anyone has set something for
it - as an environment named after its alias that opens its origin - and the
first change writes it down. Projects are folders of environments.
"""

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
                               QPushButton, QScrollArea, QStackedWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import launchprojectsfile as lpf, launchvars, search, theme, usersfile, widgets
from .environmentdialog import EnvironmentDialog, UsersPicker, opens_text

#: What an empty cell says (the table recipe's dash).
NONE = "—"


class EnvironmentsPage(QWidget):
    """See the module docstring."""

    #: An environment's url or variables were set, changed or cleared.
    addresses_changed = Signal()
    #: users.json was written from here (who is in an environment, what they run).
    accounts_changed = Signal()
    #: File -> Export with this ticked: {"projects": [id]} or {"environments": [id]}.
    export_requested = Signal(dict)
    #: "add", "rename", "export" or "delete" - done to the window's project.
    project_action = Signal(str)

    HEADERS = ["Environment", "Users"]
    VARIABLE_HEADERS = ["Variable", "Value"]
    USER_HEADERS = ["Role", "Login", "Scenarios"]

    #: The width of the list of environments beside the editor.
    LIST_WIDTH = 300

    def __init__(self, settings, parent=None, registry=None):
        super().__init__(parent)
        self.settings = settings
        self.registry = registry or lpf.Registry(
            lpf.resolve_path(settings.launch_projects_path))
        self._envs = []
        self._inventory = None
        self._project_id = lpf.WORKSPACE
        self._env_id = ""
        self._user_rows = []
        self._users_error = ""
        self._variable_names = []
        self._members = []
        self._filling = False
        self._developer = False

        # Scrolled, as Launch Sessions is: the list, the variables and the
        # users stacked are taller than a laptop screen, and a page that cannot
        # scroll makes the whole window refuse to fit.
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
            "An environment is a name and its variables: url is where its windows "
            "open, and can use the others as {{name}}. Its users are the accounts "
            "that sign in there, each with the scenarios it runs there.",
            "An environment is a name and its variables: url is passed as --url, "
            "with {{name}} filled from the others. Its users are the users.json rows "
            "filed under its account key, which is passed as --env."))
        column.addSpacing(12)

        # The project is the window's, picked in its top bar: what is done to
        # it is asked for here and done there (project_action).
        add_project = QPushButton("+ Project")
        add_project.setToolTip("A new, empty project, in a folder of its own")
        add_project.clicked.connect(lambda: self.project_action.emit("add"))
        self.rename_project_button = QPushButton("Rename project…")
        self.rename_project_button.setProperty("variant", "ghost")
        self.rename_project_button.clicked.connect(
            lambda: self.project_action.emit("rename"))
        self.export_project_button = QPushButton("Export project…")
        self.export_project_button.setProperty("variant", "ghost")
        self.export_project_button.setToolTip("Everything in this project to a file: "
                                              "environments, users and cycles")
        self.export_project_button.clicked.connect(
            lambda: self.project_action.emit("export"))
        self.delete_project_button = QPushButton("Delete project…")
        self.delete_project_button.setProperty("variant", "ghost")
        self.delete_project_button.setToolTip("Take it off the list; its folder stays")
        self.delete_project_button.clicked.connect(
            lambda: self.project_action.emit("delete"))
        project_row = widgets.row(add_project, self.rename_project_button,
                                  self.export_project_button,
                                  self.delete_project_button, None)
        self.project_row = project_row
        column.addWidget(project_row)
        # The file cannot be read: said above what it would have changed, and
        # nothing here is changeable until it reads.
        self.registry_problem = QLabel("")
        self.registry_problem.setProperty("role", "error")
        self.registry_problem.setWordWrap(True)
        self.registry_problem.setVisible(False)
        column.addWidget(self.registry_problem)
        column.addSpacing(6)

        panes = QHBoxLayout()
        panes.setSpacing(16)
        panes.addWidget(self._list_pane(), 0)
        panes.addWidget(self._editor_pane(), 1)
        column.addLayout(panes, 1)

        # Addresses the old "URL override" column saved and nothing ever
        # applied. Switching them on unasked would move where runs open, so each
        # is offered instead - and the block is not there at all without one.
        self.legacy_box = QWidget()
        self.legacy_rows = QVBoxLayout(self.legacy_box)
        self.legacy_rows.setContentsMargins(0, 8, 0, 0)
        self.legacy_rows.setSpacing(4)
        column.addWidget(self.legacy_box)
        self.legacy_box.setVisible(False)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll)

    # -- building -------------------------------------------------------------
    def _list_pane(self):
        pane = QWidget()
        pane.setFixedWidth(self.LIST_WIDTH)
        column = QVBoxLayout(pane)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = widgets.Table(0, len(self.HEADERS), reclick_clears=False)
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(320)
        self.table.itemSelectionChanged.connect(self._env_picked)
        self.search_row = widgets.SearchRow(self.table)
        self.search_row.changed.connect(self._searched)
        self._env_note = widgets.empty_note(
            self.table, "No environments in this project yet - + Environment adds "
                        "one, or File -> Import brings them from another project.")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)
        self.add_env_button = QPushButton("+ Environment")
        self.add_env_button.clicked.connect(self.add_environment)
        column.addWidget(widgets.row(self.add_env_button, None))
        return pane

    def _editor_pane(self):
        panel = widgets.BlueprintPanel()
        self.editor_stack = QStackedWidget()

        empty = QWidget()
        empty_column = QVBoxLayout(empty)
        empty_column.setContentsMargins(0, 0, 0, 0)
        self.empty_state = widgets.empty_zone(
            "No environment selected",
            "Choose one on the left to see its variables and users, or add one "
            "with + Environment.")
        empty_column.addWidget(self.empty_state)
        empty_column.addStretch(1)
        self.editor_stack.addWidget(empty)

        editor = QWidget()
        outer = QVBoxLayout(editor)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        # Name, variables, users: each on its own faint ground, as the groups
        # of Advanced on Launch Sessions are, so they read apart without boxes.
        self._sections = []
        column = self._section(outer)
        self.name_edit = QLineEdit()
        self.name_edit.editingFinished.connect(self._rename_environment)
        self.duplicate_button = QPushButton("Duplicate…")
        self.duplicate_button.setToolTip("A copy with the same variables and users, "
                                         "signing in afresh in profiles of its own")
        self.duplicate_button.clicked.connect(self.duplicate_environment)
        self.export_env_button = QPushButton("Export…")
        self.export_env_button.setToolTip("This environment to a file, with its users")
        self.export_env_button.clicked.connect(
            lambda: self.export_requested.emit({"environments": [self._env_id]}))
        self.delete_env_button = QPushButton("Delete…")
        self.delete_env_button.setProperty("variant", "ghost")
        self.delete_env_button.clicked.connect(self.delete_environment)
        name_row = widgets.row(widgets.field("Name", self.name_edit),
                               self.duplicate_button, self.export_env_button,
                               self.delete_env_button)
        name_row.layout().setStretch(0, 1)
        widgets.level_with(self.name_edit, name_row, self.duplicate_button,
                           self.export_env_button, self.delete_env_button)
        column.addWidget(name_row)
        # Developer wording only: what the launcher is given for it.
        self.key_label = widgets.mono("")
        self.key_label.setVisible(False)
        column.addWidget(self.key_label)

        column = self._section(outer)
        column.addWidget(widgets.kicker("Variables"))
        variables_panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.variables = widgets.Table(0, len(self.VARIABLE_HEADERS),
                                       reclick_clears=False)
        self.variables.setHorizontalHeaderLabels(self.VARIABLE_HEADERS)
        self.variables.verticalHeader().setVisible(False)
        self.variables.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.variables.setSelectionMode(QAbstractItemView.SingleSelection)
        self.variables.setEditTriggers(QAbstractItemView.DoubleClicked
                                       | QAbstractItemView.EditKeyPressed
                                       | QAbstractItemView.AnyKeyPressed)
        self.variables.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Interactive)
        self.variables.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.variables.setColumnWidth(0, 180)
        widgets.cell_editing(self.variables)
        self.variables.itemChanged.connect(self._variable_edited)
        self.variables.itemSelectionChanged.connect(self._update_buttons)
        variables_panel.layout().addWidget(self.variables)
        column.addWidget(variables_panel)
        self.opens = QLabel("")
        self.opens.setProperty("role", "mono")
        self.opens.setWordWrap(True)
        self.opens.setTextInteractionFlags(Qt.TextSelectableByMouse)
        column.addWidget(self.opens)
        self.add_variable_button = QPushButton("+ Variable")
        self.add_variable_button.clicked.connect(lambda: self.add_variable())
        # url names a variable nobody gave a value: one click adds it.
        self.add_missing_button = QPushButton("")
        self.add_missing_button.setVisible(False)
        self.add_missing_button.clicked.connect(
            lambda: self.add_variable(self.add_missing_button.property("variable")))
        # Beside + Variable, a button like it: as a quiet link at the far end
        # it was never noticed. Delete and the row's menu do the same.
        self.remove_variable_button = QPushButton("Remove variable")
        self.remove_variable_button.clicked.connect(self.remove_variable)
        column.addWidget(widgets.row(self.add_variable_button,
                                     self.remove_variable_button,
                                     self.add_missing_button, None))
        # Delete on the table itself (not a window shortcut): it is the table
        # that has the keyboard when a row is picked.
        self.variables.installEventFilter(self)
        self.variables.setContextMenuPolicy(Qt.CustomContextMenu)
        self.variables.customContextMenuRequested.connect(self._variable_menu)

        column = self._section(outer)
        self.users_kicker = widgets.kicker("Users")
        column.addWidget(self.users_kicker)
        users_panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.users = widgets.Table(0, len(self.USER_HEADERS))
        self.users.setHorizontalHeaderLabels(self.USER_HEADERS)
        self.users.verticalHeader().setVisible(False)
        self.users.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.users.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.users.setEditTriggers(QAbstractItemView.DoubleClicked
                                   | QAbstractItemView.EditKeyPressed)
        users_header = self.users.horizontalHeader()
        users_header.setSectionResizeMode(0, QHeaderView.Interactive)
        users_header.setSectionResizeMode(1, QHeaderView.Interactive)
        users_header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.users.setColumnWidth(0, 140)
        self.users.setColumnWidth(1, 200)
        widgets.cell_editing(self.users)
        self.users.itemChanged.connect(self._scenarios_edited)
        self.users.itemSelectionChanged.connect(self._update_buttons)
        self._users_note = widgets.empty_note(
            self.users, "No users here yet - Add users… puts accounts from "
                        "Credentials in this environment.")
        users_panel.layout().addWidget(self.users)
        column.addWidget(users_panel)
        self.users_problem = QLabel("")
        self.users_problem.setProperty("role", "error")
        self.users_problem.setWordWrap(True)
        self.users_problem.setVisible(False)
        column.addWidget(self.users_problem)
        self.add_users_button = QPushButton("Add users…")
        self.add_users_button.clicked.connect(self.add_users)
        self.remove_users_button = QPushButton("Remove users")
        self.remove_users_button.setToolTip("Take the selected users out of this "
                                            "environment; they stay in Credentials")
        self.remove_users_button.clicked.connect(self.remove_users)
        column.addWidget(widgets.row(self.add_users_button, self.remove_users_button,
                                     None))
        outer.addStretch(1)
        for section in self._sections:
            widgets.see_through(section)
        self.editor_stack.addWidget(editor)
        panel.layout().addWidget(self.editor_stack)
        return panel

    def _section(self, outer):
        """A group of the editor on its own faint ground; its layout to fill."""
        frame = QFrame()
        frame.setProperty("role", "section")
        self._sections.append(frame)
        column = QVBoxLayout(frame)
        column.setContentsMargins(12, 10, 12, 12)
        column.setSpacing(6)
        outer.addWidget(frame)
        return column

    def set_developer_mode(self, enabled):
        self._developer = bool(enabled)
        self.wording.apply(enabled)
        self._show_key(self._selected_env())

    # -- data -----------------------------------------------------------------
    def set_inventory(self, inventory):
        self._inventory = inventory
        self.registry.reload()
        self._load_users()
        self._fill_list()
        self._show_legacy()
        self.registry_problem.setText(
            "launch-projects.json cannot be read, so nothing here can be changed until "
            "it can: %s" % self.registry.error if self.registry.error else "")
        self.registry_problem.setVisible(bool(self.registry.error))
        self._update_buttons()

    def set_inventory_rows(self):
        """Redraw from the inventory last given."""
        if self._inventory is not None:
            self.set_inventory(self._inventory)

    def _users_path(self):
        return getattr(self._inventory, "config_path", "") or ""

    def _load_users(self):
        path = self._users_path()
        try:
            self._user_rows = usersfile.load(path)
            self._users_error = ""
        except usersfile.UsersFileError as exc:
            self._user_rows = []
            self._users_error = str(exc)

    def _all_environments(self):
        """Every environment, as the files are now: a users.json value this page
        has just emptied is not offered again while the inventory catches up."""
        envs = self.registry.environments(self._inventory)
        if self._users_error or not self._users_path():
            return envs
        present = {row.env for row in self._user_rows}
        return [env for env in envs if not env.virtual or env.account_key in present]

    def _env_names(self):
        """``{account key: environment name}`` across every project."""
        names = {}
        for env in self._all_environments():
            names.setdefault(env.account_key, env.name)
        return names

    def _fill_list(self):
        self._envs = self._all_environments()
        counts = {}
        for row in self._user_rows:
            counts[row.env] = counts.get(row.env, 0) + 1
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._envs))
        for row, env in enumerate(self._envs):
            self._set(self.table, row, 0, env.name)
            users = counts.get(env.account_key, 0) if not self._users_error \
                else env.accounts
            self._set(self.table, row, 1, str(users) if users else NONE,
                      color=None if users else theme.NEUTRAL[600],
                      align=Qt.AlignRight | Qt.AlignVCenter)
        self.table.blockSignals(False)
        self.search_row.apply()
        ids = [env.id for env in self._envs]
        if self._env_id not in ids:
            self._env_id = ids[0] if ids else ""
        self._select_in_list(self._env_id)
        self._show_editor()

    def _select_in_list(self, env_id):
        self.table.blockSignals(True)
        self.table.clearSelection()
        for row, env in enumerate(self._envs):
            if env.id == env_id:
                self.table.selectRow(row)
                break
        self.table.blockSignals(False)

    def _set(self, table, row, col, text, mono=False, color=None, editable=False,
             align=None):
        item = QTableWidgetItem(text)
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if editable:
            flags |= Qt.ItemIsEditable
        item.setFlags(flags)
        if mono:
            item.setFont(theme.mono_font(9))
        if color:
            item.setForeground(theme.color(color))
        if align is not None:
            item.setTextAlignment(align)
        table.setItem(row, col, item)
        return item

    def _selected_env(self):
        for env in self._envs:
            if env.id == self._env_id:
                return env
        return None

    def _env_picked(self):
        rows = self.table.selectionModel().selectedRows() \
            if self.table.selectionModel() else []
        row = rows[0].row() if rows else -1
        if 0 <= row < len(self._envs) and self._envs[row].id != self._env_id:
            self._env_id = self._envs[row].id
            self._show_editor()

    def _searched(self):
        self._env_note.say_filtered("Nothing here matches the search."
                                    if self.search_row.searching() else "")

    def address_for(self, env_id):
        """The url set for an environment; "" when it follows its origin."""
        env = lpf.find(self._all_environments(), env_id)
        return env.address if env is not None else ""

    # -- the editor -----------------------------------------------------------
    def _show_editor(self):
        env = self._selected_env()
        self.editor_stack.setCurrentIndex(1 if env is not None else 0)
        if env is None:
            self._update_buttons()
            return
        if not self.name_edit.hasFocus() or self.name_edit.text() != env.name:
            self.name_edit.setText(env.name)
        self._show_key(env)
        self._fill_variables(env)
        self._show_opens(env)
        self._fill_users(env)
        self._update_buttons()

    def _show_key(self, env):
        self.key_label.setText("--env %s" % env.account_key if env is not None else "")
        self.key_label.setVisible(self._developer and env is not None)

    def _fill_variables(self, env):
        """``url`` first - the origin when nothing was written - then the rest."""
        self._filling = True
        rows = [(lpf.URL, env.address or env.origin)] + list(env.variables.items())
        self.variables.setRowCount(len(rows))
        for row, (name, value) in enumerate(rows):
            name_item = self._set(self.variables, row, 0, name, mono=True,
                                  editable=name != lpf.URL)
            value_item = self._set(self.variables, row, 1, value, mono=True,
                                   editable=True)
            if name == lpf.URL:
                # Reserved, so it looks it: the theme's tag tone, the whole row.
                for item in (name_item, value_item):
                    item.setBackground(theme.color(theme.TAG_TINT))
                    item.setForeground(theme.color(theme.TAG_INK))
                    item.setToolTip(
                        "url is a reserved variable: the address this environment's "
                        "windows open. Its name cannot change and it cannot be "
                        "removed; write {{name}} in it to use another variable.")
        self._variable_names = [name for name, _value in rows]
        self._filling = False
        self._fit(self.variables, 2, 8)

    @staticmethod
    def _fit(table, least, most):
        """As tall as its rows - at least ``least`` of them, so an empty one has
        room to say so, and at most ``most``, past which it scrolls."""
        rows = max(least, min(most, table.rowCount()))
        table.setFixedHeight(table.horizontalHeader().sizeHint().height()
                             + rows * table.verticalHeader().defaultSectionSize()
                             + 2 * table.frameWidth())

    def _show_opens(self, env):
        """"Opens http://…" under the variables, or what stops it opening."""
        template = env.address or env.origin
        message, bad = opens_text(template, env.variables)
        self.opens.setText(message)
        self.opens.setProperty("role", "error" if bad else "mono")
        self.opens.style().unpolish(self.opens)
        self.opens.style().polish(self.opens)
        missing = [name for name in launchvars.references(template)
                   if name != lpf.URL and name not in env.variables
                   and launchvars.valid_name(name)]
        self.add_missing_button.setVisible(bool(missing))
        if missing:
            self.add_missing_button.setText("Add %s" % missing[0])
            self.add_missing_button.setProperty("variable", missing[0])

    def _fill_users(self, env):
        self._filling = True
        self._members = [row for _index, row in
                         usersfile.members(self._user_rows, env.account_key)]
        self.users.setRowCount(len(self._members))
        for row, member in enumerate(self._members):
            self._set(self.users, row, 0, member.cls or NONE,
                      color=None if member.cls else theme.NEUTRAL[600])
            self._set(self.users, row, 1, member.login, mono=True)
            tests = member.tests_text()
            item = self._set(self.users, row, 2, tests or NONE, mono=True,
                             color=None if tests else theme.NEUTRAL[600], editable=True)
            item.setToolTip("The scenarios it runs here with \"each account's own "
                            "list\": ids, or tag:name, comma-separated. Double-click "
                            "to change.")
        self.users_kicker.setText("USERS · %d" % len(self._members)
                                  if self._members else "USERS")
        self._filling = False
        self._fit(self.users, 3, 10)
        self.users_problem.setText(
            "users.json cannot be read, so who is here cannot be shown or changed: %s"
            % self._users_error if self._users_error else "")
        self.users_problem.setVisible(bool(self._users_error))

    def _update_buttons(self):
        readable = not self.registry.error
        env = self._selected_env()
        for button in (self.duplicate_button,
                       self.export_env_button, self.delete_env_button,
                       self.add_variable_button):
            button.setEnabled(readable and env is not None)
        self.name_edit.setEnabled(readable and env is not None)
        self.variables.setEnabled(readable and env is not None)
        name = self._selected_variable()
        self.remove_variable_button.setEnabled(readable and bool(name)
                                               and name != lpf.URL)
        self.remove_variable_button.setToolTip(
            "url is reserved and cannot be removed" if name == lpf.URL else
            "Remove %s from this environment (Delete)" % name if name else
            "Select a variable to remove it")
        users_ok = env is not None and not self._users_error and bool(self._users_path())
        self.add_users_button.setEnabled(users_ok)
        self.remove_users_button.setEnabled(users_ok and bool(self._selected_members()))
        self.users.setEnabled(not self._users_error)
        self.add_env_button.setEnabled(readable)
        workspace = self.settings.project_id == lpf.WORKSPACE
        self.rename_project_button.setEnabled(not workspace)
        self.delete_project_button.setEnabled(not workspace)

    # -- saving ---------------------------------------------------------------
    def _change_env(self, env, apply, what="The change"):
        """Apply ``apply(row)`` to ``env``'s row in the file; False if it failed.

        A virtual environment is written down first, with the url it opens -
        its origin came from users.json, and would go with its last user.
        """
        def change(projects):
            row = lpf.environment_row(projects, env)
            if env.virtual and not row.address and env.origin:
                row.address = env.origin
            return apply(row)
        try:
            self.registry.update(change)
        except lpf.LaunchProjectsError as exc:
            widgets.warn(self, "Environments", "%s was not saved." % what, str(exc))
            return False
        self.addresses_changed.emit()
        return True

    def _change_users(self, apply, what="The change", env=None):
        """Apply ``apply(rows)`` to users.json as it is now; False if it failed.

        Users leaving a virtual ``env`` could leave nothing in users.json to
        list it by, so it is written down first and stays, with no users.
        """
        if env is not None and env.virtual \
                and not self._change_env(env, lambda _row: None, what):
            return False
        try:
            usersfile.change(self._users_path(), apply)
        except usersfile.UsersFileError as exc:
            widgets.warn(self, "Environments", "%s was not saved to users.json." % what,
                         str(exc))
            self._load_users()
            self._refresh_selected()
            return False
        self._load_users()
        self.accounts_changed.emit()
        return True

    def _refresh_selected(self):
        """The list and the editor again, from the files as they are now."""
        self._fill_list()
        self._update_buttons()

    def _reload_after(self, done):
        if done:
            self.registry.reload()
            self._refresh_selected()
        return done

    # -- variables ------------------------------------------------------------
    def _selected_variable(self):
        rows = self.variables.selectionModel().selectedRows() \
            if self.variables.selectionModel() else []
        row = rows[0].row() if rows else -1
        return self._variable_names[row] if 0 <= row < len(self._variable_names) else ""

    def _name_problem(self, name, old):
        if not name:
            return "A variable needs a name."
        if name == lpf.URL or not launchvars.valid_name(name):
            return ("\"%s\" cannot be a variable's name: letters, digits and _, not "
                    "starting with a digit, and not url or one of %s."
                    % (name, ", ".join(sorted(launchvars.RESERVED))))
        if name != old and name in self._variable_names:
            return "There is a variable called %s already." % name
        return ""

    def _variable_edited(self, item):
        if self._filling:
            return
        env = self._selected_env()
        row, col = item.row(), item.column()
        if env is None or not 0 <= row < len(self._variable_names):
            return
        old = self._variable_names[row]
        text = item.text().strip()
        if col == 0:
            if text == old:
                return
            problem = self._name_problem(text, old)
            if problem:
                widgets.warn(self, "Variables", problem)
                self._filling = True
                item.setText(old)
                self._filling = False
                return
            value_item = self.variables.item(row, 1)
            value = value_item.text().strip() if value_item is not None else ""

            def apply(env_row):
                if old:
                    env_row.variables = {(text if name == old else name): given
                                         for name, given in env_row.variables.items()}
                else:
                    env_row.variables[text] = value
            if self._change_env(env, apply, "The variable"):
                self._variable_names[row] = text
                self._after_variable_change()
            return
        if not old:
            return          # a value typed before its name: kept until the name is

        def apply(env_row):
            if old == lpf.URL:
                env_row.address = text
            else:
                env_row.variables[old] = text
        if self._change_env(env, apply, "The value"):
            self._after_variable_change()

    def _after_variable_change(self):
        """The file was written: what is shown beside the table follows it, the
        table itself stays as it is being edited."""
        self._envs = self._all_environments()
        env = self._selected_env()
        if env is not None:
            self._show_opens(env)
            self._show_key(env)
        self._update_buttons()

    def add_variable(self, name=""):
        """A new row: its name being typed, written once it has one - or, for a
        name url already uses, written at once with its value being typed."""
        env = self._selected_env()
        if env is None:
            return
        if name:
            if not self._change_env(env, lambda row: row.variables.setdefault(name, ""),
                                    "The variable"):
                return
            self._refresh_selected()
            row = self._variable_names.index(name)
            self.variables.setCurrentCell(row, 1)
            self.variables.editItem(self.variables.item(row, 1))
            return
        row = self.variables.rowCount()
        self._filling = True
        self.variables.setRowCount(row + 1)
        self._set(self.variables, row, 0, "", mono=True, editable=True)
        self._set(self.variables, row, 1, "", mono=True, editable=True)
        self._filling = False
        self._variable_names.append("")
        self._fit(self.variables, 2, 8)
        self.variables.setCurrentCell(row, 0)
        self.variables.editItem(self.variables.item(row, 0))

    def eventFilter(self, watched, event):          # noqa: N802 - Qt's name
        """Delete in the variables table removes the row - but not while a cell
        is being typed in, where Delete is the editor's."""
        if watched is self.variables and event.type() == QEvent.KeyPress \
                and event.key() == Qt.Key_Delete \
                and self.variables.state() != QAbstractItemView.EditingState:
            self.remove_variable()
            return True
        return super().eventFilter(watched, event)

    def _variable_menu(self, point):
        row = self.variables.rowAt(point.y())
        if row < 0:
            return
        self.variables.selectRow(row)
        name = self._selected_variable()
        menu = QMenu(self.variables)
        action = menu.addAction("Remove variable", self.remove_variable)
        action.setEnabled(bool(name) and name != lpf.URL)
        if name == lpf.URL:
            action.setText("url is reserved - it cannot be removed")
        menu.exec(self.variables.viewport().mapToGlobal(point))

    def remove_variable(self):
        env = self._selected_env()
        name = self._selected_variable()
        if env is None or name == lpf.URL:
            return
        row = self.variables.currentRow()
        if not name:
            # A row added and not named yet: nothing is written, it just goes.
            if 0 <= row < len(self._variable_names) and not self._variable_names[row]:
                self.variables.removeRow(row)
                del self._variable_names[row]
                self._fit(self.variables, 2, 8)
            return

        def apply(env_row):
            env_row.variables.pop(name, None)
        if self._change_env(env, apply, "The change"):
            self._refresh_selected()

    # -- users ----------------------------------------------------------------
    def _selected_members(self):
        rows = sorted({index.row() for index in self.users.selectionModel().selectedRows()}) \
            if self.users.selectionModel() else []
        return [self._members[row] for row in rows if 0 <= row < len(self._members)]

    def _scenarios_edited(self, item):
        if self._filling or item.column() != 2:
            return
        env = self._selected_env()
        row = item.row()
        if env is None or not 0 <= row < len(self._members):
            return
        member = self._members[row]
        text = item.text().strip()
        # The table is redrawn once the edit that called this has finished.
        redraw = lambda: self._fill_users(env)          # noqa: E731
        try:
            tests = usersfile.parse_tests(text if text != NONE else "")
        except usersfile.UsersFileError as exc:
            widgets.warn(self, "Scenarios", str(exc))
            QTimer.singleShot(0, redraw)
            return
        if tests != member.tests:
            self._change_users(lambda rows: usersfile.set_tests(
                rows, env.account_key, member.login, tests), "The scenarios")
        QTimer.singleShot(0, redraw)

    def add_users(self):
        """Choose who is in this environment, from everyone in users.json."""
        env = self._selected_env()
        if env is None:
            return
        dialog = UsersPicker(self, env, self._users_path(), self._env_names())
        if dialog.exec() != QDialog.Accepted:
            if dialog.wrote:
                self._load_users()
                self._refresh_selected()
                self.accounts_changed.emit()
            return
        adding, removing = dialog.changes()
        if not adding and not removing:
            if dialog.wrote:
                self._load_users()
                self._refresh_selected()
                self.accounts_changed.emit()
            return

        def apply(rows):
            for key in adding:
                usersfile.assign(rows, key, env.account_key)
            for key in removing:
                usersfile.unassign(rows, key, env.account_key)
        self._change_users(apply, "Who is in %s" % env.name,
                           env if removing else None)
        self._refresh_selected()

    def remove_users(self):
        env = self._selected_env()
        chosen = self._selected_members()
        if env is None or not chosen:
            return
        logins = ", ".join(member.login for member in chosen)
        if not widgets.confirm(
                self, "Remove users", "Take %s out of %s?" % (logins, env.name),
                "The scenarios they run here go with them. They stay in Credentials, "
                "and their browser profiles stay on disk.",
                agree="Remove", kind="warn"):
            return
        keys = [(member.login, member.password, member.cls) for member in chosen]

        def apply(rows):
            for key in keys:
                usersfile.unassign(rows, key, env.account_key)
        self._change_users(apply, "The change", env)
        self._refresh_selected()

    # -- environments ---------------------------------------------------------
    def _taken_keys(self):
        keys = [env.get("value", "") for env in
                (self._inventory.envs if self._inventory is not None else [])]
        keys += [env.account_key for env in self._all_environments()]
        keys += [row.env for row in self._user_rows]
        return sorted({key for key in keys if key})

    def _project(self):
        return self.registry.project(self._project_id)

    def add_environment(self):
        dialog = EnvironmentDialog(self, self._project_name(),
                                   taken_names=[env.name for env in self._envs])
        if dialog.exec() != QDialog.Accepted:
            return
        name, url = dialog.result_values()
        row = lpf.EnvironmentRow(lpf.new_id("e"), name,
                                 lpf.new_key(name, self._taken_keys()), url)
        project_id = self._project_id
        try:
            self.registry.update(lambda projects: lpf.project_row(
                projects, project_id).environments.append(row))
        except lpf.LaunchProjectsError as exc:
            widgets.warn(self, "Environments", "The environment was not saved.", str(exc))
            return
        self._env_id = row.id
        self.addresses_changed.emit()
        self._refresh_selected()

    def _rename_environment(self):
        env = self._selected_env()
        name = self.name_edit.text().strip()
        if env is None or name == env.name:
            return
        taken = {other.name.casefold() for other in self._envs if other.id != env.id}
        if not name or name.casefold() in taken:
            widgets.warn(self, "Environments",
                         "Give it a name." if not name else
                         "%s already has an environment called %s."
                         % (self._project_name(), name))
            self.name_edit.setText(env.name)
            return
        if self._reload_after(self._change_env(
                env, lambda row: setattr(row, "name", name), "The name")):
            self.accounts_changed.emit()     # Credentials names environments too

    def duplicate_environment(self):
        """The same variables and users under a new name - and a key of its own,
        so the copy signs in afresh rather than in the original's profiles."""
        env = self._selected_env()
        if env is None:
            return
        taken = {other.name.casefold() for other in self._envs}
        suggestion, number = "%s copy" % env.name, 1
        while suggestion.casefold() in taken:
            number += 1
            suggestion = "%s copy %d" % (env.name, number)
        name = widgets.ask_text(
            self, "Duplicate environment", "Name the copy of %s:" % env.name, suggestion,
            "It gets the same variables and the same %d user(s) with their scenarios. "
            "They sign in to it in browser profiles of its own."
            % len(self._members))
        name = (name or "").strip()
        if not name:
            return
        if name.casefold() in taken:
            widgets.warn(self, "Duplicate environment",
                         "%s already has an environment called %s."
                         % (self._project_name(), name))
            return
        key = lpf.new_key(name, self._taken_keys())
        variables = dict(env.variables)
        row = lpf.EnvironmentRow(lpf.new_id("e"), name, key, env.address or env.origin,
                                 variables)
        project_id = env.project_id
        try:
            self.registry.update(lambda projects: lpf.project_row(
                projects, project_id).environments.append(row))
        except lpf.LaunchProjectsError as exc:
            widgets.warn(self, "Environments", "The copy was not saved.", str(exc))
            return
        self._env_id = row.id
        self.addresses_changed.emit()
        if self._members:
            self._change_users(lambda rows: usersfile.copy_env(rows, env.account_key, key),
                               "The users of the copy")
        self._refresh_selected()

    def delete_environment(self):
        """The environment goes, and its users leave it: anyone who was only
        here stays in Credentials, without an environment."""
        env = self._selected_env()
        if env is None:
            return
        users = len(self._members)
        if not widgets.confirm(
                self, "Delete environment", "Delete \"%s\"?" % env.name,
                ("Its %d user(s) leave it and stay in Credentials; the scenarios they "
                 "run here go. Browser profiles stay on disk." % users) if users
                else "It has no users. Its variables go with it.",
                agree="Delete", kind="warn"):
            return
        if users and not self._change_users(
                lambda rows: usersfile.drop_env(rows, env.account_key), "The change"):
            return

        def change(projects):
            for project in projects:
                project.environments = [row for row in project.environments
                                        if row.id != env.id]
        self._env_id = ""
        self._change(change, "The change")

    # -- projects -------------------------------------------------------------
    def _project_name(self):
        """The window's project, by name - what the forms speak of."""
        from .. import projectsfile
        try:
            projects = projectsfile.load()
        except projectsfile.ProjectsFileError:
            projects = []
        project = projectsfile.find(projects, self.settings.project_id)
        return project.name if project is not None else lpf.WORKSPACE_NAME

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

    # -- addresses saved long ago ---------------------------------------------
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
            apply.setToolTip("Use it as this environment's url" if value else
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
        env = lpf.find(self._all_environments(), env_id)
        if env is None:
            return
        if not self._change_env(env, lambda row: setattr(row, "address", url),
                                "The url"):
            return
        self.settings.discard_legacy_env_override(alias)
        self.set_inventory_rows()

    def _drop_legacy(self, alias):
        self.settings.discard_legacy_env_override(alias)
        self._show_legacy()

    # -- search (Ctrl+K) -------------------------------------------------------
    def search_rows(self):
        """Every environment: its name, where it opens, how many users it has."""
        return [search.hit(
            env.name,
            " · ".join(part for part in (env.address or env.origin or "",
                                         "%d user%s" % (env.accounts,
                                                        "" if env.accounts == 1 else "s"))
                       if part),
            keys=env.account_key, target=env.id)
            for env in self._envs]

    def show_found(self, env_id):
        for row, env in enumerate(self._envs):
            if env.id == env_id or env.name == env_id:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
                return True
        return False
