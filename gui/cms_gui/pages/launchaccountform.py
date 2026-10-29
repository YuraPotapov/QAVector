"""+ Account: one account for the environment on screen, saved and chosen.

Adding an account used to be a trip to Credentials, a row added to a table whose
environment was copied from whichever row happened to be last, a Save there and
a way back. Here the environment is the one Launch Sessions is at - shown, not
asked for - and the form asks only what an account is: what it is called on the
window, its login and its password. Its own scenarios are folded away, since
most accounts have none.

It writes users.json the way the Credentials table does (:mod:`cms_gui.usersfile`:
read afresh, every rule the launcher would exit on checked first, an atomic save
with a backup) and leaves the rest of the file exactly as it was - the rows, their
order and the keys it does not know.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout)

from .. import usersfile, widgets


class AccountForm(QDialog):
    """See the module docstring. ``saved_login`` is set once one is saved."""

    def __init__(self, parent, environment, users_path, inventory=None):
        super().__init__(parent)
        self.setWindowTitle("Add an account")
        self.environment = environment
        self.users_path = users_path or ""
        self.saved_login = ""
        self.frame = widgets.dress(self)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "A user of %s: what its window is called, its login and its password."
            % environment.name))

        self.role = QLineEdit()
        self.role.setPlaceholderText("what the window is called - Operator, Admin…")
        column.addWidget(widgets.field("Role", self.role))
        self.login = QLineEdit()
        self.login.setProperty("mono", True)
        column.addWidget(widgets.field("Login", self.login))
        self.password = QLineEdit()
        self.password.setProperty("mono", True)
        self.password.setEchoMode(QLineEdit.Password)
        self.reveal = QPushButton("Show")
        self.reveal.setProperty("variant", "ghost")
        self.reveal.setCheckable(True)
        self.reveal.toggled.connect(self._reveal)
        column.addWidget(widgets.field("Password", widgets.row(self.password,
                                                               self.reveal)))
        self.more = widgets.Disclosure("Scenarios of its own")
        self.tests = QLineEdit()
        self.tests.setProperty("mono", True)
        self.tests.setPlaceholderText("scenario ids, or tag:name, comma-separated")
        self.more.body().addWidget(widgets.field(
            "Run with \"Use each account's own list\"", self.tests))
        column.addWidget(self.more)

        self.problem = QLabel("")
        self.problem.setProperty("role", "error")
        self.problem.setWordWrap(True)
        self.problem.setVisible(False)
        column.addWidget(self.problem)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = QPushButton("Save and select")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(self.save_button)
        column.addLayout(buttons)
        self.resize(max(460, self.sizeHint().width()), self.sizeHint().height())
        self.login.setFocus(Qt.OtherFocusReason)

    def _reveal(self, shown):
        self.password.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password)
        self.reveal.setText("Hide" if shown else "Show")

    def _say(self, text):
        self.problem.setText(text)
        self.problem.setVisible(bool(text))

    def row(self):
        """The account as typed, as a users.json row of this environment."""
        tests = usersfile.parse_tests(self.tests.text()) if self.tests.text().strip() \
            else []
        return usersfile.UserRow(env=self.environment.account_key,
                                 cls=self.role.text().strip(),
                                 login=self.login.text().strip(),
                                 password=self.password.text(), tests=tests)

    def save(self):
        """Add the account to users.json; stay open, saying why, if it cannot be."""
        if not self.users_path:
            self._say("There is no users.json to write to: set its path in "
                      "Settings -> Config.")
            return False
        try:
            account = self.row()
            rows = usersfile.load(self.users_path)
        except usersfile.UsersFileError as exc:
            self._say(str(exc))
            return False
        problems = usersfile.validate(rows + [account])
        mine = [problem for problem in problems
                if problem.startswith("row %d" % (len(rows) + 1))
                or account.env in problem]
        if problems:
            self._say("\n".join((mine or problems)[:3]))
            return False
        try:
            usersfile.save(self.users_path, rows + [account])
        except usersfile.UsersFileError as exc:
            self._say(str(exc))
            return False
        self.saved_login = account.login
        self.accept()
        return True
