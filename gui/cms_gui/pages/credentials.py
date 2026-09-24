"""Credentials page: a table editor over users.json.

The file is the launcher's, so this page's job is to keep it *exactly* as valid
as the CLI expects - every rule in ``usersfile.validate`` is one the launcher
would otherwise exit on. Passwords are masked by default with a per-row reveal,
because the whole point of the file is that it holds them in the clear for the
auto-login extension to seed.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import theme, usersfile, widgets


class CredentialsPage(QWidget):
    """Edit, validate and save the config the launcher reads."""

    saved = Signal()

    HEADERS = ["env", "class", "login", "password", "run-tests", ""]
    COL_ENV, COL_CLASS, COL_LOGIN, COL_PASSWORD, COL_TESTS, COL_ACTIONS = range(6)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path = ""
        self._rows = []
        self._revealed = set()
        self._fingerprint = None
        self._loading = False

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 20, 24, 20)
        column.setSpacing(6)

        title = widgets.heading("Credentials")
        self.add_button = QPushButton("+ Add row")
        self.add_button.clicked.connect(self.add_row)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.clicked.connect(self.duplicate_row)
        self.reload_button = QPushButton("Reload")
        self.reload_button.clicked.connect(lambda: self.load(self._path))
        self.save_button = QPushButton("Save users.json")
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self.save)
        column.addWidget(widgets.row(title, None, self.add_button,
                                     self.duplicate_button, self.reload_button,
                                     self.save_button))

        self.path_label = widgets.mono("")
        column.addWidget(self.path_label)
        column.addWidget(widgets.lede(
            "Direct editor over the launcher's own config - schema unchanged, saved "
            "atomically with a one-slot .bak. Passwords stay in this file because the "
            "auto-login extension seeds them into each profile."))
        column.addSpacing(12)

        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.itemChanged.connect(self._on_item_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_TESTS, QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_ACTIONS, QHeaderView.Fixed)
        self.table.setColumnWidth(self.COL_ACTIONS, 150)
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        column.addWidget(self.status)

    # -- loading / saving -----------------------------------------------------
    def load(self, path):
        self._path = path or ""
        self.path_label.setText(self._path or "(no config path configured)")
        try:
            self._rows = usersfile.load(self._path)
        except usersfile.UsersFileError as exc:
            self._rows = []
            self._show_problem(str(exc))
        self._fingerprint = usersfile.fingerprint(self._path)
        self._revealed.clear()
        self._rebuild()
        self._validate()

    def save(self):
        if not self._path:
            self._show_problem("No config path configured (Settings -> Config).")
            return
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
        self._show_ok("Saved %d row(s) to %s (previous kept as .bak)."
                      % (len(self._rows), self._path))
        self.saved.emit()

    # -- rows -----------------------------------------------------------------
    def add_row(self):
        env = self._rows[-1].env if self._rows else ""
        self._rows.append(usersfile.UserRow(env=env, cls="", login="", password=""))
        self._rebuild()
        self.table.setCurrentCell(len(self._rows) - 1, self.COL_CLASS)
        self._validate()

    def duplicate_row(self):
        index = self.table.currentRow()
        if index < 0 or index >= len(self._rows):
            return
        clone = self._rows[index].copy()
        clone.login = ""      # env+login must stay unique - that pair is the folder
        self._rows.insert(index + 1, clone)
        self._rebuild()
        self.table.setCurrentCell(index + 1, self.COL_LOGIN)
        self._validate()

    def delete_row(self, index):
        if not (0 <= index < len(self._rows)):
            return
        row = self._rows[index]
        if not widgets.confirm(
                self, "Delete row",
                "Remove %s / %s from users.json?"
                % (row.env or "(no env)", row.login or "(no login)"),
                "The profile folder it used stays on disk.",
                agree="Delete", kind="warn"):
            return
        del self._rows[index]
        self._revealed.discard(index)
        self._rebuild()
        self._validate()

    def _rebuild(self):
        self._loading = True
        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            self._set(index, self.COL_ENV, row.env, mono=True)
            self._set(index, self.COL_CLASS, row.cls)
            self._set(index, self.COL_LOGIN, row.login, mono=True)
            self._set(index, self.COL_PASSWORD,
                      row.password if index in self._revealed else "•" * 10,
                      mono=True, editable=index in self._revealed)
            self._set(index, self.COL_TESTS, row.tests_text(), mono=True)
            self.table.setCellWidget(index, self.COL_ACTIONS, self._actions(index))
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(self.COL_ACTIONS, 150)
        self._loading = False

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
        if not (0 <= index < len(self._rows)):
            return
        row = self._rows[index]
        text = item.text()
        if col == self.COL_ENV:
            row.env = text.strip()
        elif col == self.COL_CLASS:
            row.cls = text.strip()
        elif col == self.COL_LOGIN:
            row.login = text.strip()
        elif col == self.COL_PASSWORD and index in self._revealed:
            row.password = text
        elif col == self.COL_TESTS:
            try:
                row.tests = usersfile.parse_tests(text)
            except usersfile.UsersFileError as exc:
                self._show_problem(str(exc))
                return
        self._validate()

    # -- validation -----------------------------------------------------------
    def _validate(self):
        problems = usersfile.validate(self._rows)
        if problems:
            self._show_problem("\n".join(problems[:6]))
        else:
            self._show_ok("%d row(s) · env+login unique · every row has class, login "
                          "and password · run-tests uses tag: (not tags:)"
                          % len(self._rows))
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
