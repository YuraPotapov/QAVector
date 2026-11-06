"""The small windows of Integrations -> Jira that change an issue.

Each asks the core only for what it shows, when it shows it: the editor asks
what the issue's screen lets this account change; the create window asks for a
project's issue types, then for the chosen type's fields. Nothing is sent until
the person presses the button that sends it, and what Jira says back - fields a
transition needs, an issue changed meanwhile - is said here, in the window that
asked.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
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


def _labels(text):
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


# -- editing ------------------------------------------------------------------
class EditIssueDialog(QDialog):
    """Summary, description, labels and priority of one issue.

    Only what changed is sent, with when the issue was last changed as it was
    opened - so an edit over somebody else's change is refused rather than made.
    A description with formatting plain text cannot hold is only replaced when
    the person says so.
    """

    #: (request for ``edit``, done(payload))
    save_requested = Signal(dict, object)

    def __init__(self, parent, issue):
        super().__init__(parent)
        self.issue = dict(issue)
        self.setWindowTitle("Edit %s" % issue.get("key", ""))
        self.frame = widgets.dress(self)
        self.setMinimumWidth(560)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        self.summary = QLineEdit(issue.get("summary") or "")
        column.addWidget(widgets.field("Summary", self.summary))
        self.description = QPlainTextEdit(issue.get("description_text") or "")
        self.description.setMinimumHeight(160)
        column.addWidget(widgets.field("Description", self.description))
        rich = not issue.get("description_plain", True)
        self.rich_note = QLabel("This description has formatting - lists, tables or "
                                "pictures - that plain text cannot hold. Edit it in Jira "
                                "to keep it, or replace it with the text above.")
        self.rich_note.setProperty("role", "hint")
        self.rich_note.setWordWrap(True)
        self.rich_note.setVisible(rich)
        column.addWidget(self.rich_note)
        self.rich = QCheckBox("Replace the formatted description with this text")
        self.rich.setVisible(rich)
        self.rich.toggled.connect(self.description.setEnabled)
        if rich:
            self.description.setEnabled(False)
        column.addWidget(self.rich)
        self.labels = QLineEdit(" ".join(issue.get("labels") or []))
        self.labels.setProperty("mono", "true")
        column.addWidget(widgets.field("Labels", self.labels, "Separated by spaces or commas."))
        self.priority = QComboBox()
        self.priority.addItem(issue.get("priority") or "No priority", "")
        self.priority.setEnabled(False)
        column.addWidget(widgets.field("Priority", self.priority))
        self.problem = _problem()
        column.addWidget(self.problem)
        row, self.save = _buttons(self, "Save")
        self.save.clicked.connect(self._save)
        column.addLayout(row)

    def set_meta(self, meta):
        """What the issue's screen lets this account change, read when the
        editor opened. A field it leaves out stays as it is."""
        editable = set(meta.get("editable") or [])
        for name, widget in (("summary", self.summary), ("labels", self.labels)):
            widget.setEnabled(name in editable)
        if "description" not in editable:
            self.description.setEnabled(False)
            self.rich.setVisible(False)
            self.rich_note.setVisible(False)
        current = self.issue.get("priority") or ""
        self.priority.clear()
        for one in meta.get("priorities") or []:
            self.priority.addItem(one["name"], one["id"])
        index = self.priority.findText(current)
        if index < 0:
            self.priority.insertItem(0, current or "No priority", "")
            index = 0
        self.priority.setCurrentIndex(index)
        self.priority.setEnabled("priority" in editable and self.priority.count() > 1)

    def changes(self):
        fields = {}
        if self.summary.isEnabled() and self.summary.text().strip() != (
                self.issue.get("summary") or ""):
            fields["summary"] = self.summary.text().strip()
        if self.description.isEnabled() and self.description.toPlainText().strip() != (
                self.issue.get("description_text") or "").strip():
            fields["description"] = self.description.toPlainText()
        labels = _labels(self.labels.text())
        if self.labels.isEnabled() and labels != list(self.issue.get("labels") or []):
            fields["labels"] = labels
        if (self.priority.isEnabled() and self.priority.currentData()
                and self.priority.currentText() != (self.issue.get("priority") or "")):
            fields["priority"] = self.priority.currentData()
        return fields

    def _save(self):
        fields = self.changes()
        if not fields:
            self.reject()
            return
        self.save.setEnabled(False)
        self.save.setText("Saving…")

        def done(payload):
            self.save.setEnabled(True)
            self.save.setText("Save")
            if payload.get("ok"):
                self.accept()
            else:
                self.problem.setText(payload.get("error") or "Not saved.")
                self.problem.setVisible(True)

        self.save_requested.emit({"key": self.issue.get("key"),
                                  "updated": self.issue.get("updated") or "",
                                  "fields": fields}, done)


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
                value = _labels(widget.text())
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
