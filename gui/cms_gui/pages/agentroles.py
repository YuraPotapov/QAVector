"""Integrations -> Agents -> Roles: who an agent may hand work to (QCR ADR-010).

A role is the person's own - dev, tester, reviewer: what it does (what agents
choose it by), the agent program it runs as, its model and effort, the most it
may do, instructions for each of its tasks, and whether it ends with a verdict.
Every conversation that works with other agents is offered every role, and is
told the list in its session.

The roles are a table with the chosen one below it, read-only until Edit;
Edit turns its values into fields where they stand (Save, Cancel, Ctrl+S, Esc).
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from .. import widgets
from .jiraconnections import _cell, _table

MODES = (("Read", "read"), ("Edit", "edit"), ("Full", "full"))
MODE_WORDS = {"read": "Read", "edit": "Edit", "full": "Full"}


class RolesTab(QWidget):
    HEADERS = ("Name", "Id", "Does", "Agent", "Model", "Effort", "Mode", "Verdict")

    #: The tab wants the roles: the core's file, no agent asked.
    wanted = Signal()
    #: ``(role, new, done(payload))`` - keep a role.
    save_requested = Signal(dict, bool, object)
    #: ``(id, done(payload))`` - remove one.
    delete_requested = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.roles, self.templates, self.connections, self.kinds = [], [], [], []
        self.editing = None             # the role as it was when Edit was pressed; {} for a new one
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(8)
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        self.table.setObjectName("agentRoles")
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for index, width in ((0, 130), (1, 90), (3, 110), (4, 120), (5, 70), (6, 110), (7, 64)):
            self.table.setColumnWidth(index, width)
        self.table.itemSelectionChanged.connect(self._chosen)
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._say_empty)
        self.note = widgets.empty_note(self.table, "")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)
        self.new_button = QPushButton("New role")
        self.new_button.clicked.connect(self._new)
        self.usual_button = QPushButton("Add the usual roles")
        self.usual_button.setToolTip("dev, tester and reviewer, filled in for you to change")
        self.usual_button.clicked.connect(self._add_usual)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setProperty("variant", "ghost")
        self.remove_button.clicked.connect(self._remove)
        column.addWidget(widgets.row(self.new_button, self.usual_button, None, self.remove_button))
        column.addWidget(self._form())
        self.message = QLabel("")
        self.message.setProperty("role", "muted")
        self.message.setWordWrap(True)
        column.addWidget(self.message)
        self._fill()

    # -- the form -----------------------------------------------------------------
    def _form(self):
        holder = QWidget()
        holder.setObjectName("agentRoleForm")
        form = QVBoxLayout(holder)
        form.setContentsMargins(0, 6, 0, 0)
        form.setSpacing(6)
        self.title = widgets.heading("", "h2")
        self.edit_button = QPushButton("Edit")
        self.edit_button.clicked.connect(self._edit)
        form.addWidget(widgets.row(self.title, None, self.edit_button))
        self.role_id = QLineEdit()
        self.role_id.setPlaceholderText("dev")
        self.name = QLineEdit()
        self.name.setPlaceholderText("Developer")
        self.role_id.setToolTip("What agents and after: name it by. Fixed once made.")
        form.addWidget(widgets.row(widgets.field("Id", self.role_id), widgets.field("Name", self.name)))
        self.does = QPlainTextEdit()
        self.does.setMaximumHeight(64)
        self.does.setPlaceholderText("Changes code to do the task, and runs what it changed.")
        form.addWidget(widgets.field("Does", self.does, "What agents choose it by."))
        self.connection = widgets.capped(QComboBox(), 220)
        self.connection.setAccessibleName("Agent")
        self.connection.currentIndexChanged.connect(lambda _index: self._fill_efforts())
        self.model = QLineEdit()
        self.model.setPlaceholderText("The program's default")
        self.effort = widgets.capped(QComboBox(), 150)
        self.effort.setAccessibleName("Effort")
        form.addWidget(widgets.row(widgets.field("Agent", self.connection), widgets.field("Model", self.model),
                                   widgets.field("Effort", self.effort), None))
        self.mode = widgets.Segmented([label for label, _value in MODES], "Read")
        self.verdict = QCheckBox("Ends with a verdict: approve, or rework with its issues")
        form.addWidget(widgets.row(widgets.field("At most", self.mode, "Never more than the conversation that "
                                                 "hands it work has."), self.verdict, None))
        self.instructions = QPlainTextEdit()
        self.instructions.setMaximumHeight(96)
        self.instructions.setPlaceholderText("How this role works - put before each of its tasks.")
        form.addWidget(widgets.field("Instructions", self.instructions))
        self.problem = QLabel("")
        self.problem.setProperty("role", "error")
        self.problem.setWordWrap(True)
        self.problem.setVisible(False)
        form.addWidget(self.problem)
        self.save_button = QPushButton("Save")
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self._save)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel)
        self.edit_row = widgets.row(self.save_button, self.cancel_button, None)
        form.addWidget(self.edit_row)
        QShortcut(QKeySequence.Save, holder, self._save).setContext(Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence(Qt.Key_Escape), holder, self._cancel).setContext(Qt.WidgetWithChildrenShortcut)
        self.form = holder
        return holder

    # -- the outside ----------------------------------------------------------------
    def shown(self):
        self.wanted.emit()

    def say(self, text):
        self.message.setText(text or "")

    def set_roles(self, roles, templates=None, connections=None, kinds=None):
        chosen = self._selected_id()
        self.roles = [dict(one) for one in roles or []]
        self.templates = [dict(one) for one in templates or []]
        if connections is not None:
            self.connections = list(connections)
        if kinds is not None:
            self.kinds = list(kinds)
        self._fill(chosen)

    def has_unsaved(self):
        return self.editing is not None and self._entry() != self._as_entry(self.editing)

    # -- the table ------------------------------------------------------------------
    def _fill(self, chosen=None):
        names = {one["id"]: one.get("name") or one["id"] for one in self.connections}
        self.table.setRowCount(0)
        for role in self.roles:
            row = self.table.rowCount()
            self.table.insertRow(row)
            agent = names.get(role.get("connection")) or "%s - gone" % role.get("connection")
            values = (role.get("name"), role.get("id"), role.get("does"), agent, role.get("model") or "Default",
                      role.get("effort") or "Default", MODE_WORDS.get(role.get("mode"), role.get("mode")),
                      "Yes" if role.get("verdict") else "")
            for index, value in enumerate(values):
                item = _cell(value)
                if index == 0:
                    item.setData(Qt.UserRole, role.get("id"))
                if index == 3 and role.get("broken"):
                    item.setToolTip("Its agent program was removed: the role is offered to nobody until "
                                    "you choose another.")
                self.table.setItem(row, index, item)
        self.search.apply()
        ids = [one.get("id") for one in self.roles]
        if chosen in ids:
            self.table.selectRow(ids.index(chosen))
        elif ids and self.editing is None:
            self.table.selectRow(0)
        self.usual_button.setVisible(any(one["id"] not in ids for one in self.templates))
        self.remove_button.setVisible(bool(self.roles))
        self._say_empty()
        self._chosen()

    def _say_empty(self):
        self.note.say("No roles yet. Add the usual ones - dev, tester, reviewer - or a role of your own. "
                      "Agents can hand work to agent connections without them." if not self.roles else "")
        self.note.say_filtered("No role matches the search." if self.search.searching() else "")

    def _selected_id(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.UserRole) if item else None

    def _role(self, role_id):
        return next((one for one in self.roles if one.get("id") == role_id), None)

    def _chosen(self):
        if self.editing is not None:
            return
        role = self._role(self._selected_id())
        self.form.setVisible(role is not None)
        self.remove_button.setEnabled(role is not None)
        if role is not None:
            self._show(role, editable=False)

    # -- one role -------------------------------------------------------------------
    def _show(self, role, editable):
        self.title.setText(role.get("name") or "New role")
        self.role_id.setText(role.get("id") or "")
        self.name.setText(role.get("name") or "")
        self.does.setPlainText(role.get("does") or "")
        self.connection.blockSignals(True)
        self.connection.clear()
        for one in self.connections:
            self.connection.addItem(one.get("name") or one["id"], one["id"])
        if role.get("connection") and self.connection.findData(role["connection"]) < 0:
            self.connection.addItem("%s - gone" % role["connection"], role["connection"])
        self.connection.setCurrentIndex(max(0, self.connection.findData(role.get("connection"))))
        self.connection.blockSignals(False)
        self.model.setText(role.get("model") or "")
        self._fill_efforts(role.get("effort") or "")
        self.mode.set_current({value: label for label, value in MODES}.get(role.get("mode"), "Read"), notify=False)
        self.verdict.setChecked(bool(role.get("verdict")))
        self.instructions.setPlainText(role.get("instructions") or "")
        self.problem.setVisible(bool(role.get("broken")) and not editable)
        self.problem.setText("Its agent program was removed: choose another under Edit."
                             if role.get("broken") else "")
        self._editable(editable, new=not role.get("id") or role is self.editing and not self.editing.get("id"))

    def _fill_efforts(self, wanted=None):
        wanted = self.effort.currentData() if wanted is None else wanted
        connection = next((one for one in self.connections if one["id"] == self.connection.currentData()), {})
        kind = next((one for one in self.kinds if one.get("kind") == connection.get("kind")), {})
        self.effort.clear()
        self.effort.addItem("Default", "")
        for level in list(kind.get("efforts") or []) + ([wanted] if wanted and wanted not in
                                                        (kind.get("efforts") or []) else []):
            self.effort.addItem(level, level)
        self.effort.setCurrentIndex(max(0, self.effort.findData(wanted or "")))

    def _editable(self, on, new=False):
        for edit in (self.name, self.model):
            edit.setReadOnly(not on)
        self.role_id.setReadOnly(not (on and new))
        for edit in (self.does, self.instructions):
            edit.setReadOnly(not on)
        for widget in (self.connection, self.effort, self.mode, self.verdict):
            widget.setEnabled(on)
        self.edit_button.setVisible(not on)
        self.edit_row.setVisible(on)
        for button in (self.new_button, self.usual_button, self.remove_button):
            button.setEnabled(not on)
        self.table.setEnabled(not on)

    def _as_entry(self, role):
        return {key: role.get(key) for key in ("id", "name", "does", "connection", "model", "effort", "mode",
                                               "instructions", "verdict")}

    def _entry(self):
        return {"id": self.role_id.text().strip().lower(), "name": self.name.text().strip(),
                "does": self.does.toPlainText().strip(), "connection": self.connection.currentData() or "",
                "model": self.model.text().strip(), "effort": self.effort.currentData() or "",
                "mode": dict(MODES)[self.mode.current()], "instructions": self.instructions.toPlainText().strip(),
                "verdict": self.verdict.isChecked()}

    def _edit(self):
        role = self._role(self._selected_id())
        if role is None:
            return
        self.editing = dict(role)
        self._show(role, editable=True)
        self.name.setFocus()

    def _new(self):
        self.editing = {"id": "", "mode": "read", "connection": (self.connections[0]["id"]
                                                                 if self.connections else "")}
        self.form.setVisible(True)
        self._show(self.editing, editable=True)
        self.role_id.setFocus()

    def _cancel(self):
        if self.editing is None:
            return
        self.editing = None
        self.problem.setVisible(False)
        self._editable(False)
        self._chosen()

    def _save(self):
        if self.editing is None:
            return
        new = not self.editing.get("id")
        entry = self._entry()

        def done(payload):
            if not payload.get("ok"):
                self.problem.setText(payload.get("error") or "The role was not kept.")
                self.problem.setVisible(True)
                return
            self.editing = None
            self.problem.setVisible(False)
            self._editable(False)
            self.say("")
            self.wanted.emit()
        self.save_requested.emit(entry, new, done)

    def _add_usual(self):
        ids = {one.get("id") for one in self.roles}
        missing = [one for one in self.templates if one["id"] not in ids]
        if not missing:
            return

        def next_one(payload=None):
            if payload is not None and not payload.get("ok"):
                self.say(payload.get("error") or "A role was not kept.")
                self.wanted.emit()
                return
            if missing:
                self.save_requested.emit(missing.pop(0), True, next_one)
            else:
                self.say("Added the usual roles. Change their models and instructions under Edit.")
                self.wanted.emit()
        next_one()

    def _remove(self):
        role = self._role(self._selected_id())
        if role is None or not widgets.confirm(
                self, "Remove role", "Remove the role %s?" % role.get("name"),
                "Agents can no longer hand it work. Conversations it already worked in are kept.",
                agree="Remove", refuse="Keep it", kind="warn"):
            return
        self.delete_requested.emit(role["id"], lambda payload: self.wanted.emit())
