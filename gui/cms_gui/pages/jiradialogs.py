"""The small windows of Integrations -> Jira that change an issue.

Each asks the core only for what it shows, when it shows it: the create window
asks for a project's issue types, then for the chosen type's fields. Nothing is
sent until the person presses the button that sends it, and what Jira says back
- fields a transition needs - is said here, in the window that asked. Editing
an issue is not a window: it happens in place, in the issue (``jira.py``).
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout,
                               QWidget)

from .. import widgets

#: What a field's schema type must be for the create window to fill it in.
SIMPLE = ("string", "number", "option", "priority", "array")


def _problem():
    label = QLabel("")
    label.setProperty("role", "error")
    label.setWordWrap(True)
    label.setVisible(False)
    return label


def _buttons(dialog, agree):
    row = QHBoxLayout()
    row.addStretch(1)
    cancel = QPushButton("Cancel")
    cancel.clicked.connect(dialog.reject)
    row.addWidget(cancel)
    button = QPushButton(agree)
    button.setProperty("variant", "primary")
    button.setDefault(True)
    row.addWidget(button)
    return row, button


def split_labels(text):
    """Labels as typed - separated by spaces or commas - as a list."""
    return [one for one in (part.strip() for part in str(text or "").replace(",", " ").split())
            if one]


# -- a transition's screen ----------------------------------------------------
class TransitionFieldsDialog(QDialog):
    """What a transition's screen asks for - each a choice among what Jira allows.
    A field with nothing to choose from cannot be filled here: Jira does it."""

    def __init__(self, parent, transition, needs):
        super().__init__(parent)
        self.setWindowTitle(transition or "Move issue")
        self.frame = widgets.dress(self)
        self.setMinimumWidth(420)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        column.addWidget(widgets.lede("%s asks for a little more." % (transition or "This move")))
        self.choices = {}
        missing = []
        for need in needs or []:
            if not need.get("allowed"):
                missing.append(need.get("name") or need.get("key"))
                continue
            combo = QComboBox()
            for one in need["allowed"]:
                combo.addItem(one.get("name") or one.get("id"), one.get("id"))
            self.choices[need["key"]] = combo
            column.addWidget(widgets.field(need.get("name") or need["key"], combo))
        self.problem = _problem()
        if missing:
            self.problem.setText("It also asks for %s, which can only be filled in Jira. "
                                 "Open the issue in Jira to move it." % ", ".join(missing))
            self.problem.setVisible(True)
        column.addWidget(self.problem)
        row, self.move = _buttons(self, transition or "Move")
        self.move.setEnabled(not missing)
        self.move.clicked.connect(self.accept)
        column.addLayout(row)

    def fields(self):
        return {key: combo.currentData() for key, combo in self.choices.items()}


# -- creating -----------------------------------------------------------------
class CreateIssueDialog(QDialog):
    """A new issue: a project, its type, and what that type's screen asks for.

    The types are read when a project is chosen and the fields when a type is.
    A required field this window cannot fill in - a user picker, a date - is
    said, and the issue is created in Jira instead.
    """

    #: (request for ``create_meta``, done(payload))
    meta_requested = Signal(dict, object)
    #: (request for ``create``, done(payload))
    create_requested = Signal(dict, object)

    def __init__(self, parent, projects, project=""):
        super().__init__(parent)
        self.setWindowTitle("Create issue")
        self.frame = widgets.dress(self)
        self.setMinimumWidth(560)
        self.inputs = {}
        column = QVBoxLayout(self)
        column.setSpacing(8)
        self.project = QComboBox()
        self.project.addItem("Choose a project", "")
        for key, name in projects:
            self.project.addItem("%s  ·  %s" % (name, key) if name else key, key)
        self.project.currentIndexChanged.connect(self._project_picked)
        column.addWidget(widgets.field("Project", self.project))
        self.type = QComboBox()
        self.type.setEnabled(False)
        self.type.currentIndexChanged.connect(self._type_picked)
        column.addWidget(widgets.field("Type", self.type))
        self.form = QWidget()
        self.form_layout = QVBoxLayout(self.form)
        self.form_layout.setContentsMargins(0, 0, 0, 0)
        self.form_layout.setSpacing(8)
        column.addWidget(self.form)
        self.problem = _problem()
        column.addWidget(self.problem)
        row, self.create = _buttons(self, "Create")
        self.create.setEnabled(False)
        self.create.clicked.connect(self._create)
        column.addLayout(row)
        index = self.project.findData(project) if project else -1
        if index > 0:
            self.project.setCurrentIndex(index)

    def _say(self, text):
        self.problem.setText(text)
        self.problem.setVisible(bool(text))

    def _project_picked(self, *_args):
        self.type.clear()
        self.type.setEnabled(False)
        self._clear_form()
        project = self.project.currentData()
        if not project:
            return
        self.type.addItem("Loading…", "")

        def done(payload):
            if self.project.currentData() != project:
                return
            self.type.blockSignals(True)
            self.type.clear()
            if not payload.get("ok"):
                self._say(payload.get("error") or "The issue types could not be read.")
                self.type.blockSignals(False)
                return
            self._say("")
            types = [one for one in (payload.get("result") or {}).get("types") or []
                     if not one.get("subtask")]
            self.type.addItem("Choose a type", "")
            for one in types:
                self.type.addItem(one["name"], one["id"])
            self.type.setEnabled(True)
            self.type.blockSignals(False)
            if len(types) == 1:
                self.type.setCurrentIndex(1)

        self.meta_requested.emit({"project": project}, done)

    def _type_picked(self, *_args):
        self._clear_form()
        project, kind = self.project.currentData(), self.type.currentData()
        if not project or not kind:
            return

        def done(payload):
            if self.type.currentData() != kind:
                return
            if not payload.get("ok"):
                self._say(payload.get("error") or "The fields could not be read.")
                return
            self._say("")
            self._build((payload.get("result") or {}).get("fields") or [])

        self.meta_requested.emit({"project": project, "type": kind}, done)

    def _clear_form(self):
        self.inputs = {}
        while self.form_layout.count():
            item = self.form_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        self.create.setEnabled(False)

    def _build(self, fields):
        """Summary and description always; priority and labels when the screen
        has them; any other field only when it is required."""
        blocked = []
        for one in fields:
            key, kind, required = one["key"], one.get("type", ""), one.get("required")
            if key not in ("summary", "description", "priority", "labels") and not required:
                continue
            if key == "description":
                widget = QPlainTextEdit()
                widget.setMinimumHeight(120)
            elif one.get("allowed"):
                widget = QComboBox()
                if not required:
                    widget.addItem("Default", "")
                for value in one["allowed"]:
                    widget.addItem(value["name"], value["id"])
            elif kind in ("string", "number") or (kind == "array" and one.get("items") == "string"):
                widget = QLineEdit()
                if key == "labels":
                    widget.setProperty("mono", "true")
            else:
                if required:
                    blocked.append(one["name"])
                continue
            self.inputs[key] = (widget, one)
            self.form_layout.addWidget(widgets.field(
                one["name"] + (" *" if required and key != "summary" else ""), widget,
                "Separated by spaces or commas." if key == "labels" else None))
        if "summary" not in self.inputs:
            widget = QLineEdit()
            self.inputs["summary"] = (widget, {"key": "summary", "name": "Summary",
                                               "type": "string", "required": True})
            self.form_layout.insertWidget(0, widgets.field("Summary", widget))
        if blocked:
            self._say("This type also asks for %s, which can only be filled in Jira. "
                      "Create it there, or choose another type." % ", ".join(blocked))
        self.create.setEnabled(not blocked)
        self.adjustSize()

    def request(self):
        fields = {}
        for key, (widget, spec) in self.inputs.items():
            if isinstance(widget, QPlainTextEdit):
                value = widget.toPlainText()
            elif isinstance(widget, QComboBox):
                value = {"id": widget.currentData()} if widget.currentData() else ""
            elif key == "labels" or spec.get("type") == "array":
                value = split_labels(widget.text())
            elif spec.get("type") == "number":
                text = widget.text().strip()
                try:
                    value = float(text) if text else ""
                except ValueError:
                    value = text
            else:
                value = widget.text().strip()
            fields[key] = value
        return {"project": self.project.currentData(), "type": self.type.currentData(),
                "fields": fields}

    def _create(self):
        request = self.request()
        missing = [spec["name"] for key, (widget, spec) in self.inputs.items()
                   if spec.get("required") and request["fields"].get(key) in ("", [], None)]
        if missing:
            self._say("Fill in %s." % ", ".join(missing))
            return
        self.create.setEnabled(False)
        self.create.setText("Creating…")

        def done(payload):
            self.create.setEnabled(True)
            self.create.setText("Create")
            if payload.get("ok"):
                self.created = (payload.get("result") or {}).get("issue") or {}
                self.accept()
            else:
                self._say(payload.get("error") or "Not created.")

        self.create_requested.emit(request, done)
