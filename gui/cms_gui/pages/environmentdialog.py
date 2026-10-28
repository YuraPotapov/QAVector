"""The two small forms of the Environments page: an environment, and a variable.

**An environment** is a name, the account key its accounts are filed under (a
users.json ``env`` value - one there already, or a new one for an environment
nobody has accounts in yet), the address it opens, written with the project's
variables if it likes, and the values it gives them. The address is shown
filled in as it is typed, so ``http://localhost:{{port}}/web`` reads as the
address it will be before it is saved.

**A variable** is a name, a type and, optionally, a default every environment
of the project gets unless it gives its own.

Both check what they can before closing - a name taken, a key the launcher
could not read, a value of the wrong type - and say it in the form.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMenu, QPushButton, QVBoxLayout, QWidget)

from .. import launch, launchvars, usersfile, widgets


class EnvironmentDialog(QDialog):
    """Add or edit one environment of a project."""

    def __init__(self, parent, project, environment=None, account_keys=(),
                 taken_names=(), known_keys=()):
        super().__init__(parent)
        self.setWindowTitle("Edit environment" if environment else "Add an environment")
        self.project = project
        self.environment = environment
        self._taken = {name.casefold() for name in taken_names
                       if environment is None or name != environment.name}
        self._known_keys = [key for key in known_keys if key]
        self._definitions = project.definitions()
        self.frame = widgets.dress(self)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "An environment of %s: where it opens, and whose accounts it uses."
            % project.name))

        self.name = QLineEdit(environment.name if environment else "")
        self.name.setPlaceholderText("Local, Staging, Branch 42…")
        column.addWidget(widgets.field("Name", self.name))

        self.account_key = widgets.capped(QComboBox(), 420)
        self.account_key.setEditable(True)
        self.account_key.setProperty("mono", True)
        for key in account_keys:
            self.account_key.addItem(key)
        self.account_key.setCurrentText(environment.account_key if environment else "")
        self.account_key.lineEdit().setPlaceholderText("the users.json env its accounts carry")
        column.addWidget(widgets.field(
            "Account key", self.account_key,
            "The env value in users.json its accounts and profile folders are filed "
            "under. Changing the address or a port never changes this."))

        self.address = QLineEdit(environment.address if environment else "")
        self.address.setProperty("mono", True)
        self.address.setPlaceholderText("http://localhost:{{port}}/web - empty: the "
                                        "origin of the account key")
        self.address.textChanged.connect(self._preview)
        self.insert = QPushButton("Insert variable")
        self.insert.setProperty("variant", "ghost")
        self.insert_menu = QMenu(self.insert)
        for name in sorted(self._definitions):
            action = self.insert_menu.addAction("{{%s}}" % name)
            action.triggered.connect(lambda _c=False, n=name: self._insert(n))
        self.insert.setMenu(self.insert_menu)
        self.insert.setEnabled(bool(self._definitions))
        if not self._definitions:
            self.insert.setToolTip("The project defines no variables yet - add one "
                                   "under Variables.")
        column.addWidget(widgets.field("Address",
                                       widgets.row(self.address, self.insert)))
        self.preview = widgets.mono("")
        self.preview.setWordWrap(True)
        column.addWidget(self.preview)

        self.values = {}
        if self._definitions:
            column.addWidget(widgets.kicker("Values here"))
            grid_box = QWidget()
            grid = QGridLayout(grid_box)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(10)
            given = dict(environment.variables) if environment else {}
            for line, name in enumerate(sorted(self._definitions)):
                definition = self._definitions[name]
                label = QLabel("%s  (%s)" % (name, definition.type))
                label.setProperty("role", "mono")
                raw = given.get(name)
                text = "" if raw is None else (
                    raw if isinstance(raw, str) else launchvars.text_of(definition, raw))
                value = QLineEdit(text)
                value.setProperty("mono", True)
                value.setPlaceholderText(
                    "inherits %s from the project" % launchvars.text_of(
                        definition, definition.default)
                    if definition.default is not None else "no value")
                value.textChanged.connect(self._preview)
                grid.addWidget(label, line, 0)
                grid.addWidget(value, line, 1)
                self.values[name] = value
            column.addWidget(grid_box)

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
        self.save_button = QPushButton("Save")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._accept)
        buttons.addWidget(self.save_button)
        column.addLayout(buttons)
        self.resize(max(560, self.sizeHint().width()), self.sizeHint().height())
        self._preview()

    def _insert(self, name):
        self.address.insert("{{%s}}" % name)
        self.address.setFocus(Qt.OtherFocusReason)

    def typed_values(self):
        """``(values, problems)``: what the boxes give each variable, typed."""
        found, problems = {}, []
        for name, box in self.values.items():
            text = box.text().strip()
            if not text:
                continue
            value, error = launchvars.coerce(self._definitions[name], text)
            if error:
                problems.append("%s %s." % (name, error))
            else:
                found[name] = value
        return found, problems

    def _preview(self, *_args):
        template = self.address.text().strip()
        if not template:
            self.preview.setText("Opens the origin of its account key.")
            return
        values, _problems = self.typed_values()
        result = launchvars.resolve(template, self._definitions,
                                    [launchvars.Layer("environment", values)])
        if result.problems:
            self.preview.setText(result.problems[0].message)
            return
        url, problem = launch.normalize_address(result.text)
        self.preview.setText("Opens %s" % url if url else "The address %s." % problem)

    def result_values(self):
        """``(name, account key, address, values)`` as typed."""
        values, _problems = self.typed_values()
        return (self.name.text().strip(), self.account_key.currentText().strip(),
                self.address.text().strip(), values)

    def problems(self):
        name, key, _address, _values = self.result_values()
        found = []
        if not name:
            found.append("Give it a name.")
        elif name.casefold() in self._taken:
            found.append("%s already has an environment called %s."
                         % (self.project.name, name))
        if not key:
            found.append("Give it an account key: the users.json env its accounts "
                         "carry.")
        else:
            others = [one for one in self._known_keys if one != key]
            found.extend(usersfile.env_problems(others + [key]))
        found.extend(self.typed_values()[1])
        return found

    def _accept(self):
        problems = self.problems()
        if problems:
            self.problem.setText("\n".join(problems[:4]))
            self.problem.setVisible(True)
            return
        self.accept()


class VariableDialog(QDialog):
    """Add or redefine one variable of a project."""

    def __init__(self, parent, project, name="", kind="port", default=None,
                 taken=()):
        super().__init__(parent)
        self.setWindowTitle("Edit variable" if name else "Add a variable")
        self.project = project
        self._editing = bool(name)
        self._taken = {one for one in taken if one != name}
        self.frame = widgets.dress(self)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "A variable of %s: written as {{name}} in an address, given a value "
            "by each environment, a configuration or a single run." % project.name))
        self.name = QLineEdit(name)
        self.name.setProperty("mono", True)
        self.name.setReadOnly(self._editing)
        column.addWidget(widgets.field("Name", self.name))
        self.kind = widgets.capped(QComboBox(), 160)
        for one in launchvars.TYPES:
            self.kind.addItem(one)
        self.kind.setCurrentText(kind)
        column.addWidget(widgets.field("Type", self.kind))
        self.default = QLineEdit("" if default is None else str(default))
        self.default.setProperty("mono", True)
        self.default.setPlaceholderText("none - every environment gives its own")
        column.addWidget(widgets.field("Default", self.default))
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
        save = QPushButton("Save")
        save.setProperty("variant", "primary")
        save.setDefault(True)
        save.clicked.connect(self._accept)
        buttons.addWidget(save)
        column.addLayout(buttons)
        self.resize(max(440, self.sizeHint().width()), self.sizeHint().height())

    def result_values(self):
        """``(name, type, default or None)``, the default typed."""
        name = self.name.text().strip()
        kind = self.kind.currentText()
        text = self.default.text().strip()
        if not text:
            return name, kind, None
        value, _error = launchvars.coerce(launchvars.Definition(name, kind, None, True, ()),
                                          text)
        return name, kind, value

    def problems(self):
        name = self.name.text().strip()
        found = []
        if not launchvars.valid_name(name):
            found.append("A name is letters, digits and _, not starting with a "
                         "digit, and not one of: %s." % ", ".join(sorted(launchvars.RESERVED)))
        elif name in self._taken:
            found.append("%s already has a variable called %s." % (self.project.name, name))
        text = self.default.text().strip()
        if text:
            _value, error = launchvars.coerce(
                launchvars.Definition(name, self.kind.currentText(), None, True, ()), text)
            if error:
                found.append("The default %s." % error)
        return found

    def _accept(self):
        problems = self.problems()
        if problems:
            self.problem.setText("\n".join(problems))
            self.problem.setVisible(True)
            return
        self.accept()
