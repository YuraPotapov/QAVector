"""Credentials page: everyone who signs in.

One row per person - a role (what the window is called), a login and a
password. Where they sign in is the Environments page's business; here it can
only be given to several people at once (Add to environment), and nobody types
an environment's key.

Underneath it is users.json, the launcher's own file, unchanged: a person in
three environments is three rows of it, one per environment, each with the
scenarios they run there (:func:`usersfile.credentials`). So this page's job is
still to keep that file *exactly* as valid as the CLI expects - every rule in
``usersfile.validate`` is one the launcher would otherwise exit on. Passwords
are masked by default with a per-row reveal, because the whole point of the
file is that it holds them in the clear for the auto-login extension to seed.
Changes stay on screen until Save, which writes the file atomically with a
backup.
"""

import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import search, theme, usersfile, widgets


class CredentialsPage(QWidget):
    """Edit, validate and save the people in the config the launcher reads."""

    saved = Signal()
    #: File -> Export with the selected users ticked: {"users": [key]}.
    export_requested = Signal(dict)

    HEADERS = ["Role", "Login", "Password", ""]
    COL_CLASS, COL_LOGIN, COL_PASSWORD, COL_ACTIONS = range(4)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path = ""
        self._rows = []
        self._people = []
        self._revealed = set()
        self._fingerprint = None
        self._loading = False
        #: ``[(account key, name)]`` of every environment there is, in order.
        self._environments = []
        # Why the file could not be read; "" when it could. While it is set the
        # table is not the file, so nothing here may be written over it.
        self._unreadable = ""
        # Edited since loaded or saved - and so not to be replaced by a refresh.
        self._dirty = False
        # The file changed on disk under an edit that was kept.
        self._changed_on_disk = False

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 20, 24, 20)
        column.setSpacing(6)

        title = widgets.heading("Credentials")
        self.add_button = QPushButton("+ Add user")
        self.add_button.clicked.connect(self.add_row)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.setToolTip("A new user with the same role and password, "
                                         "a login of its own and no environments yet")
        self.duplicate_button.clicked.connect(self.duplicate_row)
        self.reload_button = QPushButton("Reload")
        self.reload_button.clicked.connect(lambda: self.reload())
        self.save_button = QPushButton("Save users.json")
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self.save)
        column.addWidget(widgets.row(title, None, self.add_button,
                                     self.duplicate_button, self.reload_button,
                                     self.save_button))

        # Shortened in the middle: a project's users.json is deep in its folder,
        # and a whole path held the window wider than a laptop screen.
        self.path_label = widgets.elided_mono("")
        column.addWidget(self.path_label)
        column.addWidget(widgets.lede(
            "Everyone who signs in: the role their window is called, a login and a "
            "password. Where each one signs in is set on the Environments page. Saved "
            "to the launcher's users.json, with a backup; passwords stay in it because "
            "the auto-login extension seeds them into each profile."))
        column.addSpacing(12)

        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        widgets.cell_editing(self.table)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_LOGIN, QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_ACTIONS, QHeaderView.Fixed)
        self.table.setColumnWidth(self.COL_ACTIONS, 150)
        self._empty = widgets.empty_note(
            self.table, "Nobody here yet - + Add user puts the first account in.")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)

        self.add_to_button = QPushButton("Add to environment…")
        self.add_to_button.setToolTip("Put the selected users in one environment")
        self.add_to_button.clicked.connect(self.add_to_environment)
        self.remove_from_button = QPushButton("Remove from environment…")
        self.remove_from_button.setProperty("variant", "ghost")
        self.remove_from_button.setToolTip("Take the selected users out of one "
                                           "environment; they stay here")
        self.remove_from_button.clicked.connect(self.remove_from_environment)
        self.export_button = QPushButton("Export…")
        self.export_button.setToolTip("The selected users to a file, passwords and "
                                      "all, with the names of their environments")
        self.export_button.clicked.connect(lambda: self.export_requested.emit(
            {"users": [person.key for person in self._selected_people()
                       if person.login]}))
        column.addWidget(widgets.row(self.add_to_button, self.remove_from_button, None,
                                     self.export_button))

        self.status = QLabel("")
        self.status.setWordWrap(True)
        column.addWidget(self.status)
        self._update_buttons()

    # -- environments ---------------------------------------------------------
    def set_environments(self, environments):
        """The environments there are (``launchprojectsfile.Environment``), by
        name - what Add to environment offers and Search says."""
        found, seen = [], set()
        for env in environments or ():
            if env.account_key and env.account_key not in seen:
                seen.add(env.account_key)
                found.append((env.account_key, env.name))
        self._environments = found
        if not self._loading:
            self._rebuild()

    def _env_name(self, key):
        for value, name in self._environments:
            if value == key:
                return name
        return key

    def _env_choices(self):
        """Every environment to offer, the ones only users.json knows included."""
        choices = list(self._environments)
        known = {key for key, _name in choices}
        for row in self._rows:
            if row.env and row.env not in known:
                known.add(row.env)
                choices.append((row.env, row.env))
        return choices

    # -- loading / saving -----------------------------------------------------
    def load(self, path):
        self._path = path or ""
        self.path_label.setText(self._path or "(no config path configured)")
        self._unreadable = ""
        try:
            self._rows = usersfile.load(self._path)
        except usersfile.UsersFileError as exc:
            self._rows = []
            self._unreadable = str(exc)
        self._fingerprint = usersfile.fingerprint(self._path)
        self._revealed.clear()
        self._changed_on_disk = False
        self._set_dirty(False)
        self._rebuild()
        editable = not self._unreadable
        self.table.setEnabled(editable)
        self.add_button.setEnabled(editable)
        self._validate()

    def refresh(self, path):
        """Load ``path`` - unless the table holds an edit not saved yet.

        Every save elsewhere in the window refreshes what the core reads, and
        that used to reload this table with it, throwing the edit away. An edit
        is kept now; if the file changed underneath it, that is said, and Save
        asks before replacing it.
        """
        if self._dirty and (path or "") == self._path:
            if usersfile.fingerprint(self._path) != self._fingerprint:
                self._changed_on_disk = True
            self._validate()
            return
        self.load(path)

    def reload(self, path=None):
        """Read the file again on request; an unsaved edit is asked about first.

        The one place that throws the table away on purpose, so the one place
        that asks: the edits on screen are nowhere else.
        """
        if self._dirty and not widgets.confirm(
                self, "Reload users.json",
                "Drop the changes on screen and read the file again?",
                "Nothing has been saved since they were made.",
                agree="Drop and reload", kind="warn"):
            return
        self.load(self._path if path is None else path)

    def is_dirty(self):
        return self._dirty

    def _set_dirty(self, dirty):
        self._dirty = bool(dirty)
        self.save_button.setProperty("dirty", "true" if self._dirty else "false")
        self.save_button.setToolTip("Unsaved changes to users.json" if self._dirty
                                    else "")
        self.save_button.style().unpolish(self.save_button)
        self.save_button.style().polish(self.save_button)

    def save(self):
        if not self._path:
            self._show_problem("No config path configured (Settings -> Config).")
            return
        if self._unreadable:
            return          # what is on screen is not the file; see _validate
        current = usersfile.fingerprint(self._path)
        if self._fingerprint is not None and current != self._fingerprint:
            if not widgets.confirm(
                    self, "File changed on disk",
                    "Overwrite %s with what is on screen?" % self._path,
                    "It changed since it was loaded here, so whatever was "
                    "written meanwhile is what would be lost.",
                    agree="Overwrite", kind="warn"):
                return
        try:
            usersfile.save(self._path, self._rows)
        except usersfile.UsersFileError as exc:
            self._show_problem(str(exc))
            return
        self._fingerprint = usersfile.fingerprint(self._path)
        self._changed_on_disk = False
        self._set_dirty(False)
        self._show_ok("Saved %d user(s) to %s (previous kept as .bak)."
                      % (len(self._people), self._path))
        self.saved.emit()

    # -- people ---------------------------------------------------------------
    def _changed(self):
        """The rows changed here: draw them again, mark them unsaved, check them."""
        self._rebuild()
        self._set_dirty(True)
        self._validate()

    def _selected_people(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()}) \
            if self.table.selectionModel() else []
        return [self._people[row] for row in rows if 0 <= row < len(self._people)]

    def add_row(self):
        """A new user, in no environment yet: Role is where typing starts."""
        self._rows.append(usersfile.UserRow(env="", cls="", login="", password=""))
        self._changed()
        self.table.setCurrentCell(len(self._people) - 1, self.COL_CLASS)

    def duplicate_row(self):
        chosen = self._selected_people()
        if not chosen:
            return
        person = chosen[0]
        # A login of its own (env+login names the profile folder) and no
        # environments: where the copy signs in is ticked once it has a login.
        self._rows.append(usersfile.UserRow(env="", cls=person.role, login="",
                                            password=person.password))
        self._changed()
        self.table.setCurrentCell(len(self._people) - 1, self.COL_LOGIN)

    def delete_row(self, index):
        if not (0 <= index < len(self._people)):
            return
        person = self._people[index]
        places = [self._env_name(env) for env in person.envs(self._rows)]
        if not widgets.confirm(
                self, "Delete user",
                "Remove %s from users.json?" % (person.login or "this user"),
                ("It signs in to %s; it leaves all of them. " % ", ".join(places)
                 if places else "") + "Its browser profiles stay on disk.",
                agree="Delete", kind="warn"):
            return
        usersfile.remove_credential(self._rows, person.key)
        self._revealed.clear()
        self._changed()

    def _rebuild(self):
        self._loading = True
        self._people = usersfile.credentials(self._rows)
        self.table.setRowCount(len(self._people))
        for index, person in enumerate(self._people):
            self._set(index, self.COL_CLASS, person.role)
            login = self._set(index, self.COL_LOGIN, person.login, mono=True)
            places = [self._env_name(env) for env in person.envs(self._rows)]
            login.setToolTip("Signs in to %s" % ", ".join(places) if places
                             else "In no environment yet")
            self._set(index, self.COL_PASSWORD,
                      person.password if index in self._revealed else "•" * 10,
                      mono=True, editable=index in self._revealed)
            self.table.setCellWidget(index, self.COL_ACTIONS, self._actions(index))
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(self.COL_ACTIONS, 150)
        self._loading = False
        self._update_buttons()

    def _actions(self, index):
        reveal = QPushButton("Hide" if index in self._revealed else "Reveal")
        reveal.setProperty("variant", "ghost")
        reveal.clicked.connect(lambda _c=False, i=index: self._toggle_reveal(i))
        delete = QPushButton("Delete")
        delete.setProperty("variant", "ghost")
        delete.clicked.connect(lambda _c=False, i=index: self.delete_row(i))
        return widgets.row(reveal, delete, None, spacing=2)

    def _set(self, row, col, text, mono=False, editable=True):
        item = QTableWidgetItem(text)
        if mono:
            item.setFont(theme.mono_font(9))
        if not editable:
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.table.setItem(row, col, item)
        return item

    def _toggle_reveal(self, index):
        if index in self._revealed:
            self._revealed.discard(index)
        else:
            self._revealed.add(index)
        self._rebuild()

    def _on_item_changed(self, item):
        if self._loading:
            return
        index, col = item.row(), item.column()
        if not (0 <= index < len(self._people)):
            return
        person = self._people[index]
        text = item.text()
        if col == self.COL_CLASS:
            usersfile.update_credential(self._rows, person.key, role=text.strip())
        elif col == self.COL_LOGIN:
            usersfile.update_credential(self._rows, person.key, login=text.strip())
        elif col == self.COL_PASSWORD and index in self._revealed:
            usersfile.update_credential(self._rows, person.key, password=text)
        else:
            return
        people = usersfile.credentials(self._rows)
        if len(people) == len(self._people):
            self._people = people
        else:
            # Now the same as someone else - one person in both their places.
            # Drawn again once the edit that did it has finished.
            self._revealed.clear()
            QTimer.singleShot(0, self._rebuild)
        self._set_dirty(True)
        self._validate()

    # -- where they sign in ---------------------------------------------------
    def _pick_environment(self, title, question, keys):
        choices = [(key, name) for key, name in self._env_choices() if key in keys]
        if not choices:
            return None
        chosen = widgets.ask_item(self, title, question,
                                  [name for _key, name in choices])
        if chosen is None:
            return None
        return choices[[name for _key, name in choices].index(chosen)][0]

    def add_to_environment(self):
        people = [person for person in self._selected_people() if person.login]
        if not people:
            return
        env = self._pick_environment(
            "Add to environment",
            "Put %s in:" % ", ".join(person.login for person in people),
            {key for key, _name in self._env_choices()})
        if env is None:
            return
        refused = []
        for person in people:
            try:
                usersfile.assign(self._rows, person.key, env)
            except usersfile.UsersFileError as exc:
                refused.append(str(exc))
        self._changed()
        if refused:
            widgets.warn(self, "Add to environment",
                         "Not everyone was put in %s." % self._env_name(env),
                         "\n".join(refused))

    def remove_from_environment(self):
        people = [person for person in self._selected_people() if person.login]
        places = {env for person in people for env in person.envs(self._rows)}
        if not places:
            return
        env = self._pick_environment(
            "Remove from environment",
            "Take %s out of:" % ", ".join(person.login for person in people), places)
        if env is None:
            return
        for person in people:
            usersfile.unassign(self._rows, person.key, env)
        self._changed()

    def _update_buttons(self):
        chosen = self._selected_people() if not self._unreadable else []
        self.duplicate_button.setEnabled(bool(chosen))
        self.add_to_button.setEnabled(bool(chosen) and bool(self._env_choices()))
        self.remove_from_button.setEnabled(any(person.envs(self._rows)
                                               for person in chosen))

    # -- validation -----------------------------------------------------------
    def _said_by_person(self, problem):
        """``row 7 (admin): ...`` names a line of the file; here it is a person."""
        match = re.match(r"row \d+(?: \(([^)]*)\))?: (.*)", problem, re.S)
        if not match:
            return problem
        said = match.group(2).replace("class is required", "role is required")
        return "%s: %s" % (match.group(1) or "A new user", said)

    def _validate(self):
        if self._unreadable:
            # Never "0 users, all fine": the table is empty because the file could
            # not be read, and saving it would write an empty list over the only
            # copy of every password - one the launcher then refuses to load.
            self._show_problem("%s\nFix the file, or put its .bak back, and press "
                               "Reload. Saving is off until it reads." % self._unreadable)
            self.save_button.setEnabled(False)
            return False
        problems = list(dict.fromkeys(self._said_by_person(problem)
                                      for problem in usersfile.validate(self._rows)))
        if problems:
            self._show_problem("\n".join(problems[:6]))
        else:
            self._show_ok("%d user(s) · every user has a role, a login and a password"
                          % len(self._people))
        if self._changed_on_disk:
            self.status.setText(self.status.text() + "\nusers.json changed on disk "
                                "since this table was loaded. Save asks before "
                                "replacing it; Reload reads it and drops these edits.")
        self.save_button.setEnabled(not problems)
        return not problems

    def _show_problem(self, message):
        self.status.setText(message)
        self.status.setStyleSheet("color: %s; font-size: 12px;" % theme.BAD)

    def _show_ok(self, message):
        self.status.setText(message)
        self.status.setStyleSheet("color: %s; font-size: 12px;" % theme.NEUTRAL[700])

    def rows(self):
        return list(self._rows)

    # -- search (Ctrl+K) -------------------------------------------------------
    def search_rows(self):
        """Every user: their login, role and where they sign in.

        Never the password - Search shows what it matched on, and a password
        is not something to find by typing it into a box.
        """
        return [search.hit(
            person.login or "(no login)",
            " · ".join(part for part in (
                person.role, ", ".join(self._env_name(env)
                                       for env in person.envs(self._rows))) if part),
            target=(person.login, person.role)) for person in self._people]

    def show_found(self, target):
        login, role = target
        for index, person in enumerate(self._people):
            if person.login == login and person.role == role:
                self.table.selectRow(index)
                self.table.scrollToItem(self.table.item(index, self.COL_LOGIN))
                return True
        return False

