"""The two small windows of the Environments page: a new environment, and who
is in one.

**A new environment** is a name and its ``url`` - where its windows open, which
may use variables the environment is given afterwards
(``http://localhost:{{port}}/web``). The url is shown filled in as it is typed.

**Who is in an environment** is everyone in users.json, ticked when they are in
it. Ticking puts someone in, unticking takes them out; ``New user…`` adds an
account straight into it. Someone whose login is already here with another
password or role cannot be ticked a second time - the launcher files a profile
by environment and login, and two of one would share it.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, \
    QVBoxLayout

from .. import launch, launchprojectsfile as lpf, launchvars, usersfile, widgets
from .launchaccountform import AccountForm


def opens_text(template, variables=None):
    """``(text, bad)``: "Opens http://…" for ``template``, or what stops it."""
    if not template:
        return "No url yet - its windows cannot open until url has a value.", True
    scope = lpf.scope(variables or {})
    result = launchvars.resolve(template, scope.definitions, scope.layers)
    if result.problems:
        problem = result.problems[0]
        if problem.code == "unknown":
            names = [name for name in launchvars.references(template)
                     if name not in scope.definitions]
            if lpf.URL in names:
                return "url cannot use itself.", True
            if names:
                return "{{%s}} has no value yet." % names[0], True
        return problem.message, True
    url, problem = launch.normalize_address(result.text)
    return ("Opens %s" % url, False) if url else ("The url %s." % problem, True)


def _problem_label():
    label = QLabel("")
    label.setProperty("role", "error")
    label.setWordWrap(True)
    label.setVisible(False)
    return label


class EnvironmentDialog(QDialog):
    """A new environment of a project: its name and url."""

    def __init__(self, parent, project_name, taken_names=()):
        super().__init__(parent)
        self.setWindowTitle("Add an environment")
        self.project_name = project_name
        self._taken = {name.casefold() for name in taken_names}
        self.frame = widgets.dress(self)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "An environment of %s. Its variables and users are added on the page "
            "once it is there." % project_name))
        self.name = QLineEdit()
        self.name.setPlaceholderText("Local, Staging, Branch 42…")
        column.addWidget(widgets.field("Name", self.name))
        self.url = QLineEdit()
        self.url.setProperty("mono", True)
        self.url.setPlaceholderText("http://localhost:{{port}}/web")
        self.url.textChanged.connect(self._preview)
        column.addWidget(widgets.field(
            "url", self.url, "Where its windows open. {{name}} uses a variable you "
            "add to it afterwards."))
        self.preview = widgets.mono("")
        self.preview.setWordWrap(True)
        column.addWidget(self.preview)
        self.problem = _problem_label()
        column.addWidget(self.problem)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = QPushButton("Add")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._accept)
        buttons.addWidget(self.save_button)
        column.addLayout(buttons)
        self.resize(max(520, self.sizeHint().width()), self.sizeHint().height())
        self._preview()
        self.name.setFocus(Qt.OtherFocusReason)

    def _preview(self, *_args):
        text, _bad = opens_text(self.url.text().strip())
        self.preview.setText(text if self.url.text().strip() else "")

    def result_values(self):
        """``(name, url)`` as typed."""
        return self.name.text().strip(), self.url.text().strip()

    def problems(self):
        name, url = self.result_values()
        found = []
        if not name:
            found.append("Give it a name.")
        elif name.casefold() in self._taken:
            found.append("%s already has an environment called %s."
                         % (self.project_name, name))
        if not url:
            found.append("Give it a url: where its windows open.")
        return found

    def _accept(self):
        problems = self.problems()
        if problems:
            self.problem.setText("\n".join(problems))
            self.problem.setVisible(True)
            return
        self.accept()


class UsersPicker(QDialog):
    """Who is in one environment, ticked from everyone in users.json.

    ``changes()`` says who to put in and who to take out once it is accepted;
    ``wrote`` is True when New user… already added someone to the file.
    """

    def __init__(self, parent, environment, users_path, env_names=None):
        super().__init__(parent)
        self.setWindowTitle("Users of %s" % environment.name)
        self.environment = environment
        self.users_path = users_path or ""
        self.env_names = dict(env_names or {})
        self.wrote = False
        self._people = []
        self._here = []
        self.frame = widgets.dress(self)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "Tick who signs in to %s. Unticking takes someone out of it - they stay "
            "in Credentials." % environment.name))
        self.list = widgets.CheckList(searchable=True,
                                      placeholder="Search by login, role or "
                                                  "environment…",
                                      selected_view=True)
        self.list.set_noun("users")
        self.list.list.setMinimumHeight(260)
        column.addWidget(self.list, 1)
        self.problem = _problem_label()
        column.addWidget(self.problem)
        buttons = QHBoxLayout()
        self.new_button = QPushButton("New user…")
        self.new_button.setToolTip("Add an account to users.json, in %s"
                                   % environment.name)
        self.new_button.clicked.connect(self.new_user)
        buttons.addWidget(self.new_button)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = QPushButton("Save")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.accept)
        buttons.addWidget(self.save_button)
        column.addLayout(buttons)
        self._load()
        self.resize(max(620, self.sizeHint().width()), max(480, self.sizeHint().height()))

    def _load(self, keep=None):
        try:
            rows = usersfile.load(self.users_path)
        except usersfile.UsersFileError as exc:
            rows = []
            self.problem.setText(str(exc))
            self.problem.setVisible(True)
        key = self.environment.account_key
        self._people = usersfile.credentials(rows)
        self._here = [person.key for person in self._people if key in person.envs(rows)]
        here_logins = {person.login for person in self._people if person.key in self._here}
        self.list.clear()
        for index, person in enumerate(self._people):
            if not person.login:
                continue
            places = [self.env_names.get(env, env) for env in person.envs(rows)]
            note = "%s  ·  %s" % (person.role or "—",
                                  ", ".join(places) if places else "no environment")
            usable = person.key in self._here or person.login not in here_logins
            if not usable:
                note += "  ·  already here with another password or role"
            self.list.add(str(index), person.login, note, enabled=usable)
        wanted = set(self._here if keep is None else keep)
        self.list.set_checked([str(index) for index, person in enumerate(self._people)
                               if person.key in wanted])
        self.save_button.setEnabled(not self.problem.isVisible())
        self.new_button.setEnabled(bool(self.users_path) and not self.problem.isVisible())

    def _ticked(self):
        return {self._people[int(value)].key for value in self.list.checked()}

    def changes(self):
        """``(keys to put in, keys to take out)``, in the order of the file."""
        ticked = self._ticked()
        adding = [person.key for person in self._people
                  if person.key in ticked and person.key not in self._here]
        removing = [person.key for person in self._people
                    if person.key in self._here and person.key not in ticked]
        return adding, removing

    def new_user(self):
        """An account written straight into this environment, then ticked."""
        before = set(self._here)
        ticked = self._ticked()
        form = AccountForm(self, self.environment, self.users_path)
        if form.exec() != QDialog.Accepted:
            return
        self.wrote = True
        self._load()
        self._load(ticked | (set(self._here) - before))
